import subprocess
import os

from config import command
from utils import normalizeOutput, removeFile
from isolate import readMetaFile


submission = {}


def compile(isolatePath: str, id: int, language: str):
    try:
        subprocess.run(command.compile(isolatePath, id, language), check=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        return error.stderr
    return None


def execute(isolatePath: str, id: int, problemId: int, timeLimit: int, memoryLimit: int, language: str, testcase: int):
    inputPath = f"testcases/{problemId}/{testcase}.in"
    expectedOutputPath = f"testcases/{problemId}/{testcase}.sol"

    metaPath = f"tmp/{id}.meta"
    outputPath = f"{id}.output"
    errorPath = f"{id}.error"

    cmd = (
        f"isolate --box-id={id} "
        f"--meta={metaPath} --stdout={outputPath} --stderr={errorPath} "
        f"--time={timeLimit / 1000} --mem={memoryLimit * 1024} "
        f"--run -- {command.execute(id, language)} "
        f"< {inputPath}"
    )

    process = subprocess.Popen(cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

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
            "result": [{
                "verdict": "JE",
                "error": "No testcases found",
            }],
        }
        return
    
    submission[id] = {
        "verdict": "Compiling",
    }
    compileResult = compile(isolatePath, id, language)
    if compileResult:
        submission[id] = {
            "score": 0,
            "result": [{
                "verdict": "CE",
                "error": compileResult,
            }],
        }
        return
    
    if not os.path.exists("tmp"):
        os.makedirs("tmp")
    open(f"{isolatePath}/{id}.output", "w").close()
    open(f"{isolatePath}/{id}.error", "w").close()
    
    result = []

    for i in range(testcases):
        testcaseValid = os.path.exists(f"testcases/{problemId}/{i + 1}.in") and os.path.exists(f"testcases/{problemId}/{i + 1}.sol")
        if not testcaseValid:
            submission[id] = {
                "score": 0,
                "result": [{
                    "verdict": "JE",
                    "error": f"Testcase {i + 1} is missing",
                }]
            }
            removeFile(id, language)
            return

        submission[id] = {
            "verdict": f"Running on testcase {i + 1}",
        }
        executeResult = execute(isolatePath, id, problemId, timeLimit, memoryLimit, language, i + 1)

        result.append({
            "testcase": i + 1,
            "verdict": executeResult["verdict"],
            "time": executeResult.get("time"),
            "memory": executeResult.get("memory"),
            "error": executeResult.get("error"),
        })

    removeFile(id, language)

    score = 0
    for i in result:
        if i["verdict"] == "AC":
            score += 1

    submission[id] = {
        "score": score,
        "result": result,
    }
