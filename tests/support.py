"""What the tests share: the declaration, the sandbox flags and test inputs."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
NAME = "primitive-compile"
SIBLING_SCHEMA = ROOT.parent / "runner" / "schemas" / "primitive.schema.json"

RunImage = Callable[..., dict[str, Any]]
Check = Callable[[dict[str, Any], str], None]

SOURCES = {
    "python": ("main.py", "print(int(input()) * 2)\n"),
    "c": (
        "main.c",
        '#include <stdio.h>\nint main(void) { int n; scanf("%d", &n);'
        ' printf("%d\\n", n * 2); return 0; }\n',
    ),
    "cpp": (
        "main.cpp",
        "#include <bits/stdc++.h>\nint main() { long long n; std::cin >> n;"
        ' std::cout << n * 2 << "\\n"; }\n',
    ),
    "java": (
        "Solution.java",
        "package contest.a;\nimport java.util.*;\n"
        "public class Solution {\n  public static void main(String[] args) {\n"
        "    System.out.println(new Scanner(System.in).nextInt() * 2);\n  }\n}\n",
    ),
}

FOLDERS: dict[str, tuple[dict[str, str], str | None]] = {
    "c": (
        {
            "main.c": '#include <stdio.h>\n#include "lib/twice.h"\n'
            'int main(void) { int n; scanf("%d", &n);'
            ' printf("%d\\n", twice(n)); return 0; }\n',
            "lib/twice.h": "int twice(int n);\n",
            "lib/twice.c": '#include "twice.h"\nint twice(int n) { return 2 * n; }\n',
            "notes.txt": "not a source\n",
        },
        None,
    ),
    "cpp": (
        {
            "solution.cpp": "#include <iostream>\nint twice(int n);\n"
            "int main() { int n; std::cin >> n; std::cout << twice(n) << '\\n'; }\n",
            "src/twice.cc": "int twice(int n) { return 2 * n; }\n",
            "test/driver.cpp": "int main() { return 1; }\n",
        },
        "solution.cpp",
    ),
    "java": (
        {
            "com/example/Main.java": "package com.example;\n"
            "import com.example.util.Twice;\n"
            "public class Main {\n  public static void main(String[] args) {\n"
            "    int n = new java.util.Scanner(System.in).nextInt();\n"
            "    System.out.println(Twice.of(n));\n  }\n}\n",
            "com/example/util/Twice.java": "package com.example.util;\n"
            "public class Twice { public static int of(int n) { return 2 * n; } }\n",
        },
        None,
    ),
    "python": (
        {
            "app/main.py": "import sys\nfrom twice import twice\n"
            "print(twice(int(sys.stdin.readline())))\n",
            "app/twice.py": "def twice(n):\n    return 2 * n\n",
            "tools/check.py": 'if __name__ == "__main__":\n    print("not this one")\n',
        },
        "app/main.py",
    ),
}
"""A program of several files in nested folders for each language, and the
entry it names, None when it is found without one. Each doubles the number it
reads."""

BROKEN = {
    "python": ("main.py", "print(int(input()) * 2\n", "was never closed"),
    "c": ("main.c", "int main(void) { return x; }\n", "main.c:1:"),
    "cpp": ("main.cpp", "int main() { return x; }\n", "main.cpp:1:"),
    "java": (
        "Main.java",
        "public class Main { void f() { int x = } }\n",
        "Main.java:1:",
    ),
}


def declaration() -> dict[str, Any]:
    """This repo's primitive.yaml."""
    document: dict[str, Any] = yaml.safe_load((ROOT / "primitive.yaml").read_text())
    return document


def sandbox_flags(limits: dict[str, int]) -> list[str]:
    """The docker run flags the harness gives a step container."""
    memory = f"{limits['memory_mb']}m"
    return [
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--security-opt=seccomp=builtin",
        "--user=65532:65532",
        f"--memory={memory}",
        f"--memory-swap={memory}",
        f"--pids-limit={limits['pids']}",
        "--cpus=1",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
    ]


def open_up(work: Path) -> None:
    """Let user 65532 in the container write the working directory and read `in/`."""
    for path in [work, *work.rglob("*")]:
        path.chmod(0o777 if path.is_dir() else 0o666)


def step_inputs(
    work: Path, files: dict[str, str], language: str, entry: str | None = None
) -> None:
    """Write inputs.json and the source folder, `files` by path, for one compile.

    The folder is `in/1/source`, where the harness places a folder input, and
    a single uploaded file as a folder holding that one file.
    """
    folder = work / "in" / "1" / "source"
    folder.mkdir(parents=True)
    for name, text in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, newline="\n")
    inputs: dict[str, object] = {
        "source": {"folder": "in/1/source"},
        "language": language,
    }
    if entry is not None:
        inputs["entry"] = entry
    document = {"schema_version": 5, "inputs": inputs}
    (work / "inputs.json").write_text(json.dumps(document))
