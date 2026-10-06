"""The program's own logic, run in-process.

Compiling with the real toolchain is covered by the image tests; the tests
here that start processes need Linux, and the ones that call a compiler also
need it installed.
"""

import errno
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

import compiler
from support import BROKEN, FOLDERS, SOURCES, step_inputs


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
    """Run a Python binary the way sandbox-run does and return what it printed."""
    return subprocess.run(
        [sys.executable, "-I", "-B", str(work / "out" / "binary")],
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
        ({"schema_version": 4, "inputs": {}}, "contract version 5"),
        ({"schema_version": 5, "batch": []}, "no inputs object"),
        (
            {"schema_version": 5, "inputs": {"language": "c"}},
            "the input named source is not a folder",
        ),
        (
            {
                "schema_version": 5,
                "inputs": {"source": {"file": "in/1/main.c"}, "language": "c"},
            },
            "the input named source is not a folder",
        ),
        (
            {
                "schema_version": 5,
                "inputs": {"source": {"folder": "in/.."}, "language": "c"},
            },
            "the input named source is outside in/",
        ),
        (
            {
                "schema_version": 5,
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
        "binary": {"file": "out/binary"},
        "compile_log": "",
        "outcome": "accepted",
    }
    binary = tmp_path / "out" / "binary"
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
    with zipfile.ZipFile(tmp_path / "out" / "binary") as archive:
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
    assert result["outputs"]["compile_log"].endswith("larger than 32 MB\n")
    assert not (tmp_path / "out" / "binary").exists()


def test_python_that_does_not_compile_is_a_compile_error(tmp_path: Path) -> None:
    """A syntax error is an outcome with the interpreter's message, and no binary."""
    name, text, expected = BROKEN["python"]
    step_inputs(tmp_path, {name: text}, "python")
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "compile_error"
    assert expected in result["outputs"]["compile_log"]
    assert "binary" not in result["outputs"]
    assert not (tmp_path / "out" / "binary").exists()


def test_every_python_source_is_checked(tmp_path: Path) -> None:
    """A broken file the entry never imports is still a compile error."""
    files = {"main.py": "print(1)\n", "unused/broken.py": "def f(:\n"}
    step_inputs(tmp_path, files, "python", "main.py")
    result = outputs(tmp_path)
    assert result["outputs"]["outcome"] == "compile_error"
    assert "broken.py" in result["outputs"]["compile_log"]


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
    assert result["outputs"]["compile_log"].startswith(line)
    assert "binary" not in result["outputs"]


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
        "compile_log": "the source folder could not be copied to compile it:"
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
    assert (tmp_path / "out" / "binary").is_file()


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
    assert (tmp_path / "out" / "binary").is_file()
