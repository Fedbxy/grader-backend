#!/usr/bin/env python3
"""One-time migration: push testcase directories from the judge volume into MinIO
and record Problem.testcaseVersion.

See DESIGN.md section 3 (testcase distribution) and section 7 step 2.

Only problems still marked 'legacy' are migrated — the same rule the judge uses to
decide where to read testcases from. Anything with a real version is already in
storage (uploaded through the admin UI, or migrated by an earlier run) and is never
re-pushed: its folder may still exist on the volume, but it holds the testcases from
before the switch-over, and pushing it would overwrite the newer upload.

Dry run by default; pass --apply to upload and write to the database. Safe to re-run:
a migrated problem gets a real version, so the next run skips it. Avoid uploading
testcases through the UI while --apply is running.

Environment: DATABASE_URL, S3_ENDPOINT, S3_PORT, S3_ACCESS_KEY, S3_SECRET_KEY,
S3_BUCKET_NAME, and optionally TESTCASE_ROOT (default testcases, the same as the
worker: /app/testcases inside the judge container).
"""

import hashlib
import io
import os
import re
import sys
import zipfile
from pathlib import Path

import psycopg
from minio import Minio
from minio.error import S3Error

TESTCASE_ROOT = Path(os.environ.get("TESTCASE_ROOT", "testcases"))

# The marker the switch-over migration set on every problem that existed then.
LEGACY = "legacy"

# __MACOSX is an artifact of zips built on macOS. Everything else is preserved
# as uploaded, including helper files such as transform.py, since only the
# author knows whether they matter.
SKIP_NAMES = {"__MACOSX"}

# A worker built before the cache moved to .cache/ extracted archives into
# {problem_id}/{sha256}/ directly beside the legacy files. Excluding those, and
# dot-entries (.ready, .DS_Store), keeps this script idempotent on a volume the
# worker has already run against.
VERSION_DIR = re.compile(r"^[0-9a-f]{64}$")


def _is_legacy_content(relative: Path) -> bool:
    parts = relative.parts
    return not any(
        part in SKIP_NAMES or part.startswith(".") or VERSION_DIR.match(part)
        for part in parts
    )


def build_archive(problem_dir: Path):
    """Deterministic zip of a problem directory: sorted members, stable mtimes.

    Determinism matters because the sha256 is the cache-invalidation token. The
    same directory must always produce the same version, or every run would
    invalidate every judge's cache.
    """
    members = sorted(
        p for p in problem_dir.rglob("*")
        if p.is_file() and _is_legacy_content(p.relative_to(problem_dir))
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            archive.write(path, path.relative_to(problem_dir).as_posix())
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest(), len(members)


def validate(problem_dir: Path, expected_cases: int):
    """Every case the problem claims must exist as an .in/.sol pair."""
    faults = []
    for n in range(1, expected_cases + 1):
        for suffix in ("in", "sol"):
            if not (problem_dir / f"{n}.{suffix}").exists():
                faults.append(f"{n}.{suffix} missing")
    beyond = sorted(
        int(p.stem) for p in problem_dir.glob("*.in")
        if p.stem.isdigit() and int(p.stem) > expected_cases
    )
    if beyond:
        faults.append(f"cases beyond testcases={expected_cases}: {beyond}")
    return faults


def main() -> int:
    apply = "--apply" in sys.argv
    bucket = os.environ["S3_BUCKET_NAME"]

    client = Minio(
        f"{os.environ['S3_ENDPOINT']}:{os.environ['S3_PORT']}",
        access_key=os.environ["S3_ACCESS_KEY"],
        secret_key=os.environ["S3_SECRET_KEY"],
        secure=os.environ.get("S3_USE_SSL") == "true",
    )

    conn = psycopg.connect(os.environ["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute('SELECT id, title, testcases, "testcaseVersion" FROM problems ORDER BY id')
        problems = cur.fetchall()

    on_disk = {int(p.name) for p in TESTCASE_ROOT.iterdir() if p.is_dir() and p.name.isdigit()}
    known = {row[0] for row in problems}

    print(f"{'APPLY' if apply else 'DRY RUN'} — root={TESTCASE_ROOT} bucket={bucket}\n")

    migrated = in_storage = no_testcases = failed = 0

    for problem_id, title, testcases, current_version in problems:
        label = f"problem {problem_id} ({title})"
        key = f"problem/{problem_id}/testcase.zip"

        if current_version is None:
            # Created after the switch-over and never given testcases.
            print(f"  NONE {label}: no testcases uploaded — nothing to migrate")
            no_testcases += 1
            continue

        if current_version != LEGACY:
            # Already in storage. Confirm the archive really is there, since the
            # judge fails every submission for a version whose object is missing.
            try:
                client.stat_object(bucket, key)
            except S3Error as error:
                if error.code != "NoSuchKey":
                    raise
                print(f"  FAIL {label}: version {current_version[:12]} recorded but {key} "
                      "is missing — re-upload its testcases")
                failed += 1
                continue
            print(f"  SKIP {label}: already in storage ({current_version[:12]})")
            in_storage += 1
            continue

        directory = TESTCASE_ROOT / str(problem_id)
        if not directory.is_dir():
            print(f"  FAIL {label}: marked legacy but there is no testcase directory — "
                  "the judge reports JE for it")
            failed += 1
            continue

        faults = validate(directory, testcases)
        if faults:
            print(f"  FAIL {label}: " + "; ".join(faults))
            failed += 1
            continue

        data, version, member_count = build_archive(directory)
        size_mb = len(data) / 1024 / 1024
        print(f"  {'MIGRATE' if apply else 'WOULD MIGRATE'} {label}: "
              f"{testcases} cases, {member_count} files, {size_mb:.1f} MB, {version[:12]}")

        if apply:
            # Re-check immediately before writing, so a testcase upload through
            # the UI since the run started is not overwritten.
            with conn.cursor() as cur:
                cur.execute('SELECT "testcaseVersion" FROM problems WHERE id = %s', (problem_id,))
                if cur.fetchone()[0] != LEGACY:
                    print(f"    skipped: its testcases were uploaded while this was running")
                    in_storage += 1
                    continue

            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
            client.put_object(bucket, key, io.BytesIO(data), len(data), content_type="application/zip")

            # Only replace the legacy marker. Zero rows means an upload landed in
            # the moment between the check above and here.
            with conn.cursor() as cur:
                cur.execute(
                    'UPDATE problems SET "testcaseVersion" = %s, "updatedAt" = now() '
                    'WHERE id = %s AND "testcaseVersion" = %s',
                    (version, problem_id, LEGACY),
                )
                conflicted = cur.rowcount == 0
            conn.commit()
            if conflicted:
                print(f"    CONFLICT: testcases were uploaded during the migration and the "
                      f"archive just written may be stale — re-upload them for this problem")
                failed += 1
                continue
        migrated += 1

    orphans = sorted(on_disk - known)
    if orphans:
        print(f"\n  {len(orphans)} orphaned director{'y' if len(orphans) == 1 else 'ies'} "
              f"(no matching problem row): {orphans}")
        print("  Not touched. Safe to delete once testcases are served from MinIO,")
        print("  since the volume is then only a cache.")

    print(f"\n{migrated} {'migrated' if apply else 'would be migrated'}, "
          f"{in_storage} already in storage, {no_testcases} without testcases, {failed} failed")
    conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
