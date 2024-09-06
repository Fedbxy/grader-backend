from fastapi import FastAPI, Form, UploadFile
from typing import Annotated
import threading

from utils import createTestcase
from judge import submission
import que


app = FastAPI()


@app.get("/submission/{id}")
def getSubmission(id: str):
    if id not in submission:
        return {
            "error": "Submission not found",
        }
    return submission[id]


@app.get("/submission/{id}/finished")
def removeSubmission(id: str):
    if id not in submission:
        return {
            "error": "Submission not found",
        }
    if not submission[id].get("result"):
        return {
            "error": "Submission is still running",
        }
    submission.pop(id)
    return {
        "message": "Submission removed",
    }


@app.post("/submit")
def createSubmission(id: Annotated[str, Form()], problemId: Annotated[str, Form()], timeLimit: Annotated[int, Form()], memoryLimit: Annotated[int, Form()], testcases: Annotated[int, Form()], language: Annotated[str, Form()], code: Annotated[str, Form()]):
    que.add(id, problemId, timeLimit, memoryLimit, testcases, language, code)

    return {
        "message": "Submission created",
    }


@app.post("/testcase/{problemId}/upload")
def uploadTestcase(problemId: int, file: UploadFile):
    return createTestcase(problemId, file)


threading.Thread(target=que.process, daemon=True).start()