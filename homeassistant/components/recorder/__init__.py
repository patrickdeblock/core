"""Support for recording details."""
import asyncio
from collections import namedtuple
import concurrent.futures
from datetime import datetime, timedelta
import logging
import queue
from sqlite3 import Connection
import threading
import time
from typing import Any, Dict, Optional

from sqlalchemy import create_engine, exc
from sqlalchemy.engine import Engine
from sqlalchemy.event import listens_for
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool
import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_DOMAINS,
    CONF_ENTITIES,
    CONF_EXCLUDE,
    CONF_INCLUDE,
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    EVENT_TIME_CHANGED,
    MATCH_ALL,
)
from homeassistant.core import CoreState, HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entityfilter import generate_filter
from homeassistant.helpers.typing import ConfigType
import homeassistant.util.dt as dt_util

from . import migration, purge
from .const import DATA_INSTANCE
from .models import Base, Events, RecorderRuns, States
from .util import session_scope

_LOGGER = logging.getLogger(__name__)

DOMAIN = "recorder"

SERVICE_PURGE = "purge"

ATTR_KEEP_DAYS = "keep_days"
ATTR_REPACK = "repack"

SERVICE_PURGE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_KEEP_DAYS): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional(ATTR_REPACK, default=False): cv.boolean,
    }
)

DEFAULT_URL = "sqlite:///{hass_config_path}"
DEFAULT_DB_FILE = "home-assistant_v2.db"
DEFAULT_DB_MAX_RETRIES = 10
DEFAULT_DB_RETRY_WAIT = 3

CONF_DB_URL = "db_url"
CONF_DB_MAX_RETRIES = "db_max_retries"
CONF_DB_RETRY_WAIT = "db_retry_wait"
CONF_PURGE_KEEP_DAYS = "purge_keep_days"
CONF_PURGE_INTERVAL = "purge_interval"
CONF_EVENT_TYPES = "event_types"
CONF_COMMIT_INTERVAL = "commit_interval"

FILTER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_EXCLUDE, default={}): vol.Schema(
            {
                vol.Optional(CONF_DOMAINS): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(CONF_ENTITIES): cv.entity_ids,
                vol.Optional(CONF_EVENT_TYPES): vol.All(cv.ensure_list, [cv.string]),
            }
        ),
        vol.Optional(CONF_INCLUDE, default={}): vol.Schema(
            {
                vol.Optional(CONF_DOMAINS): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(CONF_ENTITIES): cv.entity_ids,
            }
        ),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN, default=dict): FILTER_SCHEMA.extend(
            {
                vol.Optional(CONF_PURGE_KEEP_DAYS, default=10): vol.All(
                    vol.Coerce(int), vol.Range(min=1)
                ),
                vol.Optional(CONF_PURGE_INTERVAL, default=1): vol.All(
                    vol.Coerce(int), vol.Range(min=0)
                ),
                vol.Optional(CONF_DB_URL): cv.string,
                vol.Optional(CONF_COMMIT_INTERVAL, default=1): vol.All(
                    vol.Coerce(int), vol.Range(min=0)
                ),
                vol.Optional(
                    CONF_DB_MAX_RETRIES, default=DEFAULT_DB_MAX_RETRIES
                ): cv.positive_int,
                vol.Optional(
                    CONF_DB_RETRY_WAIT, default=DEFAULT_DB_RETRY_WAIT
                ): cv.positive_int,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def run_information(hass, point_in_time: Optional[datetime] = None):
    """Return information about current run.

    There is also the run that covers point_in_time.
    """
    ins = hass.data[DATA_INSTANCE]

    recorder_runs = RecorderRuns
    if point_in_time is None or point_in_time > ins.recording_start:
        return ins.run_info

    with session_scope(hass=hass) as session:
        res = (
            session.query(recorder_runs)
            .filter(
                (recorder_runs.start < point_in_time)
                & (recorder_runs.end > point_in_time)
            )
            .first()
        )
        if res:
            session.expunge(res)
        return res


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the recorder."""
    conf = config[DOMAIN]
    keep_days = conf.get(CONF_PURGE_KEEP_DAYS)
    purge_interval = conf.get(CONF_PURGE_INTERVAL)
    commit_interval = conf[CONF_COMMIT_INTERVAL]
    db_max_retries = conf[CONF_DB_MAX_RETRIES]
    db_retry_wait = conf[CONF_DB_RETRY_WAIT]

    db_url = conf.get(CONF_DB_URL, None)
    if not db_url:
        db_url = DEFAULT_URL.format(hass_config_path=hass.config.path(DEFAULT_DB_FILE))

    include = conf.get(CONF_INCLUDE, {})
    exclude = conf.get(CONF_EXCLUDE, {})
    instance = hass.data[DATA_INSTANCE] = Recorder(
        hass=hass,
        keep_days=keep_days,
        purge_interval=purge_interval,
        commit_interval=commit_interval,
        uri=db_url,
        db_max_retries=db_max_retries,
        db_retry_wait=db_retry_wait,
        include=include,
        exclude=exclude,
    )
    instance.async_initialize()
    instance.start()

    async def async_handle_purge_service(service):
        """Handle calls to the purge service."""
        instance.do_adhoc_purge(**service.data)

    hass.services.async_register(
        DOMAIN, SERVICE_PURGE, async_handle_purge_service, schema=SERVICE_PURGE_SCHEMA
    )

    return await instance.async_db_ready


PurgeTask = namedtuple("PurgeTask", ["keep_days", "repack"])


class Recorder(threading.Thread):
    """A threaded recorder class."""

    def __init__(
        self,
        hass: HomeAssistant,
        keep_days: int,
        purge_interval: int,
        commit_interval: int,
        uri: str,
        db_max_retries: int,
        db_retry_wait: int,
        include: Dict,
        exclude: Dict,
    ) -> None:
        """Initialize the recorder."""
        threading.Thread.__init__(self, name="Recorder")

        self.hass = hass
        self.keep_days = keep_days
        self.purge_interval = purge_interval
        self.commit_interval = commit_interval
        self.queue: Any = queue.Queue()
        self.recording_start = dt_util.utcnow()
        self.db_url = uri
        self.db_max_retries = db_max_retries
        self.db_retry_wait = db_retry_wait
        self.async_db_ready = asyncio.Future()
        self.engine: Any = None
        self.run_info: Any = None

        self.entity_filter = generate_filter(
            include.get(CONF_DOMAINS, []),
            include.get(CONF_ENTITIES, []),
            exclude.get(CONF_DOMAINS, []),
            exclude.get(CONF_ENTITIES, []),
        )
        self.exclude_t = exclude.get(CONF_EVENT_TYPES, [])

        self._timechanges_seen = 0
        self.event_session = None
        self.get_session = None

    @callback
    def async_initialize(self):
        """Initialize the recorder."""
        self.hass.bus.async_listen(MATCH_ALL, self.event_listener)

    def do_adhoc_purge(self, **kwargs):
        """Trigger an adhoc purge retaining keep_days worth of data."""
        keep_days = kwargs.get(ATTR_KEEP_DAYS, self.keep_days)
        repack = kwargs.get(ATTR_REPACK)

        self.queue.put(PurgeTask(keep_days, repack))

    def run(self):
        """Start processing events to save."""
        tries = 1
        connected = False

        while not connected and tries <= self.db_max_retries:
            if tries != 1:
                time.sleep(self.db_retry_wait)
            try:
                self._setup_connection()
                migration.migrate_schema(self)
                self._setup_run()
                connected = True
                _LOGGER.debug("Connected to recorder database")
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.error(
                    "Error during connection setup: %s (retrying in %s seconds)",
                    err,
                    self.db_retry_wait,
                )
                tries += 1

        if not connected:

            @callback
            def connection_failed():
                """Connect failed tasks."""
                self.async_db_ready.set_result(False)
                persistent_notification.async_create(
                    self.hass,
                    "The recorder could not start, please check the log",
                    "Recorder",
                )

            self.hass.add_job(connection_failed)
            return

        shutdown_task = object()
        hass_started = concurrent.futures.Future()

        @callback
        def register():
            """Post connection initialize."""
            self.async_db_ready.set_result(True)

            def shutdown(event):
                """Shut down the Recorder."""
                if not hass_started.done():
                    hass_started.set_result(shutdown_task)
                self.queue.put(None)
                self.join()

            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)

            if self.hass.state == CoreState.running:
                hass_started.set_result(None)
            else:

                @callback
                def notify_hass_started(event):
                    """Notify that hass has started."""
                    hass_started.set_result(None)

                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_START, notify_hass_started
                )

        self.hass.add_job(register)
        result = hass_started.result()

        # If shutdown happened before Home Assistant finished starting
        if result is shutdown_task:
            return

        # Start periodic purge
        if self.keep_days and self.purge_interval:

            @callback
            def async_purge(now):
                """Trigger the purge and schedule the next run."""
                self.queue.put(PurgeTask(self.keep_days, repack=False))
                self.hass.helpers.event.async_track_point_in_time(
                    async_purge, now + timedelta(days=self.purge_interval)
                )

            earliest = dt_util.utcnow() + timedelta(minutes=30)
            run = latest = dt_util.utcnow() + timedelta(days=self.purge_interval)
            with session_scope(session=self.get_session()) as session:
                event = session.query(Events).first()
                if event is not None:
                    session.expunge(event)
                    run = dt_util.as_utc(event.time_fired) + timedelta(
                        days=self.keep_days + self.purge_interval
                    )
            run = min(latest, max(run, earliest))

            self.hass.helpers.event.track_point_in_time(async_purge, run)

        self.event_session = self.get_session()
        # Use a session for the event read loop
        # with a commit every time the event time
        # has changed.  This reduces the disk io.
        while True:
            event = self.queue.get()

            if event is None:
                self._close_run()
                self._close_connection()
                self.queue.task_done()
                return
            if isinstance(event, PurgeTask):
                purge.purge_old_data(self, event.keep_days, event.repack)
                self.queue.task_done()
                continue
            if event.event_type == EVENT_TIME_CHANGED:
                self.queue.task_done()
                if self.commit_interval:
                    self._timechanges_seen += 1
                    if self.commit_interval >= self._timechanges_seen:
                        self._timechanges_seen = 0
                        self._commit_event_session_or_retry()
                continue
            if event.event_type in self.exclude_t:
                self.queue.task_done()
                continue

            entity_id = event.data.get(ATTR_ENTITY_ID)
            if entity_id is not None:
                if not self.entity_filter(entity_id):
                    self.queue.task_done()
                    continue

            try:
                dbevent = Events.from_event(event)
                self.event_session.add(dbevent)
                self.event_session.flush()
            except (TypeError, ValueError):
                _LOGGER.warning("Event is not JSON serializable: %s", event)

            if dbevent and event.event_type == EVENT_STATE_CHANGED:
                try:
                    dbstate = States.from_event(event)
                    dbstate.event_id = dbevent.event_id
                    self.event_session.add(dbstate)
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "State is not JSON serializable: %s",
                        event.data.get("new_state"),
                    )

            # If they do not have a commit interval
            # than we commit right away
            if not self.commit_interval:
                self._commit_event_session_or_retry()

            self.queue.task_done()

    def _commit_event_session_or_retry(self):
        tries = 1
        while tries <= self.db_max_retries:
            if tries != 1:
                time.sleep(self.db_retry_wait)

            try:
                self._commit_event_session()
                return

            except exc.OperationalError as err:
                _LOGGER.error(
                    "Error in database connectivity: %s. " "(retrying in %s seconds)",
                    err,
                    self.db_retry_wait,
                )
                tries += 1

            except exc.SQLAlchemyError:
                _LOGGER.exception("Error saving events")
                return

        _LOGGER.error(
            "Error in database update. Could not save " "after %d tries. Giving up",
            tries,
        )
        try:
            self.event_session.close()
        except exc.SQLAlchemyError:
            _LOGGER.exception("Failed to close event session.")

        self.event_session = self.get_session()

    def _commit_event_session(self):
        try:
            self.event_session.commit()
        except Exception as err:
            _LOGGER.error("Error executing query: %s", err)
            self.event_session.rollback()
            raise

    @callback
    def event_listener(self, event):
        """Listen for new events and put them in the process queue."""
        self.queue.put(event)

    def block_till_done(self):
        """Block till all events processed."""
        self.queue.join()

    def _setup_connection(self):
        """Ensure database is ready to fly."""
        kwargs = {}

        # pylint: disable=unused-variable
        @listens_for(Engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            """Set sqlite's WAL mode."""
            if isinstance(dbapi_connection, Connection):
                old_isolation = dbapi_connection.isolation_level
                dbapi_connection.isolation_level = None
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()
                dbapi_connection.isolation_level = old_isolation

        if self.db_url == "sqlite://" or ":memory:" in self.db_url:
            kwargs["connect_args"] = {"check_same_thread": False}
            kwargs["poolclass"] = StaticPool
            kwargs["pool_reset_on_return"] = None
        else:
            kwargs["echo"] = False

        if self.engine is not None:
            self.engine.dispose()

        self.engine = create_engine(self.db_url, **kwargs)
        Base.metadata.create_all(self.engine)
        self.get_session = scoped_session(sessionmaker(bind=self.engine))

    def _close_connection(self):
        """Close the connection."""
        self.engine.dispose()
        self.engine = None
        self.get_session = None

    def _setup_run(self):
        """Log the start of the current run."""
        with session_scope(session=self.get_session()) as session:
            for run in session.query(RecorderRuns).filter_by(end=None):
                run.closed_incorrect = True
                run.end = self.recording_start
                _LOGGER.warning(
                    "Ended unfinished session (id=%s from %s)", run.run_id, run.start
                )
                session.add(run)

            self.run_info = RecorderRuns(
                start=self.recording_start, created=dt_util.utcnow()
            )
            session.add(self.run_info)
            session.flush()
            session.expunge(self.run_info)

    def _close_run(self):
        """Save end time for current run."""
        if self.event_session is not None:
            self.run_info.end = dt_util.utcnow()
            self.event_session.add(self.run_info)
            self._commit_event_session_or_retry()
            self.event_session.close()

        self.run_info = None
