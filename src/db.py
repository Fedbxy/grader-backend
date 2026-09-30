"""Postgres connection pool and the LISTEN connection.

The pool serves lane threads and short status writes. The listener holds its own
dedicated connection, because a connection blocked in LISTEN cannot be shared.
"""

import logging

import psycopg
from psycopg_pool import ConnectionPool

from config import settings

log = logging.getLogger(__name__)

# One connection per lane, plus one spare for progress writes that overlap a
# lane's own transaction.
pool = ConnectionPool(
    settings.DATABASE_URL,
    min_size=1,
    max_size=settings.LANES + 1,
    # Verify a connection before handing it out. Without this, a Postgres
    # restart leaves dead connections in the pool that fail on first use.
    check=ConnectionPool.check_connection,
    open=False,
)


def open_pool():
    pool.open()
    pool.wait(timeout=30)
    log.info("connected to postgres (pool size %d)", settings.LANES + 1)


def close_pool():
    pool.close()


def listen(on_notify, stop):
    """Block on NOTIFY, calling on_notify() for each one, until stop is set.

    Reconnects on failure. A dropped notification is not a correctness problem —
    the poll loop in worker.py picks up anything missed — so this only ever
    logs and retries.
    """
    while not stop.is_set():
        try:
            with psycopg.connect(settings.DATABASE_URL, autocommit=True) as conn:
                conn.execute(f"LISTEN {settings.NOTIFY_CHANNEL}")
                log.info("listening on %s", settings.NOTIFY_CHANNEL)
                while not stop.is_set():
                    # Short timeout so stop is noticed promptly. The connection
                    # is held across iterations; letting notifies() end the
                    # `with` block would reconnect every timeout.
                    for _ in conn.notifies(timeout=1.0):
                        on_notify()
        except Exception:
            if stop.is_set():
                return
            log.exception("listener connection lost; polling covers the gap, retrying")
            stop.wait(settings.POLL_INTERVAL)
