from fastapi import FastAPI, Form, UploadFile
from typing import Annotated

import utils
import judge


app = FastAPI()


@app.get("/submission/{id}")
def get_submission(id: str):
    if id not in judge.submission:
        return "Submission not found"
    return judge.submission[id]


@app.get("/submission/{id}/finished")
def get_submission(id: str):
    if id not in judge.submission or judge.submission[id] != "Finished":
        return "Submission not found"
    judge.submission.pop(id)
    return "Submission removed"


@app.post("/submit")
def create_submission(id: Annotated[str, Form()], problem_id: Annotated[str, Form()], time_limit: Annotated[int, Form()], memory_limit: Annotated[int, Form()], testcases: Annotated[int, Form()], language: Annotated[str, Form()], code: Annotated[str, Form()]):
    utils.create_file(id, language, code)

    result = judge.judge(id, problem_id, time_limit, memory_limit, testcases, language)

    utils.remove_file(id, language)

    score = 0
    for verdict in result:
        if verdict == "AC":
            score += 1

    return {
        "score": score,
        "result": result,
    }


@app.post("/testcase/{problem_id}/upload")
def upload_testcase(problem_id: int, file: UploadFile):
    return utils.create_testcase(problem_id, file)
