"""The judge worker: claims submissions from Postgres and grades them.

Replaces the HTTP handshake in main.py/que.py. Each lane is a thread owning a
fixed isolate box id, looping: claim -> sync testcases -> grade -> persist.

Correctness does not depend on NOTIFY. The listener only shortens latency; a
lane that is never notified still claims on its poll interval.
"""

import logging
import signal
import threading

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


def lane_loop(lane: int):
    log.info("lane %d ready", lane)
    while not stop.is_set():
        with db.pool.connection() as conn:
            job = jobs.claim(conn)

        if job is None:
            wakeup.wait(settings.POLL_INTERVAL)
            wakeup.clear()
            continue

        log.info("lane %d claimed submission %s (attempt %d)", lane, job.id, job.attempts)
        try:
            process(lane, job)
        except TestcaseError as error:
            log.warning("submission %s: %s", job.id, error)
            with db.pool.connection() as conn:
                jobs.fail(conn, job.id, "JE", str(error))
        except Exception as error:
            # Unexpected: could be transient (a restart mid-grade, a wedged
            # isolate). Retry until attempts run out, then give up rather than
            # letting one submission occupy the queue forever.
            log.exception("submission %s failed on attempt %d", job.id, job.attempts)
            with db.pool.connection() as conn:
                if job.attempts >= settings.MAX_ATTEMPTS:
                    jobs.fail(conn, job.id, "SE", str(error))
                else:
                    jobs.release(conn, job.id)


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
        stop.wait(1)

    for lane in lanes:
        lane.join(timeout=10)
    db.close_pool()


if __name__ == "__main__":
    main()
