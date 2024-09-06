import subprocess

def initIsolate(id: int):
    process = subprocess.Popen(f"isolate --box-id={id} --init", shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    stdout, stderr = process.communicate()
    path = stdout.strip()
    error = stderr.strip()
    returnCode = process.returncode

    if returnCode != 0:
        raise Exception(f"Couldn't initialize isolate: {error}")
    
    return f"{path}/box"


def cleanupIsolate(id: int):
    process = subprocess.Popen(f"isolate --box-id={id} --cleanup", shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    _, stderr = process.communicate()
    error = stderr.strip()
    returnCode = process.returncode

    if returnCode != 0:
        raise Exception(f"Couldn't cleanup isolate: {error}")


def readMetaFile(path: str):
    with open(path) as file:
        lines = file.readlines()
        return {line.split(":")[0].strip(): line.split(":")[1].strip() for line in lines}
