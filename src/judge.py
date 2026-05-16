import subprocess
import os

from config.languages import LANGUAGE_REGISTRY
from utils import normalizeOutput, removeFile, readSubtask
from isolate import readMetaFile


submission = {}


def compile(id: int, language: str):
    cmd = [
        "isolate",
        f"--box-id={id}",
        f"--mem={1024 * 1024}",
        f"--time={10}",
        "--processes=100",
        "--env=PATH=/usr/bin",
        "--run",
        "--",
    ] + LANGUAGE_REGISTRY[language]["compile"]("./", id)

    try:
        subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        return "Compilation Time Limit Exceeded"
    except subprocess.CalledProcessError as error:
        return error.stderr[2:]
    return None


def execute(isolatePath: str, id: int, problemId: int, timeLimit: int, memoryLimit: int, language: str, testcase: int):
    inputPath = f"testcases/{problemId}/{testcase}.in"
    expectedOutputPath = f"testcases/{problemId}/{testcase}.sol"

    metaPath = f"tmp/{id}.meta"
    outputPath = f"{id}.output"
    errorPath = f"{id}.error"

    timeLimit *= LANGUAGE_REGISTRY[language]["time_multiplier"]
    memoryLimit *= LANGUAGE_REGISTRY[language]["memory_multiplier"]

    cmd = [
        "isolate",
        f"--box-id={id}",
        f"--meta={metaPath}",
        f"--stdout={outputPath}",
        f"--stderr={errorPath}",
        f"--time={timeLimit / 1000}",
        f"--mem={memoryLimit * 1024}",
        "--run",
        "--"
    ] + LANGUAGE_REGISTRY[language]["execute"](id)

    with open(inputPath, "r") as inputFile:
        process = subprocess.Popen(cmd, shell=False, text=True, stdin=inputFile, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        process.communicate(timeout=timeLimit / 1000 + 5)
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


def evaluate(isolatePath: str, id: int, problemId: int, timeLimit: int, memoryLimit: int, testcases: int, language: str):
    if not os.path.exists(f"testcases/{problemId}") or not os.listdir(f"testcases/{problemId}"):
        submission[id] = {
            "score": 0,
            "errorCode": "JE",
            "error": "No testcases found",
        }
        return

    subtask_cases = []
    if os.path.exists(f"testcases/{problemId}/subtask.json"):
        subtask_data = readSubtask(problemId, testcases)
        if "error" in subtask_data:
            submission[id] = {
                "score": 0,
                "errorCode": "JE",
                "error": subtask_data["error"],
            }
            return

        subtask_cases = subtask_data["data"]

    submission[id] = {
        "status": "Compiling",
    }
    compileResult = compile(id, language)
    if compileResult:
        submission[id] = {
            "score": 0,
            "errorCode": "CE",
            "error": compileResult,
        }
        return
    
    if not os.path.exists("tmp"):
        os.makedirs("tmp")
    open(f"{isolatePath}/{id}.output", "w").close()
    open(f"{isolatePath}/{id}.error", "w").close()

    if not subtask_cases:
        subtask_cases = [{
            "id": 1,
            "cases": list(range(1, testcases + 1)),
            "weight": testcases,
            "group": False,
            "require": [],
            "option": "sum",
        }]

    total_score = 0
    scores = []
    weights = [subtask["weight"] for subtask in subtask_cases]
    verdicts = []
    times = []
    memories = []
    requirePassed = [False] * len(subtask_cases)

    for subtask in subtask_cases:
        subtask_score = 0
        subtask_verdicts = []
        subtask_times = []
        subtask_memories = []
        isSkipped = False

        for case in subtask["cases"]:
            testcaseValid = os.path.exists(f"testcases/{problemId}/{case}.in") and os.path.exists(f"testcases/{problemId}/{case}.sol")
            if not testcaseValid:
                submission[id] = {
                    "score": 0,
                    "errorCode": "JE",
                    "error": f"Testcase {case} not found",
                }
                removeFile(id)
                return

            submission[id] = {
                "status": f"Running on testcase {case}",
            }

            if isSkipped or any(not requirePassed[req - 1] for req in subtask["require"]):
                subtask_verdicts.append("SKP")
                subtask_times.append(0)
                subtask_memories.append(0)
                continue

            executeResult = execute(isolatePath, id, problemId, timeLimit, memoryLimit, language, case)

            subtask_verdicts.append(executeResult["verdict"])
            subtask_times.append(executeResult.get("time"))
            subtask_memories.append(executeResult.get("memory"))

            if executeResult["verdict"] == "AC":
                subtask_score += 1
                if subtask["option"] == "max":
                    subtask_score = max(subtask_score, 0)
                elif subtask["option"] == "min":
                    subtask_score = min(subtask_score, 1)
            elif subtask["group"]:
                isSkipped = True

        if subtask["group"] and isSkipped:
            subtask_score = 0

        if subtask_score == len(subtask["cases"]):
            requirePassed[int(subtask["id"]) - 1] = True

        score = subtask_score / len(subtask["cases"]) * subtask["weight"]
        total_score += score
        scores.append(score)
        verdicts.append(subtask_verdicts)
        times.append(subtask_times)
        memories.append(subtask_memories)

    removeFile(id)

    weights = [subtask["weight"] for subtask in subtask_cases]
    submission[id] = {
        "score": total_score,
        "result": {
            "scores": scores,
            "verdicts": verdicts,
            "times": times,
            "memories": memories,
            "weights": weights,
        },
    }
