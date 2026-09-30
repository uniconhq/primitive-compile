#!/usr/local/bin/python3 -I
"""The compile primitive: one source file in, one binary and a compile log out.

The harness mounts a working directory at `/work` (or the directory named by
the first argument) holding `inputs.json` and the source under `in/`. This
program compiles the source for the language named in the inputs and writes
`outputs.json`, with the binary under `out/` when the compile succeeded.

A source that does not compile is an ordinary result: the outcome is
`compile_error` and the compile log says why. `error` in `outputs.json` is kept
for the cases where the primitive could not work at all, such as a missing
input.

The binary is one file the sandbox-run primitive runs: a statically linked
executable for C and C++, a Python zip application for Python and a runnable
jar for Java (see the README).
"""

import json
import os
import re
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipapp
import zipfile
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 3
LANGUAGES = ("python", "c", "cpp", "java")
LOG_LIMIT = 64 * 1024
BINARY_LIMIT = 32 * 1024 * 1024
COMPILE_SECONDS = 30
COMPILER_ADDRESS_SPACE = 768 * 1024 * 1024
PYTHON_LAUNCHER = "/usr/bin/env python3"
TOOL_PATH = "/usr/local/bin:/usr/bin:/bin"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
JAVA_NAME = r"[A-Za-z_$][A-Za-z0-9_$]*"
JAVA_FILE_NAME_LIMIT = 200
"""The longest public class name the source is saved under; a longer one could
not name a file, so javac is left to refuse it as a compile error.
"""
JAVA_NOISE = re.compile(
    r'"""(?:\\.|[^\\])*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\''
    r"|//[^\n]*|/\*.*?\*/",
    re.DOTALL,
)
JAVA_PACKAGE = re.compile(
    rf"^\s*package\s+({JAVA_NAME}(?:\s*\.\s*{JAVA_NAME})*)\s*;", re.M
)
JAVA_TOP_LEVEL = re.compile(
    r"[{}]|\b((?:(?:public|abstract|final|sealed|non-sealed|strictfp)\s+)*)"
    rf"(?:class|interface|enum|record)\s+({JAVA_NAME})"
)


class PrimitiveError(Exception):
    """The primitive could not do its work at all.

    The message is one sentence for a person and becomes `error` in
    `outputs.json`, which the harness turns into a `system_error` verdict.
    """


@dataclass(frozen=True)
class Tool:
    """The result of running one compiler: its exit code and its log.

    The exit code is None when the compiler was stopped for taking too long.
    """

    returncode: int | None
    log: str

    @property
    def ok(self) -> bool:
        """Whether the compiler finished and exited 0."""
        return self.returncode == 0


def main(argv: list[str]) -> int:
    """Compile what `inputs.json` names and write `outputs.json`.

    Exits 0 whenever `outputs.json` was written, error or not; the harness
    reads the outcome and any error from that file.
    """
    root = Path(argv[1]) if len(argv) > 1 else Path("/work")
    try:
        source, language = read_inputs(root)
        outputs = compile_source(root, source, language)
        document: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "outputs": outputs,
        }
    except PrimitiveError as error:
        print(f"compile: {error}", file=sys.stderr)
        document = {"schema_version": SCHEMA_VERSION, "error": str(error)}
    write_json(root / "outputs.json", document)
    return 0


def read_inputs(root: Path) -> tuple[Path, str]:
    """Read `inputs.json` and return the source file and the language."""
    try:
        document = json.loads((root / "inputs.json").read_bytes())
    except FileNotFoundError:
        raise PrimitiveError("inputs.json is not in the working directory") from None
    except ValueError:
        raise PrimitiveError("inputs.json is not valid JSON") from None
    if not isinstance(document, dict):
        raise PrimitiveError("inputs.json is not a JSON object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise PrimitiveError(
            f"inputs.json is not written against contract version {SCHEMA_VERSION}"
        )
    inputs = document.get("inputs")
    if not isinstance(inputs, dict):
        raise PrimitiveError("inputs.json has no inputs object")
    source = input_file(root, inputs, "source")
    language = inputs.get("language")
    if language not in LANGUAGES:
        raise PrimitiveError(f"the language input is not one of {', '.join(LANGUAGES)}")
    return source, str(language)


def input_file(root: Path, inputs: dict[str, object], name: str) -> Path:
    """Resolve a file input to a path, refusing anything outside `in/`."""
    value = inputs.get(name)
    if not isinstance(value, dict) or not isinstance(value.get("file"), str):
        raise PrimitiveError(f"the input named {name} is not a file")
    path = (root / str(value["file"])).resolve()
    if not path.is_relative_to((root / "in").resolve()):
        raise PrimitiveError(f"the input named {name} is outside in/")
    if not path.is_file():
        raise PrimitiveError(f"the input named {name} is not in the working directory")
    return path


def compile_source(root: Path, source: Path, language: str) -> dict[str, object]:
    """Compile `source` into `out/binary` and return the outputs.

    Everything the compilers write goes to a fresh directory under the
    temporary directory, removed afterwards, except the binary itself.
    """
    out = root / "out"
    out.mkdir(exist_ok=True)
    binary = out / "binary"
    binary.unlink(missing_ok=True)
    build = Path(tempfile.mkdtemp(prefix="compile-"))
    try:
        if language == "python":
            tool = compile_python(source, build, binary)
        elif language == "java":
            tool = compile_java(source, build, binary)
        else:
            tool = compile_native(source, build, binary, language)
    finally:
        shutil.rmtree(build, ignore_errors=True)
    if tool.ok and binary.is_file():
        return {
            "binary": {"file": "out/binary"},
            "compile_log": tool.log,
            "outcome": "accepted",
        }
    binary.unlink(missing_ok=True)
    return {"compile_log": tool.log, "outcome": "compile_error"}


def compile_native(source: Path, build: Path, binary: Path, language: str) -> Tool:
    """Compile C or C++ with gcc into one statically linked executable."""
    name = source_name(source, ".c" if language == "c" else ".cpp")
    shutil.copyfile(source, build / name)
    if language == "c":
        command = ["gcc", "-x", "c", "-std=gnu17"]
    else:
        command = ["g++", "-x", "c++", "-std=gnu++20"]
    command += [
        "-O2",
        "-pipe",
        "-static",
        "-DONLINE_JUDGE",
        "-o",
        str(binary),
        name,
        "-lm",
    ]
    return run_tool(command, build, limit_address_space=True)


def compile_python(source: Path, build: Path, binary: Path) -> Tool:
    """Check the source with py_compile and pack it as a zip application.

    The archive holds the source as `__main__.py` behind a shebang line, so
    the interpreter in the sandbox-run image runs it as a script.
    """
    name = source_name(source, ".py")
    shutil.copyfile(source, build / name)
    tool = run_tool(
        [sys.executable, "-I", "-m", "py_compile", name],
        build,
        limit_address_space=True,
    )
    if not tool.ok:
        return tool
    app = build / "app"
    app.mkdir()
    shutil.copyfile(source, app / "__main__.py")
    zipapp.create_archive(app, binary, interpreter=PYTHON_LAUNCHER)
    return tool


def compile_java(source: Path, build: Path, binary: Path) -> Tool:
    """Compile Java with javac and pack the classes as a runnable jar.

    The file is named after its public top-level type, as javac requires, or
    `Main.java` when there is none. The main class is that public type, or
    else a top-level type named `Main`, or else the first top-level type.
    """
    text = source.read_bytes().decode("utf-8", errors="replace")
    package, types = java_declarations(text)
    public = [name for name, is_public in types if is_public]
    names = [name for name, _ in types]
    if public:
        main_class = public[0]
    elif "Main" in names or not names:
        main_class = "Main"
    else:
        main_class = names[0]
    named = public[0] if public and len(public[0]) <= JAVA_FILE_NAME_LIMIT else "Main"
    file_name = f"{named}.java"
    shutil.copyfile(source, build / file_name)
    classes = build / "classes"
    classes.mkdir()
    command = [
        "javac",
        "-J-Xmx512m",
        "-J-XX:+UseSerialGC",
        "-J-XX:TieredStopAtLevel=1",
        "-J-XX:-UsePerfData",
        "-encoding",
        "UTF-8",
        "-proc:none",
        "-Xlint:none",
        "-XDsuppressNotes",
        "-d",
        "classes",
        file_name,
    ]
    tool = run_tool(command, build, limit_address_space=False)
    if not tool.ok:
        return tool
    qualified = f"{package}.{main_class}" if package else main_class
    if not (classes / (qualified.replace(".", "/") + ".class")).is_file():
        return Tool(
            1,
            tool.log
            + f"no class named {qualified} to run; put main in a public class\n",
        )
    write_jar(classes, binary, qualified)
    return tool


def java_declarations(text: str) -> tuple[str, list[tuple[str, bool]]]:
    """Return a Java source's package and its top-level types.

    Comments and string literals are blanked first, then braces are counted so
    that only types declared outside every other type are listed, each with
    whether it is public.
    """
    code = JAVA_NOISE.sub(" ", text)
    package_match = JAVA_PACKAGE.search(code)
    package = re.sub(r"\s", "", package_match.group(1)) if package_match else ""
    types: list[tuple[str, bool]] = []
    depth = 0
    for match in JAVA_TOP_LEVEL.finditer(code):
        token = match.group(0)
        if token == "{":
            depth += 1
        elif token == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            types.append((match.group(2), "public" in match.group(1).split()))
    return package, types


def write_jar(classes: Path, binary: Path, main_class: str) -> None:
    """Write every class file under `classes` into a jar that runs `main_class`."""
    manifest = f"Manifest-Version: 1.0\r\nMain-Class: {main_class}\r\n\r\n"
    with zipfile.ZipFile(binary, "w", zipfile.ZIP_DEFLATED) as jar:
        jar.writestr("META-INF/MANIFEST.MF", manifest)
        for path in sorted(classes.rglob("*")):
            if path.is_file():
                jar.write(path, path.relative_to(classes).as_posix())


def source_name(source: Path, suffix: str) -> str:
    """The name the source is compiled under, so the log names the contestant's file.

    A name that could be read as an option or a path falls back to `main`.
    """
    name = source.name
    if SAFE_NAME.fullmatch(name) and name.endswith(suffix):
        return name
    return "main" + suffix


def run_tool(command: list[str], cwd: Path, *, limit_address_space: bool) -> Tool:
    """Run a compiler with its own limits and return its exit code and log.

    The compiler gets `COMPILE_SECONDS` of wall time, a cap on the size of any
    file it writes and, for compilers that are not a JVM, a cap on its address
    space, so a source that makes the compiler run away ends as a compile
    error rather than the container being killed. Standard output and error
    are read together and kept up to `LOG_LIMIT` bytes.
    """
    env = {"PATH": TOOL_PATH, "HOME": str(cwd), "TMPDIR": str(cwd), "LANG": "C.UTF-8"}

    def limits() -> None:
        """Set the compiler's resource limits in the child before it starts."""
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (BINARY_LIMIT, BINARY_LIMIT))
        if limit_address_space:
            resource.setrlimit(
                resource.RLIMIT_AS, (COMPILER_ADDRESS_SPACE, COMPILER_ADDRESS_SPACE)
            )

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=limits,
        )
    except OSError as error:
        raise PrimitiveError(
            f"the compiler {command[0]} could not be started: {error.strerror}"
        ) from None
    assert process.stdout is not None
    deadline = time.monotonic() + COMPILE_SECONDS
    kept = bytearray()
    dropped = 0
    timed_out = False
    with process:
        stream = process.stdout.fileno()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            if not select.select([stream], [], [], left)[0]:
                continue
            chunk = os.read(stream, 65536)
            if not chunk:
                break
            room = LOG_LIMIT - len(kept)
            kept += chunk[:room]
            dropped += max(0, len(chunk) - room)
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
        kill_group(process.pid)
        process.wait()
    log = kept.decode("utf-8", errors="replace")
    if dropped:
        log += (
            f"\n[compile log cut at {LOG_LIMIT} bytes; {dropped} more bytes left out]\n"
        )
    if timed_out:
        log += (
            f"\ncompiling took longer than {COMPILE_SECONDS} seconds and was stopped\n"
        )
        return Tool(None, log)
    return Tool(process.returncode, log)


def kill_group(pid: int) -> None:
    """Kill whatever is left of a compiler's process group."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError, PermissionError:
        pass


def write_json(path: Path, document: dict[str, object]) -> None:
    """Write a JSON file whole: to a temporary name first, then renamed."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    partial.replace(path)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
