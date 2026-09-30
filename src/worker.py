"""The judge worker: claims submissions from Postgres and grades them.

Replaces the HTTP handshake in main.py/que.py. Each lane is a thread owning a
fixed isolate box id, looping: claim -> sync testcases -> grade -> persist.

Correctness does not depend on NOTIFY. The listener only shortens latency; a
lane that is never notified still claims on its poll interval.
"""

import logging
import signal
import sys
import threading

import psycopg

import db
import jobs
import testcases as testcase_cache
from config import settings
from isolate import initIsolate, cleanupIsolate
from judge import evaluate
from testcases import TestcaseError
from utils import createFile

log = logging.getLogger("worker")

wakeup = threading.Event()
stop = threading.Event()


def process(lane: int, job: jobs.Job):
    """Grade one claimed submission and persist the outcome.

    The lane is released by the caller's finally, so no failure path here can
    take a lane out of service.
    """
    def progress(status):
        with db.pool.connection() as conn:
            jobs.progress(conn, job.id, status)

    # Not "In queue": by this point the submission has been claimed and is no
    # longer waiting. Fetching a cold 87MB archive takes seconds, so it gets its
    # own status rather than leaving a stale queue message on screen.
    progress("Preparing testcases")

    # Raises TestcaseError when the problem has no usable testcases, which no
    # amount of retrying fixes.
    testcase_dir = testcase_cache.ensure(job.problem_id, job.testcase_version)

    # Box id is the lane, not the submission id: isolate boxes are 0-999 and
    # submission ids grow without bound.
    isolate_path = initIsolate(lane)
    try:
        createFile(isolate_path, lane, job.language, job.code)
        outcome = evaluate(
            isolate_path, lane, str(testcase_dir),
            job.time_limit, job.memory_limit, job.testcases, job.language,
            onProgress=progress,
        )
    finally:
        cleanupIsolate(lane)

    with db.pool.connection() as conn:
        if outcome.errorCode == "CE":
            # A compile error is a legitimate verdict, not a judge failure.
            jobs.finish_error(conn, job.id, "CE", outcome.error)
        elif outcome.errorCode:
            jobs.fail(conn, job.id, outcome.errorCode, outcome.error)
        else:
            jobs.finish(conn, job, outcome.score, outcome.result)

    log.info("submission %s judged (score %s)", job.id, outcome.score)


def handle(lane: int, job: jobs.Job):
    """Grade a claimed job, turning judge-side failures into a recorded outcome.

    Database errors are re-raised rather than recorded: they say nothing about
    the submission, so they must not count against it. lane_loop hands the job
    back once the database is reachable again.
    """
    try:
        process(lane, job)
    except psycopg.Error:
        raise
    except TestcaseError as error:
        log.warning("submission %s: %s", job.id, error)
        with db.pool.connection() as conn:
            jobs.fail(conn, job.id, "JE", str(error))
    except Exception as error:
        # Unexpected: could be transient (MinIO briefly down, a wedged isolate).
        # Retry until attempts run out, then give up rather than letting one
        # submission occupy the queue forever.
        log.exception("submission %s failed on attempt %d", job.id, job.attempts)
        with db.pool.connection() as conn:
            if job.attempts >= settings.MAX_ATTEMPTS:
                jobs.fail(conn, job.id, "SE", str(error))
                return
            jobs.release(conn, job.id)
        # Back off before the lane claims again — the released job is still
        # first in line, so without a pause every retry lands within a second
        # and a brief outage exhausts all of them.
        stop.wait(settings.POLL_INTERVAL * job.attempts)


def lane_loop(lane: int):
    """Claim and grade until stopped. Nothing may end this loop but stop.

    A lane thread that dies takes its capacity with it while the container stays
    up and looks healthy — at LANES=1 that is all grading, silently. So every
    iteration is guarded, and a database outage is waited out rather than fatal.
    """
    log.info("lane %d ready", lane)

    # A job this lane claimed but could not hand back because the database was
    # unreachable. Released first thing once the database returns; otherwise it
    # would sit in 'judging' until the next restart.
    stranded = None

    while not stop.is_set():
        try:
            if stranded is not None:
                with db.pool.connection() as conn:
                    jobs.release(conn, stranded.id)
                log.info("lane %d handed back submission %s after the database recovered", lane, stranded.id)
                stranded = None

            with db.pool.connection() as conn:
                job = jobs.claim(conn)

            if job is None:
                wakeup.wait(settings.POLL_INTERVAL)
                wakeup.clear()
                continue

            log.info("lane %d claimed submission %s (attempt %d)", lane, job.id, job.attempts)
            stranded = job
            handle(lane, job)
            stranded = None
        except Exception:
            log.exception("lane %d: database unavailable, retrying", lane)
            stop.wait(settings.POLL_INTERVAL)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if not settings.DATABASE_URL:
        raise SystemExit("DATABASE_URL is not set")

    def shutdown(*_):
        log.info("shutting down; in-flight submissions will be requeued on next start")
        stop.set()
        wakeup.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    db.open_pool()

    with db.pool.connection() as conn:
        jobs.requeue_abandoned(conn)

    listener = threading.Thread(
        target=db.listen, args=(wakeup.set, stop), daemon=True, name="listener")
    listener.start()

    lanes = [
        threading.Thread(target=lane_loop, args=(lane,), daemon=True, name=f"lane-{lane}")
        for lane in range(settings.LANES)
    ]
    for lane in lanes:
        lane.start()

    log.info("worker %s up with %d lane(s)", settings.WORKER_ID, settings.LANES)

    while not stop.is_set():
        # Lanes are built not to die, so a dead one means a bug nothing caught.
        # Exit rather than run on with less capacity than configured: the
        # container restarts, and startup requeue recovers the in-flight work.
        if not all(lane.is_alive() for lane in lanes):
            log.error("a lane stopped unexpectedly; exiting so the container restarts")
            sys.exit(1)
        stop.wait(1)

    for lane in lanes:
        lane.join(timeout=10)
    db.close_pool()


if __name__ == "__main__":
    main()
