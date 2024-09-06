from queue import Queue
import threading
import time

from utils import createFile
from judge import submission, evaluate
from isolate import initIsolate, cleanupIsolate


queue = Queue()
laneStatus = {
    "1": False,
    "2": False,
    "3": False,
}


def add(id: str, problemId: str, timeLimit: int, memoryLimit: int, testcases: int, language: str, code: str):
    submission[id] = {
        "verdict": "In queue",
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

        id = data["id"]
        problemId = data["problemId"]
        timeLimit = data["timeLimit"]
        memoryLimit = data["memoryLimit"]
        testcases = data["testcases"]
        language = data["language"]
        code = data["code"]

        threading.Thread(target=task, args=(lane, id, problemId, timeLimit, memoryLimit, testcases, language, code)).start()


def task(lane: str, id: str, problemId: str, timeLimit: int, memoryLimit: int, testcases: int, language: str, code: str):
    isolatePath = initIsolate(id)

    if isolatePath is None:
        submission[id] = {
            "score": 0,
            "result": [{
                "verdict": "SE",
                "error": "Couldn't initialize isolate",
            }],
        }
        laneStatus[lane] = False
        return

    createFile(isolatePath, id, language, code)

    evaluate(isolatePath, id, problemId, timeLimit, memoryLimit, testcases, language)

    cleanupIsolate(id)

    laneStatus[lane] = False