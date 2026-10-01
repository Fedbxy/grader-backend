"""Subtask scoring aggregation, with execute() stubbed.

Covers DESIGN.md section 6: score is a count of passed testcases, and all
weighting happens at render time. Run from a writable directory:

    docker run --rm -w /tmp -v $PWD:/app:ro grader-backend:main python /app/tests/test_scoring.py
"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import judge

FAILED = []
def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond: FAILED.append(msg)

def make_dir(ncases, subtask=None):
    d = tempfile.mkdtemp()
    for i in range(1, ncases + 1):
        open(f"{d}/{i}.in", "w").write("x")
        open(f"{d}/{i}.sol", "w").write("x")
    if subtask:
        json.dump({"version": 1.0, "data": subtask}, open(f"{d}/subtask.json", "w"))
    return d

def run(ncases, subtask, ac_predicate):
    judge.compile = lambda box, language: None
    judge.execute = lambda isolatePath, box, testcaseDir, tl, ml, lang, case: {
        "verdict": "AC" if ac_predicate(case) else "WA", "time": 1.0, "memory": 1.0}
    d = make_dir(ncases, subtask)
    os.makedirs("/tmp/box", exist_ok=True)
    return judge.evaluate("/tmp/box", 0, d, 1000, 32, ncases, "cpp")

# 1036's real shape: weights sum to 100 across 50 cases.
S1036 = {
    "subtask 1": {"case": "1-3",   "group": True, "score": 6},
    "subtask 2": {"case": "4-10",  "group": True, "score": 14},
    "subtask 3": {"case": "11-20", "group": True, "score": 20},
    "subtask 4": {"case": "21-25", "group": True, "score": 10},
    "subtask 5": {"case": "26-30", "group": True, "score": 10},
    "subtask 6": {"case": "31-50", "group": True, "score": 40},
}

def display(r, problem_score):
    """What the renderer computes: sum(passed_i/count_i * weight_i)."""
    total_w = sum(r.result["weights"])
    earned = sum(s / len(v) * w for s, v, w in
                 zip(r.result["scores"], r.result["verdicts"], r.result["weights"]))
    return earned / total_w * problem_score, earned, total_w

print("all cases pass (1036 shape):")
r = run(50, S1036, lambda c: True)
check(r.score == 50, f"score is the case count 50, not the weighted 100 (got {r.score})")
check(r.result["scores"] == [3, 7, 10, 5, 5, 20], f"per-subtask counts {r.result['scores']}")
check(r.result["weights"] == [6, 14, 20, 10, 10, 40], "weights carried through unchanged")
d, earned, tw = display(r, 100)
check(d == 100 and earned == tw, f"renders full marks: {d}/100")
check(r.score == 50, "isAccepted comparison (score == problem.testcases) holds")

print("\nsubtask 2 fails its first case (group -> zeroed):")
r = run(50, S1036, lambda c: c <= 3)
check(r.score == 3, f"only subtask 1's 3 cases count (got {r.score})")
check(r.result["scores"][1] == 0, "failed group scores 0 even though no case in it passed anyway")
d, _, _ = display(r, 100)
check(abs(d - 6) < 1e-9, f"renders 6/100, matching subtask 1's weight (got {d})")

print("\ngroup with some cases passing before the failure:")
r = run(50, S1036, lambda c: c <= 3 or c == 4)
check(r.score == 3, f"case 4 passed but its group failed, so it does not count (got {r.score})")
check(r.result["scores"][1] == 0, "group score zeroed")

print("\ncumulative subtasks (a case in two subtasks):")
CUM = {"subtask 1": {"case": "1-3", "score": 30}, "subtask 2": {"case": "1-10", "score": 70}}
r = run(10, CUM, lambda c: True)
check(r.score == 10, f"distinct cases counted once, not 13 (got {r.score})")
d, _, _ = display(r, 100)
check(abs(d - 100) < 1e-9, f"still renders full marks: {d}")

print("\nno subtask.json:")
r = run(10, None, lambda c: c <= 7)
check(r.score == 7, f"score is passed count (got {r.score})")
check(r.result["scores"] == [7] and r.result["weights"] == [10], "single pseudo-subtask")
d, _, _ = display(r, 100)
check(abs(d - 70) < 1e-9, f"renders 70/100 (got {d})")

print("\nnon-group subtask with an indivisible weight (old fractional-score case):")
ND = {"subtask 1": {"case": "1-3", "score": 10}}
r = run(3, ND, lambda c: c == 1)
check(r.score == 1 and isinstance(r.score, int), f"score is the integer 1 (got {r.score!r})")
d, _, _ = display(r, 100)
check(abs(d - 100/3) < 1e-9, f"fraction lives only in the display: {d:.4f}")

print("\nFAILURES:", FAILED if FAILED else "none")
sys.exit(1 if FAILED else 0)
