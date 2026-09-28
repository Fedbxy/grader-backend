# Grader Backend Redesign — Postgres as Queue

Status: design, not yet implemented
Branch: `db-queue`

## Goal

Replace the in-memory queue + HTTP handshake with a design where the judge worker's
only inputs are **Postgres** (submissions, problem metadata) and **MinIO** (testcase
archives). The backend exposes no HTTP surface; the frontend never calls it.

Non-goals: changing the grading algorithm, the `result` JSON shape, the UI, or the
verdict semantics. Those stay byte-compatible so the frontend renders unchanged.

## Current architecture

```
Next.js action ──POST /submit──> FastAPI ──> in-memory Queue ──> judge thread
      │                                                              │
      └──GET /submission/{id} every 500ms──> in-memory dict <────────┘
      │
      └──writes status/result to Postgres
      └──GET /submission/{id}/finished  (frees the dict entry)
```

Problems this causes:

1. **Queue is lost on restart.** Any submission in flight during a deploy is stranded
   in `Pending` forever, with no record that it was ever queued.
2. **The poller is a server action running `setInterval`.** `getSubmission()` in
   `actions/judge.ts` returns immediately while a timer keeps firing in the background.
   Nothing owns that timer, nothing retries it, and it dies with the serverless
   invocation — so results can be silently lost even when grading succeeded.
3. **The result write lives in the frontend.** The worker computes a verdict but the
   frontend is responsible for persisting it, so a frontend crash between "graded" and
   "written" loses the grade with no way to detect it.
4. **Rejudge-all is N sequential HTTP round-trips** driven by a loop in a server action.
5. **No backpressure or recovery.** A crashed judge leaves rows in `Pending` that
   nothing will ever pick up.

## Target architecture

```
Next.js action ──INSERT submission (judgeStatus=pending)──> Postgres
                                                              │ NOTIFY
                                                              ▼
                                          worker: LISTEN + 2s poll fallback
                                                              │
                                            claim (FOR UPDATE SKIP LOCKED)
                                                              │
                                          testcase sync (MinIO, version-gated)
                                                              │
                                                    judge in isolate lane
                                                              │
                                    UPDATE submission + UPSERT UserProblem (1 txn)
                                                              │
Next.js page ◄────────────────── reads submission row ────────┘
```

The frontend writes a row and reads a row. That's the entire protocol.

---

## 1. Schema changes

In the frontend repo's `prisma/schema.prisma`:

```prisma
enum JudgeStatus {
  pending
  judging
  done
  failed
}

model Submission {
  // ...existing fields unchanged...

  judgeStatus JudgeStatus @default(pending)
  priority    Int         @default(0)   // 0 = live submit, 1 = rejudge
  attempts    Int         @default(0)

  @@index([judgeStatus, priority, id])
}

model Problem {
  // ...existing fields unchanged...

  testcaseVersion String?   // sha256/etag of the uploaded zip
}
```

### Why `judgeStatus` is separate from `status`

`status` is the human-readable progress string the UI renders ("Compiling", "Running on
testcase 7") and is set to `NULL` when finished. It is presentation. `judgeStatus` is
queue state and is the only thing the claim query looks at. Overloading one for the
other is how queues develop unfixable race conditions — keep them separate.

### Migration must backfill

Adding the column with `@default(pending)` marks **every historical submission as
queued**. The same migration must contain:

```sql
UPDATE submissions SET "judgeStatus" = 'done';
```

Migrations are transactional, so there is no window where the worker could see them.
Still: run the migration *before* deploying the new backend.

### Index note

The ideal index is partial:

```sql
CREATE INDEX submissions_queue_idx ON submissions (priority, id)
  WHERE "judgeStatus" = 'pending';
```

It stays tiny regardless of table size, since only unjudged rows are indexed. Prisma
can't express partial indexes, and `prisma migrate` treats DB objects it doesn't know
about as drift and will try to drop them. Two options:

- **Declare the plain `@@index([judgeStatus, priority, id])` in the schema** (above) and
  let Prisma manage it. Slightly larger, zero drift friction. **Recommended.**
- Use the partial index via raw SQL and accept manual drift management.

---

## 2. Queue protocol

### Claim

One statement, atomic, correct for any number of lanes or judge hosts:

```sql
UPDATE submissions s
SET "judgeStatus" = 'judging',
    attempts      = attempts + 1,
    "updatedAt"   = now()
WHERE s.id = (
    SELECT id FROM submissions
    WHERE "judgeStatus" = 'pending'
    ORDER BY priority, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING s.id, s.code, s.language, s."problemId", s."userId";
```

- `FOR UPDATE SKIP LOCKED` is what makes concurrent claims safe with no external lock,
  no advisory locks, and no possibility of two lanes grabbing the same row.
- `ORDER BY priority, id` keeps rejudges (priority 1) from starving live submits.
- Returning zero rows means "queue empty" — the loop goes back to waiting.

### Wake-up: NOTIFY + poll fallback

```sql
CREATE OR REPLACE FUNCTION notify_submission_queued() RETURNS trigger AS $$
BEGIN
  IF NEW."judgeStatus" = 'pending' THEN
    PERFORM pg_notify('submission_queued', NEW.id::text);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER submission_queued
AFTER INSERT OR UPDATE OF "judgeStatus" ON submissions
FOR EACH ROW EXECUTE FUNCTION notify_submission_queued();
```

The worker holds one dedicated connection on `LISTEN submission_queued` and treats a
notification as nothing more than "wake up and try to claim" — the payload is a hint,
never a work assignment. Notifications are coalesced: drain the queue, then claim in a
loop until empty.

**The 2s poll is not redundant.** It is what recovers:

- rows orphaned by a worker that died mid-judge (via the reaper),
- notifications missed while the listener was reconnecting,
- anything that wrote `pending` through a path that bypassed the trigger.

Correctness rests on the poll; NOTIFY only makes it feel instant.

Prisma doesn't track triggers or functions, so this lives in a raw-SQL migration and
won't be flagged as drift.

### Recovering abandoned work

A worker that dies holding a claim leaves its row in `'judging'`, where the claim query
(which only looks at `'pending'`) will never see it again. This is not rare: the compose
file uses `restart: unless-stopped`, and every deploy that lands mid-grade produces one.
The student is left watching "Running on testcase 7" forever — the same class of silent
stranding this redesign exists to eliminate.

With a single worker, no timestamp is needed to detect it. At startup the worker knows
any `'judging'` row is abandoned, because it is the only thing that could have claimed
one and it has just booted:

```sql
-- once, on worker startup
UPDATE submissions
SET "judgeStatus" = 'pending', "updatedAt" = now()
WHERE "judgeStatus" = 'judging' AND attempts < 3;

-- rows that have already burned their retries
UPDATE submissions
SET "judgeStatus" = 'failed', status = NULL, score = 0,
    "errorCode" = 'JE', error = 'Judge failed repeatedly', "updatedAt" = now()
WHERE "judgeStatus" = 'judging' AND attempts >= 3;
```

Recovery is immediate rather than delayed by a reap interval, and it costs no columns
and no background loop.

**`attempts` is what keeps this from becoming an infinite crash loop.** A submission
that kills the worker otherwise repeats: boot → requeue → claim → die → container
restarts → requeue → claim → die. Nothing else in the queue is ever graded, and the row
cannot be cleared from the UI because every restart re-queues it. Three strikes sends it
to `'failed'` and the queue moves on.

That failure mode has a concrete route in the current code — see "Unbounded output read"
in §4.

**When a second judge host appears**, startup-requeue becomes unsafe: worker A's restart
would steal worker B's in-flight rows. That is the point to add `heartbeatAt` (refreshed
on each progress write) plus a periodic reaper keyed on staleness, and `claimedBy` to
identify which host stranded a row. Both are deferred until then — they buy nothing with
one worker.

### Finalize

Result write and `UserProblem` upsert happen in **one transaction** in the worker:

```
BEGIN
  UPDATE submissions SET score, result, status=NULL, errorCode, error,
                         "judgeStatus"='done', "updatedAt"=now()
  UPSERT user_problems (guard: only if submission.id >= existing submissionId)
COMMIT
```

Either both land or neither does. This is the logic currently in `actions/judge.ts`
lines 143–204, moved server-side and made atomic.

### Prisma `@updatedAt` is client-side

`@updatedAt` is implemented in the Prisma client, **not** as a database trigger. Raw SQL
from Python must set `"updatedAt" = now()` explicitly or the column silently stops
tracking reality. Every write above does this.

---

## 3. Testcase distribution

MinIO already runs in the stack (`minio-storage`, same network) and already stores
problem statements via `lib/minio.ts`. Testcases join it under the same bucket:

```
problem/{id}/statement.pdf   (existing)
problem/{id}/testcase.zip    (new)
```

The DB holds the pointer (`Problem.testcaseVersion`), MinIO holds the bytes, and the
judge's `testcases/` volume becomes a **cache**.

### Why not Postgres blobs

Real measurement from problem 1036: the archive in use is **87 MB** and barely
compresses — extracted files total within 9 KB of the packed size. The app's own ceiling
(`limits.testcase.size`) is **100 MB**. Blobs approaching that size inflate every backup
and every WAL segment, and force the whole buffer through Node's heap on upload. Object
storage is the correct tool and it's already running.

### Versioning does not duplicate storage

The object key is constant. Re-uploading overwrites in place; five uploads leave one
185 MB object, not five. `testcaseVersion` is a **cache-invalidation token, not a
version history** — its only job is letting a worker answer "is my copy stale?" without
downloading 185 MB to find out.

MinIO bucket versioning *would* retain old copies, but it's opt-in and the bucket is
created with a plain `makeBucket`. Leave it off unless "undo a bad upload" is worth 5×
storage.

### Sync algorithm

Before judging a submission for problem P:

1. Read `testcaseVersion` from the DB (the worker already fetches the problem row for
   `timeLimit`/`memoryLimit`/`testcases`).
2. Compare to `testcases/{P}/.version` on local disk. Match → judge immediately, no
   network at all.
3. Mismatch → take a per-problem lock file, download the object, extract to
   `testcases/.tmp/{P}.{version}/`, write `.version` **last**, then atomically
   `os.rename` into `testcases/{P}/` and delete the old directory.

The atomic rename is what prevents a lane reading a half-extracted problem while
another lane syncs. The lock file prevents two lanes downloading the same 185 MB twice.

Disk: 185 MB in MinIO + 185 MB extracted per judge host, with a transient ~3× for a
single problem mid-sync. Nothing accumulates across versions.

**New capability:** because MinIO now holds the master copy, the judge cache is
disposable. Problems untouched for months can be LRU-evicted and silently refetched on
the next submission. Today that's impossible — the volume *is* the master, so deleting
anything is data loss, and losing the volume destroys every problem's testcases while
the DB still looks healthy.

### Zip Slip — must fix

`zipFile.extractall()` on an admin-supplied archive lets an entry named `../../etc/...`
write anywhere the process can reach. That extraction is moving **into** the judge
container, which runs `privileged: true` as root — that is a container-escape-grade
write primitive, not just a messy directory. Validate that every member resolves under
the destination before extracting, and reject the archive otherwise.

---

## 4. Worker structure

```
src/
  main.py       entrypoint: config, pool, start listener + reaper + lanes
  db.py         psycopg3 connection pool, LISTEN connection
  queue.py      claim / finish / fail / requeue-on-startup
  testcases.py  MinIO sync, version check, safe extraction
  worker.py     lane loop: claim -> sync -> judge -> finalize
  judge.py      grading core (logic unchanged)
  isolate.py    unchanged
  utils.py      loses createTestcase(), keeps the rest
  config/
```

Changes to existing modules:

- **`judge.py`**: drop the module-level `submission = {}` dict. `evaluate()` takes an
  `on_progress(status: str)` callback and *returns* a result object instead of mutating
  global state. The grading logic itself is untouched.
- **`que.py`**: deleted, replaced by `queue.py` + `worker.py`.
- **`main.py`**: no FastAPI. `CMD ["python", "src/main.py"]`.
- **`requirements.txt`**: `fastapi[standard]` out; `psycopg[binary,pool]`, `minio` in.
- **`docker-compose.yml`**: drop `ports: 8000:8000` entirely — no inbound listener.
  Add `DATABASE_URL`, `S3_*`, `LANES`, `WORKER_ID`.

### Lanes and isolate box ids

`LANES` env var, **default 1** (behavior identical to today). Each lane is a thread
owning a fixed isolate box id (`0..LANES-1`).

Two bugs this fixes:

- **`--box-id={submission_id}`** — isolate boxes are 0–999 and submission ids grow
  without bound. This breaks permanently once ids pass 1000. Box id becomes the lane
  index, which is what it was always meant to be.
- **A stuck lane is currently unrecoverable.** No `--wall-time` is passed to isolate, so
  a program that blocks rather than spins is caught only by the Python
  `communicate(timeout=...)`, which raises out of `task()` and leaves
  `laneStatus[lane] = True` forever — one such submission permanently removes a lane
  from service. Under the new design, an escaped exception marks the row failed and
  releases the lane; `--wall-time` is passed as defence in depth.

### Unbounded output read

`judge.py:66` reads a submission's entire stdout into memory:

```python
output = open(f"{isolatePath}/{outputPath}").read()
```

`execute()` never passes isolate's `--fsize`, so a program printing in a loop grows that
file until the disk fills, and the worker then tries to load it into RAM. The result is
an OOM-killed worker — which, combined with `restart: unless-stopped`, is the crash loop
`attempts` exists to break (§2).

Fix both ends: pass `--fsize` to cap what a submission can write, and read the output
with a size limit rather than `.read()`. A submission whose output exceeds the cap is a
WA (or an explicit output-limit verdict), not a judge failure.

### On running multiple lanes on one machine

isolate's `--time` and the meta `time` field are **CPU time**, not wall clock, so a
neighbour on another core doesn't directly consume your submission's budget. What does
leak through is shared L3 and memory bandwidth — and hyperthread siblings, which are
severe. A memory-bound solution can measure 5–30% slower with a noisy neighbour, enough
to flip a borderline submission to TLE non-deterministically.

Throughput does improve, because compile, isolate init/cleanup, testcase I/O and DB
round-trips aren't CPU-saturating. But the tradeoff is **timing fidelity**, not
throughput. Hence: default 1; raise only with `taskset` pinning one *physical* core per
lane, leaving cores for Postgres, MinIO, Next.js and the OS. Keep it at 1 for contests.

### Connection budget

`LANES + 2` connections (lanes + listener + reaper).

---

## 5. Frontend changes

| File | Change |
|---|---|
| `actions/judge.ts` | `submitCode` drops the `fetch` and the `getSubmission` call — it creates the row and redirects. **`getSubmission()` is deleted entirely**, including its `setInterval`. |
| `actions/admin/judge.ts` | `rejudge` becomes `UPDATE ... SET judgeStatus='pending', status='Pending', priority=1, attempts=0, score=0, result=[], errorCode=NULL, error=NULL`. `rejudgeAllSubmission` becomes **one** UPDATE with `WHERE "problemId" = X` — instant instead of N HTTP calls. |
| `utils/uploadTestcase.ts` | Uploads to MinIO via `lib/minio` and records `testcaseVersion`. No backend call. |
| `actions/admin/problem.ts` | **Check `uploadTestcase`'s return value** — both call sites currently discard it, so a failed upload reports success and the problem goes live with no testcases. |
| `docker-compose.prod.yaml` | Drop `BACKEND_PROTOCOL` / `BACKEND_ENDPOINT` / `BACKEND_PORT`. |

The submission page already renders from the DB, so live progress keeps working with no
UI change — the worker writes `status` where the frontend poller used to.

### Upload memory, and the real size ceiling

Uploads work today and the sizes involved are bounded by the app's own policy:
`limits.testcase.size` is **100 MB**. Problem 1036's `testcases.zip` (185 MB) is
rejected by that zod rule; the archive actually in use is `testcases (subtask).zip` at
87 MB. So ~100 MB is the working ceiling, not 185 MB.

The cost worth fixing is heap, not the limit. `lib/minio.ts` does:

```ts
const fileBuffer = Buffer.from(await sourceFile.arrayBuffer());
await minioClient.putObject(bucket, fileName, fileBuffer);
```

which materializes the whole archive in Node's heap on top of the FormData copy — a
couple hundred MB spike on a host also running Postgres and MinIO. `putObject` accepts a
`Readable`, so stream instead:

```ts
await minioClient.putObject(bucket, fileName,
    Readable.fromWeb(sourceFile.stream()), sourceFile.size);
```

A **presigned PUT** (browser uploads straight to MinIO, Next never touches the bytes) is
the answer only if the 100 MB ceiling ever needs raising. Not required for this work.

---

## 6. Restore the defined unit: `score` = cases passed

### The definition

`submissions.score` is **the number of passed testcases**, not the final score. The UI
derives the displayed score at render time. This is deliberate: raising a problem's max
score from 100 to 200 must not require rejudging, because stored scores are independent
of scoring policy. This definition is correct and stays.

### Where the code violates it

`judge.py:208` converts the passed-case count into weight units:

```python
score = subtask_score / len(subtask["cases"]) * subtask["weight"]
```

`weight` comes from `subtask.json`'s `score` field, so on the subtask path both
`submissions.score` and `result.scores[i]` hold weighted points, not cases. The unit
leak reaches the renderers too — `verdict.tsx:110` divides `scores[i]` by `weights[i]`,
and `score-cell.tsx:25` uses `sum(weights)` as the denominator.

Consequences on problem 1036 (weights 6+14+20+10+10+40 = 100, `testcases` = 50):

- `score` is out of 100 while the definition says out of 50.
- `isAccepted = score === problem.testcases` never fires on full marks (100 ≠ 50), and
  fires on a partial solution that happens to score exactly 50.
- Fractional scores become reachable via non-group subtasks with an indivisible weight
  (`"case": "1-3", "score": 10`, one case passing → 3.33 into an `Int` column).

### The fix

Persist only the raw measurement; derive everything weight-dependent at render.

**`judge.py`** — drop the weight multiplication:

```python
scores.append(subtask_score)      # passed case count for this subtask
...
total_score = len(passed_cases)   # distinct case ids with AC, across all subtasks
```

Use a **set of case ids**, not a sum of per-subtask counts: the format allows a case in
several subtasks (cumulative scoring — `subtask 1: "1-3"`, `subtask 2: "1-10"`), and
summing would double-count past `problem.testcases`. 1036's subtasks are disjoint, so
both agree there; the set is correct for both layouts.

`result` keeps its shape — `scores`, `verdicts`, `times`, `memories`, `weights` — but
`scores[i]` is now a case count. `weights[i]` is unchanged and still carries the scoring
policy to the renderer.

**Frontend** — divide by case count instead of weight (already in scope as
`subtask_verdicts.length`):

```ts
// verdict.tsx:110
const subtaskScore = subtaskScoreDecimal.div(subtask_verdicts.length).mul(subtaskFullScore)

// score-cell.tsx — derive the weighted total from result
const total = scores.reduce((sum, s, i) => sum + (s / verdicts[i].length) * weights[i], 0);
// display: (total / weight) * problemScore
```

### Precision policy: exact integers stored, rounding only at render

`score` stays `Int`, and `result.scores[i]` stays an integer count. A count is discrete —
`Float` adds no precision, weakens `score === testcases` to a float comparison, and
reopens the unit confusion. Everything persisted is an exact integer.

The displayed score is derived:

```
passed_i / count_i × weight_i / Σweights × problem.score
```

All inputs exact, so raising `problem.score` recomputes cleanly with no inherited error.
**This is what makes rejudge-free rescaling exact rather than approximate.** Rounding
before storage would break it — a subtask of 3 cases worth 10 with 1 passing, stored as
`3.33`, doubles to `6.66` when `problem.score` goes 100 → 200, where the true value is
`6.667`. Stored as the count `1`, it recomputes exactly. Rounding errors bake in and then
amplify.

Render-side cleanups to do at the same time:

- **Round once, at the end.** `verdict.tsx:109-110` rounds `subtaskFullScore` to 2dp and
  then feeds that rounded value into `subtaskScore`, compounding the error. Carry
  `Decimal` through the chain and round only for output.
- **Match the two renderers.** `verdict.tsx` uses `Decimal`; `score-cell.tsx` uses plain
  JS arithmetic. They can disagree in the last decimal place.
- Rounded subtask badges need not sum to the rounded total (3.33 × 3 = 9.99 ≠ 10).
  Acceptable; largest-remainder allocation if they must add up.

### What this buys beyond correctness

- **`score` is integral by construction.** The fractional-score class stops existing
  rather than being guarded — no `round()`, no divisibility validation.
- **`isAccepted = score === problem.testcases` becomes correct on both paths**, with no
  change to `actions/judge.ts:171` or `actions-button.tsx:26`. One unit everywhere.
- **Rejudge-free rescoring gets stronger**: `score` is now independent of subtask
  weights, not just of `problem.score`.

### Migrating existing rows

Every current submission stores `score` and `result.scores` in weight units, so changing
the render math makes history display wrong (1036's perfect submissions would read
50/100). Two options:

1. **Rejudge after the queue lands.** Rejudge-all becomes a single UPDATE, making a
   re-grade of every subtask problem cheap. One code path afterwards. **Recommended** —
   and the reason this change is sequenced *after* the queue migration.
2. **Version the result JSON** (`"version": 2`) and branch in the UI. No rejudge, but
   both formulas live on indefinitely.

---

## 7. Rollout

Ordered so the existing path keeps working until the last step.

1. **Prisma migration** — add columns, enum, index, trigger; backfill
   `judgeStatus='done'`. Deploy frontend. Nothing reads the new columns yet; the old
   HTTP flow is untouched.
2. **Migrate testcases to MinIO** — one-time script: for each problem directory on the
   judge volume, zip it, upload to `problem/{id}/testcase.zip`, set `testcaseVersion`.
   Verify counts against `Problem.testcases`.
3. **Build the worker** alongside the existing FastAPI app — claim loop, testcase sync,
   finalize. Run with `LANES=1` against a staging DB and confirm verdicts match the old
   path on a set of known submissions.
4. **Cut over**: deploy the worker, delete the FastAPI endpoints and `que.py`, drop port
   8000, remove the frontend's `fetch` calls and `getSubmission`.
5. **Cleanup**: remove `BACKEND_*` env vars; optionally raise `LANES` with pinning.
6. **Scoring unit fix** (§6) — drop the weight multiplication in `judge.py`, update the
   two renderers, then rejudge subtask problems. Deliberately last: rejudge-all is a
   single UPDATE by this point, so re-grading the backlog is cheap.

Rollback for step 4 is a redeploy of the previous image — the new columns are additive
and the old code ignores them.

## 8. Open decisions

- **Cache eviction**: implement LRU now, or leave the cache unbounded (matching today's
  behavior) until disk pressure appears?
- **`WORKER_ID`**: hostname is a sensible default; worth setting explicitly if you ever
  run two judge hosts.
