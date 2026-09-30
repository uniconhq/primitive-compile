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


def step_inputs(work: Path, name: str, text: str, language: str) -> None:
    """Write inputs.json and the source for one compile."""
    source = work / "in" / "1" / name
    source.parent.mkdir(parents=True)
    source.write_text(text)
    document = {
        "schema_version": 3,
        "step": "compile",
        "inputs": {"source": {"file": f"in/1/{name}"}, "language": language},
    }
    (work / "inputs.json").write_text(json.dumps(document))
