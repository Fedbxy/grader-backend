import subprocess
import psutil
import time
import os

from config import command
from utils import normalize_output


submission = {}


def compile(id: int, language: str):
    try:
        subprocess.run(command.compile(id, language), check=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        return error.stderr
    return None


def execute(id: int, problem_id: int, language: str, testcase: int):
    time_limit = 1000
    memory_limit = 32

    input = open(f"testcases/{problem_id}/{testcase}.in", "r")
    expected_output = open(f"testcases/{problem_id}/{testcase}.sol", "r")

    start_time = time.time()
    memory_usage = 0

    process = subprocess.Popen(command.execute(id, language), stdin=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    try:
        while not process.poll():
            try:
                memory_usage = max(memory_usage, psutil.Process(process.pid).memory_info().rss / 1024)
            except psutil.NoSuchProcess:
                break

            if memory_usage > memory_limit * 1024:
                process.kill()
                return {
                    "verdict": "MLE",
                    "time": f"{(time.time() - start_time) * 1000}ms",
                    "memory": f"{memory_usage}KB",
                }
            
            if (time.time() - start_time) * 1000 > time_limit:
                process.kill()
                return {
                    "verdict": "TLE",
                    "time": f"{(time.time() - start_time) * 1000}ms",
                    "memory": f"{memory_usage}KB",
                }

        stdout, stderr = process.communicate()

        execution_time = (time.time() - start_time) * 1000

        if process.returncode != 0:
            return {
                "verdict": "RE",
                "time": f"{execution_time}ms",
                "memory": f"{memory_usage}KB",
                "error": stderr,
            }

        if stderr:
            return {
                "verdict": "RE",
                "time": f"{execution_time}ms",
                "memory": f"{memory_usage}KB",
                "error": stderr,
            }
        
        if normalize_output(stdout) == normalize_output(expected_output.read()):
            return {
                "verdict": "AC",
                "time": f"{execution_time}ms",
                "memory": f"{memory_usage}KB",
            }
        
        return {
            "verdict": "WA",
            "time": f"{execution_time}ms",
            "memory": f"{memory_usage}KB",
        }
    except Exception as error:
        process.kill()
        return {
            "verdict": "RE",
            "error": str(error),
        }
    

def judge(id: int, problem_id: int, testcases: int, language: str):
    submission[id] = "Compiling"
    compile_result = compile(id, language)
    if compile_result:
        submission[id] = "Finished"
        return {
            "verdict": "CE",
            "error": compile_result,
        }

    if not os.path.exists(f"testcases/{problem_id}") or not os.listdir(f"testcases/{problem_id}"):
        submission[id] = "Finished"
        return {
            "verdict": "JE",
            "error": "No testcases found",
        }
    
    result = []

    for i in range(testcases):
        testcase_valid = os.path.exists(f"testcases/{problem_id}/{i + 1}.in") and os.path.exists(f"testcases/{problem_id}/{i + 1}.sol")
        if not testcase_valid:
            submission[id] = "Finished"
            return {
                "verdict": "JE",
                "error": f"Testcase {i + 1} is missing",
            }

        submission[id] = f"Running testcase {i + 1}"
        execute_result = execute(id, problem_id, language, i + 1)

        result.append({
            "testcase": i + 1,
            "verdict": execute_result["verdict"],
            "time": execute_result.get("time"),
            "memory": execute_result.get("memory"),
            "error": execute_result.get("error"),
        })

    submission[id] = "Finished"
    return result

