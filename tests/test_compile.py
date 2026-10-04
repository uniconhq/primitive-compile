"""The program's own logic, run in-process.

Compiling with the real toolchain is covered by the image tests; the tests
here that start processes need Linux, and the ones that call a compiler also
need it installed.
"""

import json
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

import compiler
from support import BROKEN, SOURCES, step_inputs


def outputs(work: Path) -> dict[str, Any]:
    """Run the program over a working directory and read outputs.json."""
    assert compiler.main(["compile", str(work)]) == 0
    document: dict[str, Any] = json.loads(
        (work / "outputs.json").read_text(encoding="utf-8")
    )
    return document


def has_tool(name: str) -> bool:
    """Whether a compiler is on the path the program uses."""
    return shutil.which(name, path=compiler.TOOL_PATH) is not None


@pytest.mark.parametrize(
    ("text", "package", "types"),
    [
        (
            "package a.b;\npublic class Solution { public static class Inner {} }",
            "a.b",
            [("Solution", True)],
        ),
        (
            "/* public class Fake {} */\n// public class Other\n"
            "class Main { String s = \"public class Nope {\"; char c = '{'; }\n"
            "record Pair(int a, int b) {}\nenum Colour { RED }\n",
            "",
            [("Main", False), ("Pair", False), ("Colour", False)],
        ),
        (
            'class Helper { public static class Main {} String t = """\n'
            'public class InText {\n"""; }\npublic final class Main {}\n',
            "",
            [("Helper", False), ("Main", True)],
        ),
        ("package  x . y ;\ninterface Shape {}\n", "x.y", [("Shape", False)]),
        ("", "", []),
    ],
)
def test_java_declarations(
    text: str, package: str, types: list[tuple[str, bool]]
) -> None:
    """Only top-level types count, and comments and strings never do."""
    assert compiler.java_declarations(text) == (package, types)


@pytest.mark.parametrize(
    ("name", "suffix", "used"),
    [
        ("main.cpp", ".cpp", "main.cpp"),
        ("solution_2.c", ".c", "solution_2.c"),
        ("-o.c", ".c", "main.c"),
        ("my file.py", ".py", "main.py"),
        ("prog.c", ".cpp", "main.cpp"),
        (".hidden.py", ".py", "main.py"),
    ],
)
def test_source_name(name: str, suffix: str, used: str) -> None:
    """The contestant's file name is kept when it cannot be read as an option."""
    assert compiler.source_name(Path("in") / name, suffix) == used


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (None, "inputs.json is not in the working directory"),
        ("{", "inputs.json is not valid JSON"),
        ("[]", "inputs.json is not a JSON object"),
        ({"schema_version": 2, "inputs": {}}, "contract version 4"),
        ({"schema_version": 4, "batch": []}, "no inputs object"),
        (
            {"schema_version": 4, "inputs": {"language": "c"}},
            "the input named source is not a file",
        ),
        (
            {
                "schema_version": 4,
                "inputs": {"source": {"file": "in/../inputs.json"}, "language": "c"},
            },
            "the input named source is outside in/",
        ),
        (
            {
                "schema_version": 4,
                "inputs": {"source": {"file": "in/nothing.c"}, "language": "c"},
            },
            "the input named source is not in the working directory",
        ),
    ],
)
def test_inputs_it_cannot_use_are_an_error(
    tmp_path: Path, document: object, message: str
) -> None:
    """Anything that stops the primitive working is `error` alone, in one sentence."""
    if isinstance(document, str):
        (tmp_path / "inputs.json").write_text(document)
    elif document is not None:
        (tmp_path / "inputs.json").write_text(json.dumps(document))
    result = outputs(tmp_path)
    assert set(result) == {"schema_version", "error"}
    assert message in result["error"]


def test_an_unknown_language_is_an_error(tmp_path: Path) -> None:
    """A language outside the declared list is refused before anything runs."""
    step_inputs(tmp_path, "main.rs", "fn main() {}\n", "rust")
    assert "python, c, cpp, java" in outputs(tmp_path)["error"]


def test_the_log_is_cut_at_its_limit(tmp_path: Path) -> None:
    """A compiler that prints without end leaves a log of bounded size."""
    command = [sys.executable, "-c", "import sys; sys.stdout.write('e' * 300000)"]
    tool = compiler.run_tool(command, tmp_path, limit_address_space=True)
    assert tool.ok
    assert tool.log.startswith("e" * compiler.LOG_LIMIT + "\n[compile log cut at")
    assert f"{300000 - compiler.LOG_LIMIT} more bytes left out" in tool.log


def test_a_compiler_that_runs_too_long_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Going over the compile time is a failed compile with a note in the log."""
    monkeypatch.setattr(compiler, "COMPILE_SECONDS", 1)
    command = [
        sys.executable,
        "-c",
        "import time; print('started', flush=True); time.sleep(30)",
    ]
    tool = compiler.run_tool(command, tmp_path, limit_address_space=True)
    assert not tool.ok
    assert (
        tool.log == "started\n\ncompiling took longer than 1 seconds and was stopped\n"
    )


def test_python_compiles_to_a_zip_application(tmp_path: Path) -> None:
    """The binary holds the source as `__main__.py` behind a shebang line."""
    name, text = SOURCES["python"]
    step_inputs(tmp_path, name, text, "python")
    result = outputs(tmp_path)
    assert result["outputs"] == {
        "binary": {"file": "out/binary"},
        "compile_log": "",
        "outcome": "accepted",
    }
    binary = tmp_path / "out" / "binary"
    assert binary.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(binary) as archive:
        assert archive.read("__main__.py").decode() == text


def test_python_that_does_not_compile_is_a_compile_error(tmp_path: Path) -> None:
    """A syntax error is an outcome with the interpreter's message, and no binary."""
    name, text, expected = BROKEN["python"]
    step_inputs(tmp_path, name, text, "python")
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "compile_error"
    assert expected in result["outputs"]["compile_log"]
    assert "binary" not in result["outputs"]
    assert not (tmp_path / "out" / "binary").exists()


@pytest.mark.parametrize("language", ["c", "cpp", "java"])
def test_compiled_languages(tmp_path: Path, language: str) -> None:
    """Each compiled language gives a binary, where its compiler is installed."""
    tool = {"c": "gcc", "cpp": "g++", "java": "javac"}[language]
    if not has_tool(tool):
        pytest.skip(f"{tool} is not installed")
    name, text = SOURCES[language]
    step_inputs(tmp_path, name, text, language)
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "accepted", result
    assert (tmp_path / "out" / "binary").is_file()
