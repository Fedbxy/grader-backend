path = "tmp"

def compile(id: int, language: str):
    commands = {
        "cpp": ["g++", "-std=c++17", "-O2", f"{path}/{id}.cpp", "-o", f"{path}/{id}"],
        "c": ["gcc", "-std=c11", "-O2", f"{path}/{id}.c", "-o", f"{path}/{id}"],
        "py": ["true"],
    }

    return commands[language]

def execute(id: int, language: str):
    commands = {
        "cpp": [f"{path}/{id}"],
        "c": [f"{path}/{id}"],
        "py": ["python3", "-B", f"{path}/{id}.py"],
    }

    return commands[language]