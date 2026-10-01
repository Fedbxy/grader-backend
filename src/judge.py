import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import settings
from config.languages import LANGUAGE_REGISTRY
from utils import normalizeOutput, removeFile, readSubtask
from isolate import readMetaFile


@dataclass
class JudgeResult:
    """Outcome of grading one submission.

    score is a count of passed testcases. errorCode set means the submission
    never produced a score: CE is the submitter's fault, JE is the judge's or
    the problem's.
    """
    score: int = 0
    result: dict | None = None
    errorCode: str | None = None
    error: str | None = None


def compile(box: int, language: str):
    cmd = [
        "isolate",
        f"--box-id={box}",
        f"--mem={1024 * 1024}",
        f"--time={10}",
        "--processes=100",
        "--env=PATH=/usr/bin",
        "--run",
        "--",
    ] + LANGUAGE_REGISTRY[language]["compile"]("./", box)

    try:
        subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        return "Compilation Time Limit Exceeded"
    except subprocess.CalledProcessError as error:
        return error.stderr[2:]
    return None


def execute(isolatePath: str, box: int, testcaseDir: str, timeLimit: int, memoryLimit: int, language: str, testcase: int):
    inputPath = f"{testcaseDir}/{testcase}.in"
    expectedOutputPath = f"{testcaseDir}/{testcase}.sol"

    metaPath = f"tmp/{box}.meta"
    outputPath = f"{box}.output"
    errorPath = f"{box}.error"

    timeLimit *= LANGUAGE_REGISTRY[language]["time_multiplier"]
    memoryLimit *= LANGUAGE_REGISTRY[language]["memory_multiplier"]

    cmd = [
        "isolate",
        f"--box-id={box}",
        f"--meta={metaPath}",
        f"--stdout={outputPath}",
        f"--stderr={errorPath}",
        f"--time={timeLimit / 1000}",
        f"--wall-time={timeLimit / 1000 + 5}",
        f"--mem={memoryLimit * 1024}",
        f"--fsize={settings.OUTPUT_LIMIT_KB}",
        "--run",
        "--"
    ] + LANGUAGE_REGISTRY[language]["execute"](box)

    with open(inputPath, "r") as inputFile:
        process = subprocess.Popen(cmd, shell=False, text=True, stdin=inputFile, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        process.communicate(timeout=timeLimit / 1000 + 10)
    except subprocess.TimeoutExpired:
        process.kill()
        raise Exception("Isolate didn't terminate in time")

    meta = readMetaFile(metaPath)
    output = open(f"{isolatePath}/{outputPath}").read()
    expectedOutput = open(expectedOutputPath).read()

    result = {
        "time": float(meta["time"]) * 1000,
        "memory": float(meta["max-rss"]),
    }

    status = "status" in meta and meta["status"] or None
    exitsig = "exitsig" in meta and meta["exitsig"] or None

    if status and status == "XX":
        raise Exception("Isolate failed to execute")

    if status and status == "TO":
        result["verdict"] = "TLE"
        result["time"] = timeLimit
        return result

    if exitsig and exitsig == "6":
        result["verdict"] = "MLE"
        result["memory"] = memoryLimit * 1024
        return result

    if status and (status == "SG" or status == "RE"):
        result["verdict"] = "RE"
        return result

    if (normalizeOutput(output) == normalizeOutput(expectedOutput)):
        result["verdict"] = "AC"
        return result

    result["verdict"] = "WA"
    return result


def evaluate(isolatePath: str, box: int, testcaseDir: str, timeLimit: int, memoryLimit: int, testcases: int, language: str, onProgress=None) -> JudgeResult:
    """Grade one submission and return the outcome.

    onProgress(status) receives the human-readable strings the UI renders. The
    caller decides where those go — the DB worker writes them to the submission
    row, the legacy HTTP path puts them in its in-memory dict.
    """
    def progress(status):
        if onProgress:
            onProgress(status)

    if not os.path.exists(testcaseDir) or not os.listdir(testcaseDir):
        return JudgeResult(errorCode="JE", error="No testcases found")

    subtask_cases = []
    if os.path.exists(f"{testcaseDir}/subtask.json"):
        subtask_data = readSubtask(testcaseDir, testcases)
        if "error" in subtask_data:
            return JudgeResult(errorCode="JE", error=subtask_data["error"])

        subtask_cases = subtask_data["data"]

    progress("Compiling")
    compileResult = compile(box, language)
    if compileResult:
        return JudgeResult(errorCode="CE", error=compileResult)

    if not os.path.exists("tmp"):
        os.makedirs("tmp")
    open(f"{isolatePath}/{box}.output", "w").close()
    open(f"{isolatePath}/{box}.error", "w").close()

    if not subtask_cases:
        subtask_cases = [{
            "id": 1,
            "cases": list(range(1, testcases + 1)),
            "weight": testcases,
            "group": False,
            "require": [],
            "option": "sum",
        }]

    # score is the number of passed testcases, never a weighted total: it must
    # stay independent of scoring policy so raising a problem's max score needs
    # no rejudge. Weights travel in the result for the renderer to apply
    # (DESIGN.md section 6).
    passed_cases = set()
    scores = []
    verdicts = []
    times = []
    memories = []
    requirePassed = [False] * len(subtask_cases)

    for subtask in subtask_cases:
        subtask_score = 0
        subtask_passed = set()
        subtask_verdicts = []
        subtask_times = []
        subtask_memories = []
        isSkipped = False

        for case in subtask["cases"]:
            testcaseValid = os.path.exists(f"{testcaseDir}/{case}.in") and os.path.exists(f"{testcaseDir}/{case}.sol")
            if not testcaseValid:
                removeFile(box)
                return JudgeResult(errorCode="JE", error=f"Testcase {case} not found")

            progress(f"Running on testcase {case}")

            if isSkipped or any(not requirePassed[req - 1] for req in subtask["require"]):
                subtask_verdicts.append("SKP")
                subtask_times.append(0)
                subtask_memories.append(0)
                continue

            executeResult = execute(isolatePath, box, testcaseDir, timeLimit, memoryLimit, language, case)

            subtask_verdicts.append(executeResult["verdict"])
            subtask_times.append(executeResult.get("time"))
            subtask_memories.append(executeResult.get("memory"))

            if executeResult["verdict"] == "AC":
                subtask_score += 1
                subtask_passed.add(case)
                if subtask["option"] == "max":
                    subtask_score = max(subtask_score, 0)
                elif subtask["option"] == "min":
                    subtask_score = min(subtask_score, 1)
            elif subtask["group"]:
                isSkipped = True

        if subtask["group"] and isSkipped:
            # All-or-nothing: a failed group scores zero, so none of its cases
            # count towards the total either.
            subtask_score = 0
            subtask_passed.clear()

        if subtask_score == len(subtask["cases"]):
            requirePassed[int(subtask["id"]) - 1] = True

        # A case may appear in several subtasks (cumulative scoring), so union
        # rather than sum: the total must not exceed the problem's case count.
        passed_cases |= subtask_passed
        scores.append(subtask_score)
        verdicts.append(subtask_verdicts)
        times.append(subtask_times)
        memories.append(subtask_memories)

    removeFile(box)

    weights = [subtask["weight"] for subtask in subtask_cases]
    return JudgeResult(
        score=len(passed_cases),
        result={
            "scores": scores,
            "verdicts": verdicts,
            "times": times,
            "memories": memories,
            "weights": weights,
        },
    )
