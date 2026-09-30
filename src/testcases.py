"""Testcase distribution: MinIO is the master copy, local disk is a cache.

The DB holds Problem.testcaseVersion; the worker compares it against what it has
extracted and refetches only on a mismatch (DESIGN.md section 3).

Extracted content lives at {root}/.cache/{problem_id}/{version}/ rather than
being swapped in place. A version-stamped path means a sync never mutates a
directory another lane is reading mid-judge.
"""

import fcntl
import io
import logging
import os
import shutil
import zipfile
from pathlib import Path

from minio import Minio
from minio.error import S3Error

from config import settings

log = logging.getLogger(__name__)


class TestcaseError(Exception):
    """Testcases are unusable for this problem. Retrying will not help."""


_client = None


def client():
    global _client
    if _client is None:
        _client = Minio(
            f"{settings.S3_ENDPOINT}:{settings.S3_PORT}",
            access_key=settings.S3_ACCESS_KEY,
            secret_key=settings.S3_SECRET_KEY,
            secure=settings.S3_USE_SSL,
        )
    return _client


def _safe_extract(data: bytes, dest: Path):
    """Extract, rejecting any member that escapes dest.

    zipfile.extractall() on an admin-supplied archive would let a member named
    ../../etc/... write anywhere the process can reach — and this process runs as
    root in a privileged container.
    """
    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            target = (dest / member.filename).resolve()
            if target != resolved_dest and resolved_dest not in target.parents:
                raise TestcaseError(f"Unsafe path in testcase archive: {member.filename}")
        archive.extractall(dest)


def ensure(problem_id: int, version: str | None) -> Path:
    """Return a directory holding this problem's testcases at `version`.

    Downloads and extracts only when the local cache does not already have it.
    """
    if not version:
        raise TestcaseError("No testcases found")

    # A namespace of its own inside the volume. The volume also holds the
    # legacy flat layout ({problem_id}/1.in) that migrate_testcases.py reads;
    # caching into those directories made the migration sweep cached copies
    # into its archives, and let pruning delete legacy subdirectories.
    root = Path(settings.TESTCASE_ROOT) / ".cache"
    target = root / str(problem_id) / version
    if (target / ".ready").exists():
        return target

    lock_dir = root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)

    # Serialise lanes that want the same problem, so a 180 MB archive is not
    # downloaded twice concurrently.
    with open(lock_dir / f"{problem_id}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        if (target / ".ready").exists():
            return target

        key = f"problem/{problem_id}/testcase.zip"
        log.info("syncing testcases for problem %s (%s)", problem_id, version[:12])
        try:
            response = client().get_object(settings.S3_BUCKET_NAME, key)
            try:
                data = response.read()
            finally:
                response.close()
                response.release_conn()
        except S3Error as error:
            if error.code == "NoSuchKey":
                # The database names a version that storage does not have.
                # Permanent until someone re-uploads, so fail the submission.
                raise TestcaseError("Testcase archive is missing from storage") from error
            raise
        # Anything else (MinIO restarting, a network blip) propagates as an
        # ordinary error, which the worker retries with backoff. Treating it as
        # a TestcaseError would permanently fail every submission that arrived
        # during a brief outage.

        staging = root / ".tmp" / f"{problem_id}.{version}"
        if staging.exists():
            shutil.rmtree(staging)
        _safe_extract(data, staging)

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        os.rename(staging, target)

        # Written last: its presence is what marks the directory complete, so a
        # crash mid-extract leaves a directory that is retried rather than trusted.
        (target / ".ready").touch()

        _prune_old_versions(target.parent, version)
        log.info("testcases ready for problem %s (%.1f MB)", problem_id, len(data) / 1024 / 1024)
        return target


def _prune_old_versions(problem_dir: Path, current: str):
    """Drop superseded versions. Best effort — the cache is disposable."""
    for entry in problem_dir.iterdir():
        if entry.is_dir() and entry.name != current:
            try:
                shutil.rmtree(entry)
            except OSError:
                log.warning("could not prune stale testcase version %s", entry)
