import os
import shutil
from zipfile import ZipFile
from fastapi import UploadFile


def normalizeOutput(output: str):
    lines = output.split("\n")
    normalizedLines = (line.rstrip() for line in lines)
    return ("\n".join(normalizedLines)).strip()


def createFile(isolatePath: str, id: int, language: str, code: str):
    path = f"{isolatePath}/{id}.{language}"

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