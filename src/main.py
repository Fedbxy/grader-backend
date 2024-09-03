from fastapi import FastAPI, Form, UploadFile
from typing import Annotated
import threading

import utils
import judge
import que


app = FastAPI()


@app.get("/submission/{id}")
def get_submission(id: str):
    if id not in judge.submission:
        return {
            "error": "Submission not found",
        }
    return judge.submission[id]


@app.get("/submission/{id}/finished")
def get_submission(id: str):
    if id not in judge.submission:
        return {
            "error": "Submission not found",
        }
    if not judge.submission[id].get("result"):
        return {
            "error": "Submission is still running",
        }
    judge.submission.pop(id)
    return {
        "message": "Submission removed",
    }


@app.post("/submit")
def create_submission(id: Annotated[str, Form()], problem_id: Annotated[str, Form()], time_limit: Annotated[int, Form()], memory_limit: Annotated[int, Form()], testcases: Annotated[int, Form()], language: Annotated[str, Form()], code: Annotated[str, Form()]):
    que.add(id, problem_id, time_limit, memory_limit, testcases, language, code)

    return {
        "message": "Submission created",
    }


@app.post("/testcase/{problem_id}/upload")
def upload_testcase(problem_id: int, file: UploadFile):
    return utils.create_testcase(problem_id, file)


threading.Thread(target=que.process, daemon=True).start()