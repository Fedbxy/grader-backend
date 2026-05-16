LANGUAGE_REGISTRY = {
    "cpp": {
        "extension": "cpp",
        "compile": lambda path, id: ["/usr/bin/g++", "-std=c++17", "-O2", f"{path}/{id}.cpp", "-o", f"{path}/{id}"],
        "execute": lambda id: f"./{id}",
        "time_multiplier": 1,
        "memory_multiplier": 1,
    },
    "c": {
        "extension": "c",
        "compile": lambda path, id: ["/usr/bin/gcc", "-std=c11", "-O2", f"{path}/{id}.c", "-o", f"{path}/{id}"],
        "execute": lambda id: f"./{id}",
        "time_multiplier": 1,
        "memory_multiplier": 1,
    },
    "py": {
        "extension": "py",
        "compile": lambda path, id: ["/usr/local/bin/python3", "-m", "py_compile", f"{path}/{id}.py"],
        "execute": lambda id: f"/usr/local/bin/python3 -B {id}.py",
        "time_multiplier": 3,
        "memory_multiplier": 1,
    },
    "pypy": {
        "extension": "py",
        "compile": lambda path, id: ["/usr/bin/pypy3", "-m", "py_compile", f"{path}/{id}.py"],
        "execute": lambda id: f"/usr/bin/pypy3 -B {id}.py",
        "time_multiplier": 1,
        "memory_multiplier": 2,
    }
}
