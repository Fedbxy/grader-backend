import os
import shutil
import json
from zipfile import ZipFile
from fastapi import UploadFile
from config.languages import LANGUAGE_REGISTRY


def normalizeOutput(output: str):
    lines = output.split("\n")
    normalizedLines = (line.rstrip() for line in lines)
    return ("\n".join(normalizedLines)).strip()


def createFile(isolatePath: str, id: int, language: str, code: str):
    path = f"{isolatePath}/{id}.{LANGUAGE_REGISTRY[language]['extension']}"

    with open(path, "w") as file:
        file.write(code)

    return path


def removeFile(id: int):
    meta = f"tmp/{id}.meta"
    dir = os.path.dirname(meta)

    if os.path.exists(meta):
        os.remove(meta)

    if not os.listdir(dir):
        os.rmdir(dir)


def createTestcase(problemId: int, file: UploadFile):
    if file.filename.split(".")[-1] != "zip":
        return "Invalid file type"

    if not os.path.exists("testcases"):
        os.makedirs("testcases")

    if os.path.exists(f"testcases/{problemId}"):
        shutil.rmtree(f"testcases/{problemId}")

    os.makedirs(f"testcases/{problemId}")

    with ZipFile(file.file, "r") as zipFile:
        zipFile.extractall(f"testcases/{problemId}")


def extractRangeString(rangeString: str):
    subtask_cases = []
    if rangeString.find("-") == -1:
        subtask_cases.append(int(rangeString))
        return subtask_cases

    start, end = rangeString.split("-")
    start = int(start)
    end = int(end)
    for i in range(start, end + 1):
        subtask_cases.append(i)

    return subtask_cases


def extractSubtask(data: dict, testcaseCount: int):
    subtasks = []

    for (key, subtask) in data.items():
        subtask_cases = []

        subtask_id = key.replace("subtask", "").strip()
        case = subtask.get("case")
        weight = subtask.get("score") or testcaseCount
        group = subtask.get("group") or False
        require = subtask.get("require") or []
        option = subtask.get("option") or "sum"

        if not subtask_id.isdigit() or case is None:
            return {"error": "Invalid subtask format"}
        
        parts = case.split(",")
        for part in parts:
            subtask_cases.extend(extractRangeString(part))

        subtasks.append({
            "id": subtask_id,
            "cases": subtask_cases,
            "weight": weight,
            "group": group,
            "require": require,
            "option": option,
        })

    return {"data": subtasks}


def readSubtask(problemId: int, testcaseCount: int):
    if not os.path.exists(f"testcases/{problemId}/subtask.json"):
        return {"error": "Subtask file not found"}
    
    file = open(f"testcases/{problemId}/subtask.json").read()
    file = json.loads(file)
    
    data = file.get("data")
    if not data:
        return {"error": "Invalid subtask format"}

    subtask_cases = extractSubtask(data, testcaseCount)
    return subtask_cases