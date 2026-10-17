"""Queue operations against Postgres (DESIGN.md section 2).

The queue is the submissions table. Claiming is a single atomic statement using
FOR UPDATE SKIP LOCKED, which is what makes concurrent lanes safe without any
external lock.
"""

import logging
from dataclasses import dataclass

from psycopg.types.json import Json

from config import settings

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: int
    code: str
    language: str
    problem_id: int
    attempts: int
    time_limit: int
    memory_limit: int
    testcases: int
    testcase_version: str | None


@dataclass
class RunJob:
    """A claimed Run (DESIGN.md "Runs (#67)").

    Deliberately not a Job: a run has no testcases, so a submission code path
    handed one would fail on a missing field instead of grading it wrongly.
    """
    id: int
    code: str
    language: str
    input: str
    problem_id: int
    attempts: int
    time_limit: int
    memory_limit: int


def requeue_abandoned(conn):
    """Reclaim work stranded by a crash or restart (DESIGN.md section 2).

    With a single worker this is sound at startup: any row still marked
    'judging' must have been claimed by a previous incarnation of this process,
    because nothing else claims. Rows that have already burned their retries are
    failed instead, so a submission that kills the worker cannot loop forever.

    A second judge host makes this unsafe — it would steal the other host's
    in-flight work — and is the point at which heartbeatAt and a reaper are
    needed instead.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE submissions SET "judgeStatus" = 'failed', status = NULL, score = 0,
                   "errorCode" = 'JE', error = 'Judge failed repeatedly',
                   "updatedAt" = now()
            WHERE "judgeStatus" = 'judging' AND attempts >= %s
            """,
            (settings.MAX_ATTEMPTS,),
        )
        failed = cur.rowcount
        cur.execute(
            """
            UPDATE submissions SET "judgeStatus" = 'pending', "updatedAt" = now()
            WHERE "judgeStatus" = 'judging'
            """
        )
        requeued = cur.rowcount

        # Runs follow the same rules as submissions.
        cur.execute(
            """
            UPDATE runs SET "judgeStatus" = 'failed', status = NULL,
                   "errorCode" = 'JE', error = 'Judge failed repeatedly',
                   "updatedAt" = now()
            WHERE "judgeStatus" = 'judging' AND attempts >= %s
            """,
            (settings.MAX_ATTEMPTS,),
        )
        failed += cur.rowcount
        cur.execute(
            """
            UPDATE runs SET "judgeStatus" = 'pending', "updatedAt" = now()
            WHERE "judgeStatus" = 'judging'
            """
        )
        requeued += cur.rowcount
    conn.commit()
    if requeued or failed:
        log.info("startup: requeued %d abandoned, failed %d exhausted", requeued, failed)
    return requeued, failed


def claim(conn):
    """Take the next submission, or None when the queue is empty.

    ORDER BY priority, id keeps rejudges (priority 1) behind live submissions.
    Without it, rejudged rows — which have small ids — would sort ahead of
    everything new.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE submissions s
            SET "judgeStatus" = 'judging', attempts = attempts + 1, "updatedAt" = now()
            WHERE s.id = (
                SELECT id FROM submissions
                WHERE "judgeStatus" = 'pending'
                ORDER BY priority, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING s.id, s.code, s.language, s."problemId", s.attempts
            """
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return None

        submission_id, code, language, problem_id, attempts = row
        cur.execute(
            'SELECT "timeLimit", "memoryLimit", testcases, "testcaseVersion" '
            "FROM problems WHERE id = %s",
            (problem_id,),
        )
        problem = cur.fetchone()
    conn.commit()

    if problem is None:
        # The problem was deleted while the submission was queued.
        return Job(submission_id, code, language, problem_id, attempts,
                   0, 0, 0, None)

    time_limit, memory_limit, testcases, testcase_version = problem
    return Job(submission_id, code, language, problem_id, attempts,
               time_limit, memory_limit, testcases, testcase_version)


def progress(conn, submission_id, status):
    """Write the human-readable progress string the UI renders."""
    with conn.cursor() as cur:
        cur.execute(
            'UPDATE submissions SET status = %s, "updatedAt" = now() WHERE id = %s',
            (status, submission_id),
        )
    conn.commit()


def finish(conn, job, score, result):
    """Persist a graded result.

    Solved status and each user's latest submission are derived from
    submissions by the frontend, so nothing else needs writing here.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE submissions
            SET score = %s, result = %s, status = NULL, "errorCode" = NULL, error = NULL,
                "judgeStatus" = 'done', "updatedAt" = now()
            WHERE id = %s
            """,
            (score, Json(result), job.id),
        )
    conn.commit()


def _write_error(conn, submission_id, error_code, message, judge_status):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE submissions
            SET score = 0, result = '[]'::jsonb, status = NULL,
                "errorCode" = %s, error = %s,
                "judgeStatus" = %s, "updatedAt" = now()
            WHERE id = %s
            """,
            (error_code, message, judge_status, submission_id),
        )
    conn.commit()


def finish_error(conn, submission_id, error_code, message):
    """A verdict the submitter caused, such as a compile error.

    The submission was judged, so it leaves the queue as 'done'.
    """
    _write_error(conn, submission_id, error_code, message, "done")


def fail(conn, submission_id, error_code, message):
    """A judge-side failure: missing testcases, a bad subtask config, a crash.

    Marked 'failed' rather than 'done' so these are distinguishable from graded
    submissions when looking for trouble.
    """
    _write_error(conn, submission_id, error_code, message, "failed")


def release(conn, submission_id):
    """Put a submission back in the queue after an unexpected failure.

    Only a row still marked 'judging' is released. After a database outage the
    lane cannot know whether its last write landed; if it did, the row is
    already 'done' and must not be requeued.
    """
    with conn.cursor() as cur:
        cur.execute(
            'UPDATE submissions SET "judgeStatus" = \'pending\', "updatedAt" = now() '
            "WHERE id = %s AND \"judgeStatus\" = 'judging'",
            (submission_id,),
        )
    conn.commit()



def claim_run(conn):
    """Take the next Run, or None when there are none.

    Only called once claim() has found no submission, which is what puts runs
    behind everything else (effectively priority 2: live 0, rejudge 1, run 2).
    A run is someone waiting on a custom input, but a graded submission still
    outranks it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE runs r
            SET "judgeStatus" = 'judging', attempts = attempts + 1, "updatedAt" = now()
            WHERE r.id = (
                SELECT id FROM runs
                WHERE "judgeStatus" = 'pending'
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING r.id, r.code, r.language, r.input, r."problemId", r.attempts
            """
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return None

        run_id, code, language, input_text, problem_id, attempts = row
        cur.execute(
            'SELECT "timeLimit", "memoryLimit" FROM problems WHERE id = %s',
            (problem_id,),
        )
        problem = cur.fetchone()
    conn.commit()

    if problem is None:
        # The problem was deleted while the run was queued. Runs cascade with
        # their problem, so the row is gone and the writes below update nothing.
        return RunJob(run_id, code, language, input_text, problem_id, attempts, 0, 0)

    time_limit, memory_limit = problem
    return RunJob(run_id, code, language, input_text, problem_id, attempts,
                  time_limit, memory_limit)


# The website deletes runs older than an hour, so every write below may find
# its row gone. That updates zero rows, which is fine: nobody is waiting.

def run_progress(conn, run_id, status):
    """Write the progress string the UI renders for a run."""
    with conn.cursor() as cur:
        cur.execute(
            'UPDATE runs SET status = %s, "updatedAt" = now() WHERE id = %s',
            (status, run_id),
        )
    conn.commit()


def finish_run(conn, run_id, outcome):
    """Persist a run that executed. outcome is a judge.RunOutcome."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE runs
            SET verdict = %s, "exitCode" = %s, "exitSignal" = %s,
                stdout = %s, "stdoutTruncated" = %s,
                stderr = %s, "stderrTruncated" = %s,
                time = %s, memory = %s,
                status = NULL, "errorCode" = NULL, error = NULL,
                "judgeStatus" = 'done', "updatedAt" = now()
            WHERE id = %s
            """,
            (outcome.verdict, outcome.exitCode, outcome.exitSignal,
             outcome.stdout, outcome.stdoutTruncated,
             outcome.stderr, outcome.stderrTruncated,
             outcome.time, outcome.memory, run_id),
        )
    conn.commit()


def _write_run_error(conn, run_id, error_code, message, judge_status):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE runs
            SET status = NULL, "errorCode" = %s, error = %s,
                "judgeStatus" = %s, "updatedAt" = now()
            WHERE id = %s
            """,
            (error_code, message.replace("\x00", ""), judge_status, run_id),
        )
    conn.commit()


def finish_run_error(conn, run_id, error_code, message):
    """The user's own mistake, such as a compile error: the run is 'done'."""
    _write_run_error(conn, run_id, error_code, message, "done")


def fail_run(conn, run_id, error_code, message):
    """A judge-side failure, marked 'failed' like fail() does for submissions."""
    _write_run_error(conn, run_id, error_code, message, "failed")


def release_run(conn, run_id):
    """Put a run back in the queue; only if still 'judging', as release() does."""
    with conn.cursor() as cur:
        cur.execute(
            'UPDATE runs SET "judgeStatus" = \'pending\', "updatedAt" = now() '
            "WHERE id = %s AND \"judgeStatus\" = 'judging'",
            (run_id,),
        )
    conn.commit()
