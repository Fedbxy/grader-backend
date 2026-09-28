"""Legacy in-memory queue behind the HTTP endpoints.

Superseded by worker.py, which claims from Postgres. Both grade through the same
judge.evaluate(); this one keeps its results in a dict for the frontend to poll.
Deleted at cutover (DESIGN.md section 7 step 4).
"""

from queue import Queue
import threading
import time

from utils import createFile
from judge import evaluate
from isolate import initIsolate, cleanupIsolate


queue = Queue()
totalLanes = 1
laneStatus = {i: False for i in range(totalLanes)}

submission = {}


def add(id: str, problemId: str, timeLimit: int, memoryLimit: int, testcases: int, language: str, code: str):
    submission[id] = {
        "status": "In queue",
    }

    data = {
        "id": id,
        "problemId": problemId,
        "timeLimit": timeLimit,
        "memoryLimit": memoryLimit,
        "testcases": testcases,
        "language": language,
        "code": code,
    }

    queue.put(data)


def getFreeLane():
    for lane in laneStatus:
        if not laneStatus[lane]:
            return lane
    return None


def process():
    while True:
        time.sleep(0.1)
        lane = getFreeLane()
        if lane is None or queue.empty():
            continue

        laneStatus[lane] = True

        data = queue.get()

        threading.Thread(target=task, args=(
            lane, data["id"], data["problemId"], data["timeLimit"],
            data["memoryLimit"], data["testcases"], data["language"], data["code"],
        )).start()


def task(lane: str, id: str, problemId: str, timeLimit: int, memoryLimit: int, testcases: int, language: str, code: str):
    try:
        isolatePath = initIsolate(id)

        if isolatePath is None:
            submission[id] = {
                "score": 0,
                "errorCode": "SE",
                "error": "Couldn't initialize isolate",
            }
            return

        createFile(isolatePath, id, language, code)

        outcome = evaluate(
            isolatePath, id, f"testcases/{problemId}", timeLimit, memoryLimit,
            testcases, language,
            onProgress=lambda status: submission.__setitem__(id, {"status": status}),
        )

        if outcome.errorCode:
            submission[id] = {
                "score": 0,
                "errorCode": outcome.errorCode,
                "error": outcome.error,
            }
        else:
            submission[id] = {
                "score": outcome.score,
                "result": outcome.result,
            }

        cleanupIsolate(id)
    except Exception as error:
        # Previously an escaped exception left the lane marked busy forever,
        # taking the only lane out of service until a restart.
        submission[id] = {
            "score": 0,
            "errorCode": "SE",
            "error": str(error),
        }
    finally:
        laneStatus[lane] = False
