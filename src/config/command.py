def compile(isolatePath: str, id: int, language: str):
    commands = {
        "cpp": ["/usr/bin/g++", "-std=c++17", "-O2", f"{isolatePath}/{id}.cpp", "-o", f"{isolatePath}/{id}"],
        "c": ["/usr/bin/gcc", "-std=c11", "-O2", f"{isolatePath}/{id}.c", "-o", f"{isolatePath}/{id}"],
        "py": ["/usr/local/bin/python3", "-m", "py_compile", f"{isolatePath}/{id}.py"],
    }

    return commands[language]

def execute(id: int, language: str):
    commands = {
        "cpp": f"./{id}",
        "c": f"./{id}",
        "py": f"/usr/local/bin/python3 -B {id}.py",
    }

    return commands[language]