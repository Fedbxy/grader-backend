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
    user_id: int
    attempts: int
    time_limit: int
    memory_limit: int
    testcases: int
    testcase_version: str | None


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
            RETURNING s.id, s.code, s.language, s."problemId", s."userId", s.attempts
            """
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return None

        submission_id, code, language, problem_id, user_id, attempts = row
        cur.execute(
            'SELECT "timeLimit", "memoryLimit", testcases, "testcaseVersion" '
            "FROM problems WHERE id = %s",
            (problem_id,),
        )
        problem = cur.fetchone()
    conn.commit()

    if problem is None:
        # The problem was deleted while the submission was queued.
        return Job(submission_id, code, language, problem_id, user_id, attempts,
                   0, 0, 0, None)

    time_limit, memory_limit, testcases, testcase_version = problem
    return Job(submission_id, code, language, problem_id, user_id, attempts,
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
    """Persist a graded result and the user's problem state in one transaction.

    This is the logic that used to live in actions/judge.ts. Doing both writes
    atomically is the point: a crash between them previously lost the grade.
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

        # Correct on both paths now that score is a passed-case count: full
        # marks means every case passed.
        is_accepted = score == job.testcases

        # The WHERE clause is the "only move forward" guard from the old code:
        # an older submission finishing late must not overwrite a newer one.
        cur.execute(
            """
            INSERT INTO user_problems ("userId", "problemId", "submissionId", "isAccepted")
            VALUES (%s, %s, %s, %s)
            ON CONFLICT ("userId", "problemId") DO UPDATE
            SET "submissionId" = EXCLUDED."submissionId",
                "isAccepted"   = EXCLUDED."isAccepted"
            WHERE user_problems."submissionId" <= EXCLUDED."submissionId"
            """,
            (job.user_id, job.problem_id, job.id, is_accepted),
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
    """Put a submission back in the queue after an unexpected failure."""
    with conn.cursor() as cur:
        cur.execute(
            'UPDATE submissions SET "judgeStatus" = \'pending\', "updatedAt" = now() '
            "WHERE id = %s",
            (submission_id,),
        )
    conn.commit()
