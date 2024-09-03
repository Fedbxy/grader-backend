from queue import Queue
import threading
import time

import utils
import judge


queue = Queue()
lane_status = {
    "1": False,
    "2": False,
    "3": False,
}


def add(id: str, problem_id: str, time_limit: int, memory_limit: int, testcases: int, language: str, code: str):
    judge.submission[id] = {
        "verdict": "In queue",
    }

    data = {
        "id": id,
        "problem_id": problem_id,
        "time_limit": time_limit,
        "memory_limit": memory_limit,
        "testcases": testcases,
        "language": language,
        "code": code,
    }

    queue.put(data)


def free_lane():
    for lane in lane_status:
        if not lane_status[lane]:
            return lane
    return None
    

def process():
    while True:
        time.sleep(0.1)
        lane = free_lane()
        if lane is None or queue.empty():
            continue

        lane_status[lane] = True
        
        data = queue.get()

        id = data["id"]
        problem_id = data["problem_id"]
        time_limit = data["time_limit"]
        memory_limit = data["memory_limit"]
        testcases = data["testcases"]
        language = data["language"]
        code = data["code"]

        threading.Thread(target=task, args=(lane, id, problem_id, time_limit, memory_limit, testcases, language, code)).start()


def task(lane: str, id: str, problem_id: str, time_limit: int, memory_limit: int, testcases: int, language: str, code: str):
    utils.create_file(id, language, code)

    judge.judge(id, problem_id, time_limit, memory_limit, testcases, language)

    lane_status[lane] = False