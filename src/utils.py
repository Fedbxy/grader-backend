import os
import shutil
from zipfile import ZipFile
from fastapi import UploadFile


def normalize_output(output: str):
    lines = output.split("\n")
    normalized_lines = (line.rstrip() for line in lines)
    return ("\n".join(normalized_lines)).strip()


def create_file(id: int, language: str, code: str):
    if not os.path.exists("tmp"):
        os.makedirs("tmp")

    with open(f"tmp/{id}.{language}", "w") as file:
        file.write(code)


def remove_file(id: int, language: str):
    if os.path.exists(f"tmp/{id}"):
        os.remove(f"tmp/{id}")

    if os.path.exists(f"tmp/{id}.{language}"):
        os.remove(f"tmp/{id}.{language}")

    if not os.listdir("tmp"):
        os.rmdir("tmp")


def create_testcase(problem_id: int, file: UploadFile):
    if file.filename.split(".")[-1] != "zip":
        return "Invalid file type"

    if not os.path.exists("testcases"):
        os.makedirs("testcases")

    if os.path.exists(f"testcases/{problem_id}"):
        shutil.rmtree(f"testcases/{problem_id}")

    os.makedirs(f"testcases/{problem_id}")

    with ZipFile(file.file, "r") as zip_file:
        zip_file.extractall(f"testcases/{problem_id}")