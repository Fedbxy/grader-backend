import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import settings
from config.languages import LANGUAGE_REGISTRY
from utils import normalizeOutput, removeFile, readSubtask, readCapped
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


def isolateRunCommand(box: int, metaPath: str, outputPath: str, errorPath: str, timeLimit: float, memoryLimit: float, language: str):
    """The isolate invocation for running a program once.

    Shared by testcase grading and runs so the sandbox limits cannot drift
    apart. Limits arrive already scaled by the language multipliers.
    """
    return [
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


def runIsolate(cmd: list, inputPath: str, timeLimit: float):
    """Run an isolate command with inputPath as stdin and wait for it to end."""
    with open(inputPath, "r") as inputFile:
        process = subprocess.Popen(cmd, shell=False, text=True, stdin=inputFile, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        process.communicate(timeout=timeLimit / 1000 + 10)
    except subprocess.TimeoutExpired:
        process.kill()
        raise Exception("Isolate didn't terminate in time")


def classify(meta: dict):
    """Map an isolate meta file to TLE, MLE, RE, or None when the program ran to completion.

    Raises when isolate itself failed, which is the judge's fault, not the program's.
    """
    status = "status" in meta and meta["status"] or None
    exitsig = "exitsig" in meta and meta["exitsig"] or None

    if status and status == "XX":
        raise Exception("Isolate failed to execute")

    if status and status == "TO":
        return "TLE"

    if exitsig and exitsig == "6":
        return "MLE"

    if status and (status == "SG" or status == "RE"):
        return "RE"

    return None


def execute(isolatePath: str, box: int, testcaseDir: str, timeLimit: int, memoryLimit: int, language: str, testcase: int):
    inputPath = f"{testcaseDir}/{testcase}.in"
    expectedOutputPath = f"{testcaseDir}/{testcase}.sol"

    metaPath = f"tmp/{box}.meta"
    outputPath = f"{box}.output"
    errorPath = f"{box}.error"

    timeLimit *= LANGUAGE_REGISTRY[language]["time_multiplier"]
    memoryLimit *= LANGUAGE_REGISTRY[language]["memory_multiplier"]

    cmd = isolateRunCommand(box, metaPath, outputPath, errorPath, timeLimit, memoryLimit, language)
    runIsolate(cmd, inputPath, timeLimit)

    meta = readMetaFile(metaPath)
    output = open(f"{isolatePath}/{outputPath}").read()
    expectedOutput = open(expectedOutputPath).read()

    result = {
        "time": float(meta["time"]) * 1000,
        "memory": float(meta["max-rss"]),
    }

    verdict = classify(meta)

    if verdict == "TLE":
        result["verdict"] = "TLE"
        result["time"] = timeLimit
        return result

    if verdict == "MLE":
        result["verdict"] = "MLE"
        result["memory"] = memoryLimit * 1024
        return result

    if verdict == "RE":
        result["verdict"] = "RE"
        return result

    if (normalizeOutput(output) == normalizeOutput(expectedOutput)):
        result["verdict"] = "AC"
        return result

    result["verdict"] = "WA"
    return result


@dataclass
class RunOutcome:
    """Outcome of one Run (DESIGN.md "Runs (#67)").

    errorCode set means the program never ran: CE is the user's compile error.
    Otherwise verdict is OK, TLE, MLE or RE and the output fields are filled.
    """
    verdict: str | None = None
    exitCode: int | None = None
    exitSignal: int | None = None
    stdout: str = ""
    stdoutTruncated: bool = False
    stderr: str = ""
    stderrTruncated: bool = False
    time: float | None = None
    memory: float | None = None
    errorCode: str | None = None
    error: str | None = None


def run_once(isolatePath: str, box: int, stdin: str, timeLimit: int, memoryLimit: int, language: str, onProgress=None) -> RunOutcome:
    """Compile and run the code in the box once on the given stdin.

    No testcases are involved. The caller owns the box: it is initialised
    before and cleaned up after.
    """
    def progress(status):
        if onProgress:
            onProgress(status)

    progress("Compiling")
    compileResult = compile(box, language)
    if compileResult:
        return RunOutcome(errorCode="CE", error=compileResult)

    progress("Running")

    metaPath = f"tmp/{box}.meta"
    # Kept outside the box so the program cannot read or replace it; like a
    # testcase input, it reaches the program only as stdin.
    inputPath = f"tmp/{box}.stdin"
    outputPath = f"{box}.output"
    errorPath = f"{box}.error"

    timeLimit *= LANGUAGE_REGISTRY[language]["time_multiplier"]
    memoryLimit *= LANGUAGE_REGISTRY[language]["memory_multiplier"]

    os.makedirs("tmp", exist_ok=True)
    open(f"{isolatePath}/{outputPath}", "w").close()
    open(f"{isolatePath}/{errorPath}", "w").close()

    try:
        # Explicit so non-ASCII input never depends on the container locale.
        with open(inputPath, "w", encoding="utf-8") as inputFile:
            inputFile.write(stdin)

        cmd = isolateRunCommand(box, metaPath, outputPath, errorPath, timeLimit, memoryLimit, language)
        runIsolate(cmd, inputPath, timeLimit)

        meta = readMetaFile(metaPath)
        verdict = classify(meta)
    finally:
        if os.path.exists(inputPath):
            os.remove(inputPath)
        removeFile(box)

    # A program may print up to OUTPUT_LIMIT_KB; only the head is kept, and it
    # is read as a head rather than read whole and sliced.
    stdout, stdoutTruncated = readCapped(f"{isolatePath}/{outputPath}", settings.RUN_OUTPUT_LIMIT_BYTES)
    stderr, stderrTruncated = readCapped(f"{isolatePath}/{errorPath}", settings.RUN_OUTPUT_LIMIT_BYTES)

    outcome = RunOutcome(
        verdict=verdict or "OK",
        stdout=stdout, stdoutTruncated=stdoutTruncated,
        stderr=stderr, stderrTruncated=stderrTruncated,
        time=float(meta["time"]) * 1000,
        memory=float(meta["max-rss"]),
    )

    if verdict == "TLE":
        outcome.time = timeLimit
    elif verdict == "MLE":
        outcome.memory = memoryLimit * 1024
    elif verdict == "RE":
        if "exitcode" in meta:
            outcome.exitCode = int(meta["exitcode"])
        if "exitsig" in meta:
            outcome.exitSignal = int(meta["exitsig"])

    return outcome


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
