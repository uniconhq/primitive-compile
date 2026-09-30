"""The image, run on Docker under the harness's sandbox flags.

Every language compiles a small program, and the binary is then run from the
same image the way sandbox-run runs it, to see that it works.
"""

import json
import subprocess
from pathlib import Path

import pytest

from support import (
    BROKEN,
    SOURCES,
    Check,
    RunImage,
    declaration,
    sandbox_flags,
    step_inputs,
)

pytestmark = pytest.mark.image

LAUNCH = {
    "c": (["--entrypoint", "/work/out/binary"], []),
    "cpp": (["--entrypoint", "/work/out/binary"], []),
    "python": (["--entrypoint", "python3"], ["-I", "/work/out/binary"]),
    "java": (
        ["--entrypoint", "java"],
        ["-XX:-UsePerfData", "-jar", "/work/out/binary"],
    ),
}


def run_binary(image: str, work: Path, language: str, stdin: str) -> str:
    """Run a compiled binary in the sandbox and return what it printed."""
    entrypoint, arguments = LAUNCH[language]
    command = ["docker", "run", "--rm", "-i", *sandbox_flags(declaration()["limits"])]
    command += [*entrypoint, "--volume", f"{work}:/work", image, *arguments]
    return subprocess.run(
        command, input=stdin, capture_output=True, text=True, check=True, timeout=120
    ).stdout


@pytest.mark.parametrize("language", ["python", "c", "cpp", "java"])
def test_each_language_compiles_to_a_binary_that_runs(
    tmp_path: Path, image: str, run_image: RunImage, check: Check, language: str
) -> None:
    """A good source gives `accepted`, a quiet log and a binary that works."""
    name, text = SOURCES[language]
    step_inputs(tmp_path, name, text, language)
    check(json.loads((tmp_path / "inputs.json").read_text()), "inputs_file")
    result = run_image(tmp_path)
    check(result, "outputs_file")
    assert result["outputs"] == {
        "binary": {"file": "out/binary"},
        "compile_log": "",
        "outcome": "accepted",
    }
    assert run_binary(image, tmp_path, language, "21\n") == "42\n"


@pytest.mark.parametrize("language", ["python", "c", "cpp", "java"])
def test_a_source_that_does_not_compile_is_a_compile_error(
    tmp_path: Path, run_image: RunImage, check: Check, language: str
) -> None:
    """A broken source is an outcome with the compiler's log and no binary."""
    name, text, expected = BROKEN[language]
    step_inputs(tmp_path, name, text, language)
    result = run_image(tmp_path)
    check(result, "outputs_file")
    assert result["outputs"]["outcome"] == "compile_error"
    assert expected in result["outputs"]["compile_log"]
    assert "binary" not in result["outputs"]
    assert not (tmp_path / "out" / "binary").exists()


def test_a_huge_compile_log_is_cut(tmp_path: Path, run_image: RunImage) -> None:
    """Thousands of errors still give a log of bounded size."""
    text = "".join(f"int f{n}(void) {{ return y{n}; }}\n" for n in range(3000))
    step_inputs(tmp_path, "main.c", text, "c")
    result = run_image(tmp_path)
    log = result["outputs"]["compile_log"]
    assert result["outputs"]["outcome"] == "compile_error"
    assert "[compile log cut at 65536 bytes;" in log
    assert len(log.encode()) < 65536 + 200


def test_a_java_class_that_is_not_public_runs_as_main(
    tmp_path: Path, image: str, run_image: RunImage
) -> None:
    """With no public class, the top-level class named Main is the one run."""
    text = (
        "class Helper { static int twice(int n) { return 2 * n; } }\n"
        "class Main { public static void main(String[] a) {"
        " System.out.println(Helper.twice(new java.util.Scanner(System.in).nextInt()));"
        " } }\n"
    )
    step_inputs(tmp_path, "whatever.java", text, "java")
    assert run_image(tmp_path)["outputs"]["outcome"] == "accepted"
    assert run_binary(image, tmp_path, "java", "5\n") == "10\n"


def test_a_public_class_too_long_to_name_a_file_is_a_compile_error(
    tmp_path: Path, run_image: RunImage, check: Check
) -> None:
    """The contestant's mistake, never a step that could not work."""
    name = "A" * 300
    text = f"public class {name} {{ public static void main(String[] a) {{}} }}\n"
    step_inputs(tmp_path, "Main.java", text, "java")
    result = run_image(tmp_path)
    check(result, "outputs_file")
    assert result["outputs"]["outcome"] == "compile_error"


def test_a_file_name_that_looks_like_an_option_is_compiled_safely(
    tmp_path: Path, image: str, run_image: RunImage
) -> None:
    """A source named like a compiler flag is compiled under a plain name."""
    _, text = SOURCES["c"]
    step_inputs(tmp_path, "-o.c", text, "c")
    assert run_image(tmp_path)["outputs"]["outcome"] == "accepted"
    assert run_binary(image, tmp_path, "c", "4\n") == "8\n"
