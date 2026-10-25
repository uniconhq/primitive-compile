"""The program's own logic, run in-process.

Compiling with the real toolchain is covered by the image tests; the tests
here that start processes need Linux, and the ones that call a compiler also
need it installed.
"""

import errno
import json
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

import compiler
from support import (
    BROKEN,
    FOLDERS,
    SOURCES,
    declaration,
    returned_outcomes,
    step_inputs,
)


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


def run_python(work: Path, stdin: str) -> str:
    """Run a Python binary the way exec does and return what it printed."""
    return subprocess.run(
        [sys.executable, "-I", "-B", str(work / "out" / "program")],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


def make_folder(root: Path, files: dict[str, str]) -> Path:
    """Write files by path under a folder and return it."""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


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
        (
            "/* c */ package p;\npublic  final\nclass Solution {}\npublic void x;\n"
            'final public class Twice {}\nclass Open { String t = """\n}',
            "p",
            [("Solution", True), ("Twice", True), ("Open", False)],
        ),
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
    ("path", "used"),
    [
        ("main.c", "main.c"),
        ("src/a.c", "src/a.c"),
        ("_util.py", "_util.py"),
        ("-o.c", "./-o.c"),
        ("@args.java", "./@args.java"),
        (".hidden/a.c", "./.hidden/a.c"),
    ],
)
def test_argument(path: str, used: str) -> None:
    """A path a compiler could read as an option or an argument file is
    prefixed with `./`."""
    assert compiler.argument(path) == used


@pytest.mark.parametrize(
    ("language", "text", "holds"),
    [
        ("c", "int main(void) { return 0; }\n", True),
        ("c", "int main(int argc, char **argv)\n{\n  return 0;\n}\n", True),
        ("c", "int main(void);\nint helper(void) { return 1; }\n", False),
        ("c", "/* int main(void) { } */\nint f(void) { return 0; }\n", False),
        ("c", 'const char *s = "int main() {";\n', False),
        ("cpp", "auto main() -> int { return 0; }\n", True),
        ("cpp", "struct Game { void run(); };\nvoid Game::main() {}\n", False),
        ("cpp", "int f(Game g) { g.main(); return 0; }\n", False),
        ("java", "class A { public static void main(String[] a) {} }", True),
        ("java", "class A { static public void main(String... a) {} }", True),
        ("java", "class A { public void main(String[] a) {} }", False),
        ("java", "class A { // public static void main(String[] a)\n}", False),
        ("java", "class A { final public\n  static void main (String[] a) {} }", True),
        ("java", 'class A { String s = """\nstatic void main(\n"""; }', False),
        ("java", "class A { static int main(String[] a) {} }", False),
        ("c", "int main(void) { /* left open", True),
        ("c", "/* left open\nint main(void) { return 0; }\n", False),
        ("c", 'char *s = "open\nint main(void) { return 0; }\n', True),
        ("cpp", 'int main(int a = f("{")) { return 0; }\n', True),
        ("c", "int main(void) /* x */ ;\n", False),
        ("python", 'if __name__ == "__main__":\n    main()\n', True),
        ("python", "if '__main__' == __name__:\n    main()\n", True),
        ("python", "def main():\n    pass\nmain()\n", False),
        ("python", '    if __name__ == "__main__":\n        pass\n', False),
    ],
)
def test_holds_main(tmp_path: Path, language: str, text: str, holds: bool) -> None:
    """A file holds main by the language's own sign of a program's start."""
    path = tmp_path / "source"
    path.write_text(text)
    assert compiler.holds_main(path, language) is holds


@pytest.mark.parametrize(
    ("text", "language", "code"),
    [
        ("a /* b */ c // d\ne", "c", "a   c  \ne"),
        ('x = "a\\"b"; y', "c", "x =  ; y"),
        ("c = '\\''; d", "java", "c =  ; d"),
        ('s = "open\nnext', "c", "s =  \nnext"),
        ("a /* open", "c", "a  "),
        ("a / b // end", "c", "a / b  "),
        ('t = """\nq " "" \\""" x\n"""; u', "java", "t =  ; u"),
        ('t = """ open /* x', "java", "t =  "),
        ('a """b""" c', "c", "a     c"),
    ],
)
def test_code_only(text: str, language: str, code: str) -> None:
    """Each comment and literal becomes one space; one left open runs to the
    end of its line, or for a block comment or text block to the end."""
    assert compiler.code_only(text, language) == code


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("c", "/*a" * 40000),
        ("java", "/*a" * 40000),
        ("java", 'class A { String s = """\n' + "a" * 200_000),
        ("java", 'class A { String s = """' + '""\\"' * 50_000),
        ("c", "'" * 200_000),
        ("java", "public " * 30_000),
        ("java", "static " * 30_000),
        ("java", "\n" * 200_000),
        ("java", "package " + "a." * 100_000),
        ("c", "main()" * 40_000),
    ],
)
def test_reading_a_source_takes_linear_time(
    tmp_path: Path, language: str, text: str
) -> None:
    """Sources shaped to make a backtracking search slow are read in well
    under a second."""
    path = tmp_path / "source"
    path.write_text(text)
    started = time.monotonic()
    compiler.holds_main(path, language)
    if language == "java":
        compiler.java_declarations(text)
    assert time.monotonic() - started < 1


def test_reading_the_folder_past_its_time_is_a_compile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preparing the folder is stopped at its deadline as a failed compile with
    a line in the log, and the timer is cleared after."""
    monkeypatch.setattr(compiler, "PREPARE_SECONDS", 1)

    def slow(folder: Path) -> list[str]:
        time.sleep(30)
        return []

    monkeypatch.setattr(compiler, "folder_files", slow)
    step_inputs(tmp_path, {"main.py": "pass\n"}, "python")
    started = time.monotonic()
    result = outputs(tmp_path)
    assert time.monotonic() - started < 10
    assert result["outputs"] == {
        "log": "reading the source folder took longer than 1 seconds and was stopped\n",
        "outcome": "compile_error",
    }
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == signal.SIG_DFL


def test_folder_files_lists_regular_files_by_sorted_path(tmp_path: Path) -> None:
    """Nested files are listed by their path; links are left out."""
    folder = make_folder(tmp_path / "f", {"b.c": "", "a/z.c": "", "a/b/y.h": ""})
    (folder / "link.c").symlink_to(folder / "b.c")
    (folder / "linked").symlink_to(folder / "a", target_is_directory=True)
    assert compiler.folder_files(folder) == ["a/b/y.h", "a/z.c", "b.c"]


def test_an_empty_folder_is_a_compile_error(tmp_path: Path) -> None:
    """A folder with nothing in it is the contestant's to fix."""
    (tmp_path / "f").mkdir()
    with pytest.raises(compiler.CompileError, match="holds no files"):
        compiler.folder_files(tmp_path / "f")


def test_a_folder_of_too_many_files_is_a_compile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The number of files and folders is bounded before anything is copied."""
    monkeypatch.setattr(compiler, "FOLDER_LIMIT", 3)
    folder = make_folder(tmp_path / "f", {f"{n}.c": "" for n in range(4)})
    with pytest.raises(compiler.CompileError, match="more than 3 files and folders"):
        compiler.folder_files(folder)


@pytest.mark.parametrize(
    ("entry", "used"),
    [("src/main.py", "src/main.py"), ("./src/main.py", "src/main.py")],
)
def test_entry_path(entry: str, used: str) -> None:
    """The entry is a path relative to the folder."""
    assert compiler.entry_path(entry, ["src/main.py"]) == used


@pytest.mark.parametrize(
    "entry", ["main.py", "/src/main.py", "src/../src/main.py", "src", "."]
)
def test_an_entry_not_in_the_folder_is_a_compile_error(entry: str) -> None:
    """Only a file of the folder can be the entry."""
    with pytest.raises(compiler.CompileError, match="is not a file in the source"):
        compiler.entry_path(entry, ["src/main.py"])


@pytest.mark.parametrize(
    ("files", "language", "entry"),
    [
        ({"main.py": "x = 1\n", "data.txt": ""}, "python", "main.py"),
        (
            {"a.py": "", "b.py": 'if __name__ == "__main__":\n    pass\n'},
            "python",
            "b.py",
        ),
        ({"a.c": "int a;\n", "b/c.c": "int main(void) { return 0; }\n"}, "c", "b/c.c"),
        (
            {
                "A.java": "class A {}",
                "B.java": "class B { public static void main(String[] a) {} }",
            },
            "java",
            "B.java",
        ),
    ],
)
def test_find_entry(
    tmp_path: Path, files: dict[str, str], language: str, entry: str
) -> None:
    """With no entry named, the one source, or else the one holding main."""
    folder = make_folder(tmp_path / "f", files)
    sources = [name for name in files if name.endswith(compiler.EXTENSIONS[language])]
    assert compiler.find_entry(folder, sorted(sources), language) == entry


@pytest.mark.parametrize(
    ("files", "language", "message"),
    [
        (
            {"a.py": "", "b.py": ""},
            "python",
            'no Python source checks __name__ == "__main__"',
        ),
        (
            {"a.c": "int main(void) { return 0; }", "b.c": "int main() {}"},
            "c",
            "a.c, b.c each define main",
        ),
        ({"notes.txt": "", "data.txt": ""}, "java", "holds no Java source file"),
    ],
)
def test_an_entry_that_is_not_clear_is_a_compile_error(
    tmp_path: Path, files: dict[str, str], language: str, message: str
) -> None:
    """None or several candidates ask for the entry to be named."""
    folder = make_folder(tmp_path / "f", files)
    sources = [name for name in files if name.endswith(compiler.EXTENSIONS[language])]
    with pytest.raises(compiler.CompileError, match=message):
        compiler.find_entry(folder, sorted(sources), language)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (None, "inputs.json is not in the working directory"),
        ("{", "inputs.json is not valid JSON"),
        ("[]", "inputs.json is not a JSON object"),
        ({"schema_version": 4, "inputs": {}}, "contract version 6"),
        ({"schema_version": 5, "inputs": {}}, "contract version 6"),
        ({"schema_version": 6, "batch": []}, "no inputs object"),
        (
            {"schema_version": 6, "inputs": {"language": "c"}},
            "the input named source is not a folder",
        ),
        (
            {
                "schema_version": 6,
                "inputs": {"source": {"file": "in/1/main.c"}, "language": "c"},
            },
            "the input named source is not a folder",
        ),
        (
            {
                "schema_version": 6,
                "inputs": {"source": {"folder": "in/.."}, "language": "c"},
            },
            "the input named source is outside in/",
        ),
        (
            {
                "schema_version": 6,
                "inputs": {"source": {"folder": "in/nothing"}, "language": "c"},
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
    step_inputs(tmp_path, {"main.rs": "fn main() {}\n"}, "rust")
    assert "c, cpp, java, python" in outputs(tmp_path)["error"]


def test_an_entry_that_is_not_text_is_an_error(tmp_path: Path) -> None:
    """The entry port is text; anything else is the platform's fault."""
    step_inputs(tmp_path, {"main.py": "pass\n"}, "python")
    document = json.loads((tmp_path / "inputs.json").read_text())
    document["inputs"]["entry"] = 3
    (tmp_path / "inputs.json").write_text(json.dumps(document))
    assert outputs(tmp_path)["error"] == "the input named entry is not text"


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


def test_a_command_line_too_long_to_start_is_a_compile_error(tmp_path: Path) -> None:
    """Paths the contestant chose can make a command too long for the system."""
    command = [sys.executable, "-c", "pass", "x" * (256 * 1024)]
    tool = compiler.run_tool(command, tmp_path, limit_address_space=True)
    assert not tool.ok
    assert tool.log == "the source folder's paths are too long to compile\n"


def test_python_compiles_to_a_zip_application(tmp_path: Path) -> None:
    """The binary holds the source under `source/` and a launcher as
    `__main__.py`, behind a shebang line.
    """
    name, text = SOURCES["python"]
    step_inputs(tmp_path, {name: text}, "python")
    result = outputs(tmp_path)
    assert result["outputs"] == {
        "program": {"file": "out/program"},
        "log": "",
        "outcome": "accepted",
    }
    binary = tmp_path / "out" / "program"
    assert binary.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(binary) as archive:
        assert archive.read("source/main.py").decode() == text
        assert "__main__.py" in archive.namelist()
    assert run_python(tmp_path, "21\n") == "42\n"


def test_a_python_folder_keeps_its_layout(tmp_path: Path) -> None:
    """Every file goes in with its path, and the entry imports from its own
    folder.
    """
    files, entry = FOLDERS["python"]
    step_inputs(tmp_path, files, "python", entry)
    assert outputs(tmp_path)["outputs"]["outcome"] == "accepted"
    with zipfile.ZipFile(tmp_path / "out" / "program") as archive:
        names = set(archive.namelist())
    assert names == {"__main__.py", *(f"source/{name}" for name in files)}
    assert run_python(tmp_path, "21\n") == "42\n"


def test_a_python_entry_runs_as_the_main_module(tmp_path: Path) -> None:
    """The entry sees itself as `__main__`, with its own path as `__file__`."""
    files = {
        "pkg/__init__.py": "",
        "pkg/helper.py": "VALUE = 7\n",
        "run.py": "import sys\nfrom pkg.helper import VALUE\n"
        'if __name__ == "__main__":\n'
        "    print(VALUE, __file__.endswith('/source/run.py'),"
        " sys.modules['__main__'].__file__ == __file__)\n",
    }
    step_inputs(tmp_path, files, "python")
    assert outputs(tmp_path)["outputs"]["outcome"] == "accepted"
    assert run_python(tmp_path, "") == "7 True True\n"


def test_a_binary_over_the_limit_is_a_compile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zip application is written by the primitive itself, so its size is
    checked against the limit here.
    """

    def too_large(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EFBIG, "File too large")

    monkeypatch.setattr(compiler, "write_zip_application", too_large)
    name, text = SOURCES["python"]
    step_inputs(tmp_path, {name: text}, "python")
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "compile_error"
    assert result["outputs"]["log"].endswith("program would be larger than 32 MB\n")
    assert not (tmp_path / "out" / "program").exists()


def test_the_declaration_says_what_each_outcome_it_reports_means(
    tmp_path: Path,
) -> None:
    """The outcome port's outcomes are exactly the ones a compile gives, each
    with a line saying when, for the workflow editor's drawer.
    """
    reported = set()
    for case, (name, text) in (
        ("good", SOURCES["python"]),
        ("broken", BROKEN["python"][:2]),
    ):
        step_inputs(tmp_path / case, {name: text}, "python")
        reported.add(outputs(tmp_path / case)["outputs"]["outcome"])

    outcomes = declaration()["outputs"]["outcome"]["outcomes"]

    assert list(outcomes) == ["accepted", "compile_error"]
    assert set(outcomes) == reported
    assert all(isinstance(when, str) and "\n" not in when for when in outcomes.values())


def test_the_declaration_says_every_outcome_a_compile_can_give() -> None:
    """The declared outcomes are every one `compile_source` can return, read
    from its source, so a new way to an outcome needs its line too.
    """
    outcomes = declaration()["outputs"]["outcome"]["outcomes"]

    assert set(outcomes) == returned_outcomes(compiler.compile_source)


def test_python_that_does_not_compile_is_a_compile_error(tmp_path: Path) -> None:
    """A syntax error is an outcome with the interpreter's message, and no binary."""
    name, text, expected = BROKEN["python"]
    step_inputs(tmp_path, {name: text}, "python")
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "compile_error"
    assert expected in result["outputs"]["log"]
    assert "program" not in result["outputs"]
    assert not (tmp_path / "out" / "program").exists()


def test_every_python_source_is_checked(tmp_path: Path) -> None:
    """A broken file the entry never imports is still a compile error."""
    files = {"main.py": "print(1)\n", "unused/broken.py": "def f(:\n"}
    step_inputs(tmp_path, files, "python", "main.py")
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "compile_error"
    assert "broken.py" in result["outputs"]["log"]


@pytest.mark.parametrize(
    ("files", "entry", "line"),
    [
        (
            {"a.py": 'if __name__ == "__main__": pass\n', "b.py": ""},
            "c.py",
            "the entry c.py is not a file in the source folder\n",
        ),
        (
            {"a.py": "", "notes.txt": ""},
            "notes.txt",
            "the entry notes.txt is not a Python source file (.py)\n",
        ),
        (
            {
                "a.py": 'if __name__ == "__main__": pass\n',
                "b.py": 'if __name__ == "__main__": pass\n',
            },
            None,
            'the entry is not clear: a.py, b.py each check __name__ == "__main__";'
            " name the file to start from as the entry\n",
        ),
        ({"main.py": ""}, "other.py", "the entry other.py is not a file"),
    ],
)
def test_an_entry_problem_is_a_compile_error(
    tmp_path: Path, files: dict[str, str], entry: str | None, line: str
) -> None:
    """A wrong or missing entry is the contestant's to fix: the log says how."""
    step_inputs(tmp_path, files, "python", entry)
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "compile_error"
    assert result["outputs"]["log"].startswith(line)
    assert "program" not in result["outputs"]


def test_an_empty_entry_is_no_entry(tmp_path: Path) -> None:
    """An entry of only spaces is taken as not given."""
    step_inputs(tmp_path, {"solve.py": "print(2)\n"}, "python", "  ")
    assert outputs(tmp_path)["outputs"]["outcome"] == "accepted"


def test_a_folder_that_cannot_be_copied_is_a_compile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder too large for the scratch space is the contestant's."""

    def full(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copyfile", full)
    step_inputs(tmp_path, {"a.py": "", "b.py": ""}, "python", "a.py")
    result = outputs(tmp_path)
    assert result["outputs"] == {
        "log": "the source folder could not be copied to compile it:"
        " No space left on device\n",
        "outcome": "compile_error",
    }


@pytest.mark.parametrize("language", ["c", "cpp", "java"])
def test_compiled_languages(tmp_path: Path, language: str) -> None:
    """Each compiled language gives a binary, where its compiler is installed."""
    tool = {"c": "gcc", "cpp": "g++", "java": "javac"}[language]
    if not has_tool(tool):
        pytest.skip(f"{tool} is not installed")
    name, text = SOURCES[language]
    step_inputs(tmp_path, {name: text}, language)
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "accepted", result
    assert (tmp_path / "out" / "program").is_file()


@pytest.mark.parametrize("language", ["c", "cpp", "java"])
def test_compiled_folders(tmp_path: Path, language: str) -> None:
    """A folder of nested sources gives one binary, where its compiler is
    installed.
    """
    tool = {"c": "gcc", "cpp": "g++", "java": "javac"}[language]
    if not has_tool(tool):
        pytest.skip(f"{tool} is not installed")
    files, entry = FOLDERS[language]
    step_inputs(tmp_path, files, language, entry)
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "accepted", result
    assert (tmp_path / "out" / "program").is_file()


def notebook(*cells: tuple[str, str | list[str]], **fields: Any) -> str:
    """A notebook of nbformat 4 holding `cells`, each its type and source."""
    document = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {"cell_type": kind, "metadata": {}, "source": source}
            | ({"outputs": [], "execution_count": None} if kind == "code" else {})
            for kind, source in cells
        ],
    }
    return json.dumps(document | fields)


THREE_CELLS = notebook(
    ("markdown", "# Doubling\nRead a number, print twice it."),
    ("code", ["import sys\n", "n = int(sys.stdin.readline())"]),
    ("code", "print(n * 2)"),
)


def test_a_notebook_runs_as_one_program(tmp_path: Path) -> None:
    """Its code cells, in order, are one Python file, packed as a Python
    program is; its markdown is passed over."""
    step_inputs(tmp_path, {"double.ipynb": THREE_CELLS}, "ipynb")
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "accepted", result
    assert run_python(tmp_path, "21\n") == "42\n"


def test_a_notebooks_code_names_each_cell(tmp_path: Path) -> None:
    code = compiler.notebook_code(THREE_CELLS, "double.ipynb")
    assert code == (
        "# coding: utf-8\n# cell 2\nimport sys\nn = int(sys.stdin.readline())\n"
        "# cell 3\nprint(n * 2)\n"
    )


def test_a_notebook_imports_the_modules_beside_it(tmp_path: Path) -> None:
    files = {
        "work/run.ipynb": notebook(
            ("code", "from twice import twice\nprint(twice(int(input())))")
        ),
        "work/twice.py": "def twice(n):\n    return 2 * n\n",
        "notes.txt": "kept\n",
    }
    step_inputs(tmp_path, files, "ipynb")
    assert outputs(tmp_path)["outputs"]["outcome"] == "accepted"
    assert run_python(tmp_path, "4\n") == "8\n"


def test_the_outputs_a_notebook_holds_are_passed_over(tmp_path: Path) -> None:
    text = json.loads(THREE_CELLS)
    text["cells"][2]["outputs"] = [
        {"output_type": "stream", "name": "stdout", "text": ["%pip install x\n"]}
    ]
    step_inputs(tmp_path, {"double.ipynb": json.dumps(text)}, "ipynb")
    assert outputs(tmp_path)["outputs"]["outcome"] == "accepted"
    assert run_python(tmp_path, "5\n") == "10\n"


@pytest.mark.parametrize(
    ("cell", "shown"),
    [
        ("%pip install numpy", "%pip install numpy"),
        ("import os\n!ls -la", "!ls -la"),
        ("%%time\nx = 1", "%%time"),
        ("for i in range(2):\n    %time f(i)", "%time f(i)"),
        ("a = 1\x0cb = 2\nc = 3\n%who", "%who"),
    ],
)
def test_a_magic_is_a_compile_error_naming_its_cell(
    tmp_path: Path, cell: str, shown: str
) -> None:
    """There is no IPython when the program runs, so a magic or a shell
    command could not run there."""
    text = notebook(("markdown", "intro"), ("code", cell), ("code", "print(1)"))
    step_inputs(tmp_path, {"run.ipynb": text}, "ipynb")
    result = outputs(tmp_path)["outputs"]
    assert result["outcome"] == "compile_error"
    assert result["log"] == (
        f"cell 2 of the notebook run.ipynb runs {shown!r}, which only IPython"
        " runs; there is none when the program runs\n"
    )


@pytest.mark.parametrize(
    "cell",
    [
        'print("%d" % 3)',
        "x = (10\n     % 3)\nprint(x)",
        'text = """\n!not a command\n%neither\n"""\nprint(len(text))',
        "# !ls in a comment\nprint(1)",
    ],
)
def test_a_percent_or_bang_python_reads_is_no_magic(tmp_path: Path, cell: str) -> None:
    step_inputs(tmp_path, {"run.ipynb": notebook(("code", cell))}, "ipynb")
    assert outputs(tmp_path)["outputs"]["outcome"] == "accepted"


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("{", "the notebook run.ipynb is not valid JSON"),
        ("[]", "the notebook run.ipynb is not a notebook of format 4"),
        (
            json.dumps({"nbformat": 3, "worksheets": []}),
            "the notebook run.ipynb is not a notebook of format 4",
        ),
        (
            json.dumps({"nbformat": 4, "cells": {}}),
            "the notebook run.ipynb is not a notebook of format 4",
        ),
        (
            notebook(("code", "print(1)"), ("code", 7)),  # type: ignore[arg-type]
            "cell 2 of the notebook run.ipynb has no source text",
        ),
        (
            notebook(("code", "print('\ud83d')")),
            "cell 1 of the notebook run.ipynb holds text that is not Unicode",
        ),
    ],
)
def test_a_notebook_that_is_not_one_is_a_compile_error(
    tmp_path: Path, text: str, line: str
) -> None:
    """What an entrant uploads is theirs, so a broken notebook is an outcome,
    never an `error`."""
    step_inputs(tmp_path, {"run.ipynb": text}, "ipynb")
    result = outputs(tmp_path)["outputs"]
    assert result == {"log": f"{line}\n", "outcome": "compile_error"}


def test_a_syntax_error_in_a_cell_is_a_compile_error_naming_the_notebook(
    tmp_path: Path,
) -> None:
    text = notebook(("code", "x = 1"), ("code", "print(x"))
    step_inputs(tmp_path, {"run.ipynb": text}, "ipynb")
    result = outputs(tmp_path)["outputs"]
    assert result["outcome"] == "compile_error"
    assert "run.ipynb" in result["log"]
    assert "was never closed" in result["log"]


def test_two_notebooks_need_the_entry_named(tmp_path: Path) -> None:
    files = {
        "a.ipynb": notebook(("code", "print('a')")),
        "b.ipynb": notebook(("code", "print('b')")),
    }
    step_inputs(tmp_path, files, "ipynb")
    result = outputs(tmp_path)["outputs"]
    assert result["log"] == (
        "the entry is not clear: the source folder holds 2 notebooks; name the"
        " one to run as the entry\n"
    )
    named = tmp_path / "named"
    named.mkdir()
    step_inputs(named, files, "ipynb", "b.ipynb")
    assert outputs(named)["outputs"]["outcome"] == "accepted"
    assert run_python(named, "") == "b\n"


def test_a_notebook_saved_with_a_byte_order_mark_compiles(tmp_path: Path) -> None:
    step_inputs(tmp_path, {"double.ipynb": "\ufeff" + THREE_CELLS}, "ipynb")
    assert outputs(tmp_path)["outputs"]["outcome"] == "accepted"


def test_a_coding_line_in_the_first_cell_does_not_change_how_it_is_read(
    tmp_path: Path,
) -> None:
    """The joined file is UTF-8 and says so on its first line, so a cell's
    `# -*- coding: latin-1 -*-`, its second, is passed over."""
    code = "# -*- coding: latin-1 -*-\nprint('caf\u00e9')"
    step_inputs(tmp_path, {"run.ipynb": notebook(("code", code))}, "ipynb")
    assert outputs(tmp_path)["outputs"]["outcome"] == "accepted"
    assert run_python(tmp_path, "") == "caf\u00e9\n"


def test_a_notebook_that_is_not_utf_8_is_a_compile_error(tmp_path: Path) -> None:
    step_inputs(tmp_path, {"run.ipynb": ""}, "ipynb")
    source = next(path for path in tmp_path.rglob("run.ipynb"))
    source.write_bytes(notebook(("code", "print('x')")).encode() + b"\xff")
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "compile_error"
    assert result["outputs"]["log"] == "the notebook run.ipynb is not UTF-8 text\n"


def test_a_notebook_over_32_mb_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = notebook(("code", "print(1)"))
    monkeypatch.setattr(compiler, "BINARY_LIMIT", len(text) - 1)
    step_inputs(tmp_path, {"run.ipynb": text}, "ipynb")
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "compile_error"
    assert result["outputs"]["log"] == "the notebook run.ipynb is larger than 32 MB\n"


def test_a_cell_that_is_not_an_object_is_passed_over() -> None:
    text = json.loads(notebook(("code", "print(1)")))
    text["cells"].insert(0, "stray")
    assert (
        compiler.notebook_code(json.dumps(text), "run.ipynb")
        == "# coding: utf-8\n# cell 2\nprint(1)\n"
    )


def test_a_broken_module_beside_a_notebook_fails_the_compile(tmp_path: Path) -> None:
    files = {"run.ipynb": notebook(("code", "print(1)")), "helper.py": "def f(:\n"}
    step_inputs(tmp_path, files, "ipynb")
    result = outputs(tmp_path)["outputs"]
    assert result["outcome"] == "compile_error"
    assert "helper.py" in result["log"]
