#!/usr/local/bin/python3 -I
"""The compile primitive: a folder of sources in, one binary and a compile log out.

The harness mounts a working directory at `/work` (or the directory named by
the first argument) holding `inputs.json` and the source folder under `in/`.
This program compiles the folder for the language named in the inputs,
starting from the entry file, and writes `outputs.json`, with the binary under
`out/` when the compile succeeded.

A source that does not compile is an ordinary result: the outcome is
`compile_error` and the compile log says why. So is anything else the
contestant's folder causes, such as naming an entry that is not there or
making a compiler run out of time. `error` in `outputs.json` is kept for the
cases where the primitive could not work at all, such as a missing input.

The binary is one file the sandbox-run primitive runs: a statically linked
executable for C and C++, a Python zip application for Python and a runnable
jar for Java (see the README).
"""

import errno
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
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 5
LANGUAGES = ("c", "cpp", "java", "python")
LANGUAGE_NAMES = {"c": "C", "cpp": "C++", "java": "Java", "python": "Python"}
EXTENSIONS = {
    "c": (".c",),
    "cpp": (".cpp", ".cc", ".cxx"),
    "java": (".java",),
    "python": (".py",),
}
"""The files of a folder that are compiled as the language's sources."""
MAIN_DESCRIPTIONS = {
    "c": ("defines main", "define main"),
    "cpp": ("defines main", "define main"),
    "java": ("declares static void main", "declare static void main"),
    "python": ('checks __name__ == "__main__"', 'check __name__ == "__main__"'),
}
"""How the log says which files could be the entry, for one file and for many."""
FOLDER_LIMIT = 1000
"""The most files and folders the source folder may hold."""
NAMES_SHOWN = 10
LOG_LIMIT = 64 * 1024
BINARY_LIMIT = 32 * 1024 * 1024
TOO_LARGE = "the binary would be larger than 32 MB\n"
"""What the log ends with when the binary is over `BINARY_LIMIT`: the zip
application and the jar are written by this program, which a compiler's
file-size limit does not hold, so their size is checked here."""
COMPILE_SECONDS = 30
COMPILER_ADDRESS_SPACE = 768 * 1024 * 1024
PYTHON_SHEBANG = b"#!/usr/bin/env python3\n"
PYTHON_FOLDER = "source"
"""Where a Python binary holds the source folder, beside its `__main__.py`."""
PYTHON_LAUNCHER = """\
import sys
import zipimport
from types import ModuleType

entry = {entry!r}
archive = __file__.rpartition("/")[0]
path = archive + "/{folder}/" + entry
folder = path.rpartition("/")[0]
if sys.path and sys.path[0] == archive:
    sys.path[0] = folder
else:
    sys.path.insert(0, folder)
code = compile(zipimport.zipimporter(archive).get_data(path), path, "exec")
main = ModuleType("__main__")
main.__file__ = path
sys.modules["__main__"] = main
exec(code, main.__dict__)
"""
"""The `__main__.py` of every Python binary: it runs the entry as the main
module, with the entry's own folder first on the import path, as `python3
<entry>` does."""
TOOL_PATH = "/usr/local/bin:/usr/bin:/bin"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
PLAIN_START = re.compile(r"[A-Za-z0-9_]")
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
JAVA_MAIN = re.compile(
    r"\b((?:(?:public|protected|private|static|final|synchronized|strictfp)\s+)+)"
    r"void\s+main\s*\("
)
C_NOISE = re.compile(
    r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|//[^\n]*|/\*.*?\*/', re.DOTALL
)
C_MAIN = re.compile(r"(?<![\w.:>])main\s*\([^;{}]{0,2000}\)[^;{}()]{0,200}\{")
PYTHON_MAIN = re.compile(
    r"^if\s+(?:__name__\s*==\s*(['\"])__main__\1|(['\"])__main__\2\s*==\s*__name__)"
    r"\s*:",
    re.M,
)


class PrimitiveError(Exception):
    """The primitive could not do its work at all.

    The message is one sentence for a person and becomes `error` in
    `outputs.json`, which the harness turns into a `system_error` verdict.
    """


class CompileError(Exception):
    """The contestant's folder cannot be compiled as it is.

    The message is one line of the compile log, saying why, and the outcome is
    `compile_error`.
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


@dataclass(frozen=True)
class Program:
    """The source folder as copied for compiling, by paths relative to the copy.

    `files` is every file of the folder, `sources` those compiled as the
    language's sources and `entry` the file to start from.
    """

    files: list[str]
    sources: list[str]
    entry: str


def main(argv: list[str]) -> int:
    """Compile what `inputs.json` names and write `outputs.json`.

    Exits 0 whenever `outputs.json` was written, error or not; the harness
    reads the outcome and any error from that file.
    """
    root = Path(argv[1]) if len(argv) > 1 else Path("/work")
    try:
        folder, language, entry = read_inputs(root)
        outputs = compile_source(root, folder, language, entry)
        document: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "outputs": outputs,
        }
    except PrimitiveError as error:
        print(f"compile: {error}", file=sys.stderr)
        document = {"schema_version": SCHEMA_VERSION, "error": str(error)}
    write_json(root / "outputs.json", document)
    return 0


def read_inputs(root: Path) -> tuple[Path, str, str | None]:
    """Read `inputs.json` and return the source folder, the language and the
    entry, None when it was not given or is empty.
    """
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
    folder = input_folder(root, inputs, "source")
    language = inputs.get("language")
    if language not in LANGUAGES:
        raise PrimitiveError(f"the language input is not one of {', '.join(LANGUAGES)}")
    entry = inputs.get("entry")
    if entry is not None and not isinstance(entry, str):
        raise PrimitiveError("the input named entry is not text")
    if entry is not None:
        entry = entry.strip() or None
    return folder, str(language), entry


def input_folder(root: Path, inputs: dict[str, object], name: str) -> Path:
    """Resolve a folder input to a path, refusing anything outside `in/`."""
    value = inputs.get(name)
    if not isinstance(value, dict) or not isinstance(value.get("folder"), str):
        raise PrimitiveError(f"the input named {name} is not a folder")
    path = (root / str(value["folder"])).resolve()
    if not path.is_relative_to((root / "in").resolve()):
        raise PrimitiveError(f"the input named {name} is outside in/")
    if not path.is_dir():
        raise PrimitiveError(f"the input named {name} is not in the working directory")
    return path


def compile_source(
    root: Path, folder: Path, language: str, entry: str | None
) -> dict[str, object]:
    """Compile `folder` into `out/binary` and return the outputs.

    Everything the compilers write goes to a fresh directory under the
    temporary directory, removed afterwards, except the binary itself.
    """
    out = root / "out"
    out.mkdir(exist_ok=True)
    binary = out / "binary"
    binary.unlink(missing_ok=True)
    build = Path(tempfile.mkdtemp(prefix="compile-"))
    try:
        source = build / "source"
        program = prepare(folder, source, language, entry)
        if language == "python":
            tool = compile_python(program, source, binary)
        elif language == "java":
            tool = compile_java(program, source, build, binary)
        else:
            tool = compile_native(program, source, binary, language)
    except CompileError as error:
        tool = Tool(1, f"{error}\n")
    finally:
        shutil.rmtree(build, ignore_errors=True)
    if tool.ok and binary.is_file() and binary.stat().st_size > BINARY_LIMIT:
        tool = Tool(1, tool.log + TOO_LARGE)
    if tool.ok and binary.is_file():
        return {
            "binary": {"file": "out/binary"},
            "compile_log": tool.log,
            "outcome": "accepted",
        }
    binary.unlink(missing_ok=True)
    return {"compile_log": tool.log, "outcome": "compile_error"}


def prepare(folder: Path, source: Path, language: str, entry: str | None) -> Program:
    """Copy the source folder to `source` and work out what to compile.

    A folder of one file is that file, whatever it is called, copied under the
    name it is compiled as. A larger folder is copied with its layout, its
    sources are its files with the language's extensions, and the entry is
    the one named, or else the one source, or else the one source that holds
    main (see the README).
    """
    files = folder_files(folder)
    if entry is not None:
        entry = entry_path(entry, files)
    if len(files) == 1:
        name = single_name(folder / files[0], language)
        copy_file(folder / files[0], source / name)
        return Program([name], [name], name)
    sources = [path for path in files if path.endswith(EXTENSIONS[language])]
    if entry is None:
        entry = find_entry(folder, sources, language)
    elif entry not in sources:
        raise CompileError(
            f"the entry {entry} is not a {LANGUAGE_NAMES[language]} source file"
            f" ({', '.join(EXTENSIONS[language])})"
        )
    for path in files:
        copy_file(folder / path, source / path)
    return Program(files, sources, entry)


def folder_files(folder: Path) -> list[str]:
    """Every regular file under `folder`, as sorted paths relative to it.

    Links are neither followed nor listed. A folder that is empty, holds more
    than `FOLDER_LIMIT` entries or a name that is not UTF-8 is a compile
    error.
    """
    found: list[str] = []
    entries = 0
    for directory, folders, names in folder.walk():
        entries += len(folders) + len(names)
        if entries > FOLDER_LIMIT:
            raise CompileError(
                f"the source folder holds more than {FOLDER_LIMIT} files and folders"
            )
        for name in names:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(folder).as_posix()
            try:
                relative.encode("utf-8")
            except UnicodeEncodeError:
                shown = os.fsencode(relative).decode("utf-8", errors="replace")
                raise CompileError(
                    f"the file name {shown} in the source folder is not UTF-8"
                ) from None
            found.append(relative)
    if not found:
        raise CompileError("the source folder holds no files")
    return sorted(found)


def entry_path(entry: str, files: list[str]) -> str:
    """The entry as a path relative to the folder, refusing one that is not a
    file in it.
    """
    path = PurePosixPath(entry)
    relative = path.as_posix()
    if path.is_absolute() or ".." in path.parts or relative not in files:
        raise CompileError(f"the entry {entry} is not a file in the source folder")
    return relative


def find_entry(folder: Path, sources: list[str], language: str) -> str:
    """The entry when none was named: the one source, or the one that holds main.

    Anything else is a compile error that asks for the entry to be named.
    """
    name = LANGUAGE_NAMES[language]
    if not sources:
        raise CompileError(
            f"the source folder holds no {name} source file"
            f" ({', '.join(EXTENSIONS[language])})"
        )
    if len(sources) == 1:
        return sources[0]
    holders = [path for path in sources if holds_main(folder / path, language)]
    if len(holders) == 1:
        return holders[0]
    one, many = MAIN_DESCRIPTIONS[language]
    if holders:
        shown = ", ".join(holders[:NAMES_SHOWN])
        if len(holders) > NAMES_SHOWN:
            shown += f" and {len(holders) - NAMES_SHOWN} more"
        found = f"{shown} each {many}"
    else:
        found = f"no {name} source {one}"
    raise CompileError(
        f"the entry is not clear: {found}; name the file to start from as the entry"
    )


def holds_main(path: Path, language: str) -> bool:
    """Whether a source file looks like a program's start.

    C and C++: it defines a function `main`. Java: it declares a `static void
    main`. Python: it has a top-level `if __name__ == "__main__":`. Comments
    and string literals are ignored for C, C++ and Java.
    """
    text = read_text(path)
    if language == "python":
        return PYTHON_MAIN.search(text) is not None
    if language == "java":
        code = JAVA_NOISE.sub(" ", text)
        return any(
            "static" in match.group(1).split() for match in JAVA_MAIN.finditer(code)
        )
    return C_MAIN.search(C_NOISE.sub(" ", text)) is not None


def single_name(path: Path, language: str) -> str:
    """The name the only file of a folder is compiled under.

    Java is named after the public top-level type, as javac requires, or
    `Main.java` when there is none; the others keep a plain name of the
    language's own extension and fall back to `main`.
    """
    if language != "java":
        return source_name(path, EXTENSIONS[language][0])
    public = [
        name for name, is_public in java_declarations(read_text(path))[1] if is_public
    ]
    named = public[0] if public and len(public[0]) <= JAVA_FILE_NAME_LIMIT else "Main"
    return f"{named}.java"


def copy_file(origin: Path, target: Path) -> None:
    """Copy one file of the source folder, a failure being a compile error."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, target)
    except OSError as error:
        raise CompileError(
            f"the source folder could not be copied to compile it: {error.strerror}"
        ) from None


def compile_native(program: Program, source: Path, binary: Path, language: str) -> Tool:
    """Compile C or C++ with gcc into one statically linked executable.

    The entry and every other source is compiled and linked, except a source
    other than the entry that defines its own main.
    """
    others = [
        path
        for path in program.sources
        if path != program.entry and not holds_main(source / path, language)
    ]
    if language == "c":
        command = ["gcc", "-x", "c", "-std=gnu17"]
    else:
        command = ["g++", "-x", "c++", "-std=gnu++20"]
    command += [
        "-O2",
        "-pipe",
        "-static",
        "-DONLINE_JUDGE",
        "-iquote",
        ".",
        "-o",
        str(binary),
        *map(argument, [program.entry, *others]),
        "-lm",
    ]
    return run_tool(command, source, limit_address_space=True)


def compile_python(program: Program, source: Path, binary: Path) -> Tool:
    """Check every source with py_compile and pack the folder as a zip application.

    The archive holds the folder under `source/` and a `__main__.py` that runs
    the entry, behind a shebang line, so the interpreter in the sandbox-run
    image runs it as a script.
    """
    tool = run_tool(
        [sys.executable, "-I", "-m", "py_compile", *map(argument, program.sources)],
        source,
        limit_address_space=True,
    )
    if not tool.ok:
        return tool
    try:
        write_zip_application(source, program, binary)
    except OSError:
        return Tool(1, tool.log + TOO_LARGE)
    return tool


def write_zip_application(source: Path, program: Program, binary: Path) -> None:
    """Write the folder and the launcher for its entry as a zip application."""
    launcher = PYTHON_LAUNCHER.format(entry=program.entry, folder=PYTHON_FOLDER)
    with binary.open("wb") as stream:
        stream.write(PYTHON_SHEBANG)
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("__main__.py", launcher)
            for path in program.files:
                archive.write(source / path, f"{PYTHON_FOLDER}/{path}")


def compile_java(program: Program, source: Path, build: Path, binary: Path) -> Tool:
    """Compile Java with javac and pack the classes as a runnable jar.

    Every source is compiled. The main class is the entry's public top-level
    type, or else its top-level type named `Main`, or else its first
    top-level type, in the entry's package.
    """
    package, types = java_declarations(read_text(source / program.entry))
    public = [name for name, is_public in types if is_public]
    names = [name for name, _ in types]
    if public:
        main_class = public[0]
    elif "Main" in names or not names:
        main_class = "Main"
    else:
        main_class = names[0]
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
        str(classes),
        *map(argument, program.sources),
    ]
    tool = run_tool(command, source, limit_address_space=False)
    if not tool.ok:
        return tool
    qualified = f"{package}.{main_class}" if package else main_class
    if not (classes / (qualified.replace(".", "/") + ".class")).is_file():
        return Tool(
            1,
            tool.log
            + f"no class named {qualified} to run; put main in a public class\n",
        )
    try:
        write_jar(classes, binary, qualified)
    except OSError:
        return Tool(1, tool.log + TOO_LARGE)
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


def read_text(path: Path) -> str:
    """A source file's text, any bytes that are not UTF-8 replaced."""
    return path.read_bytes().decode("utf-8", errors="replace")


def source_name(source: Path, suffix: str) -> str:
    """The name the source is compiled under, so the log names the contestant's file.

    A name that could be read as an option or a path falls back to `main`.
    """
    name = source.name
    if SAFE_NAME.fullmatch(name) and name.endswith(suffix):
        return name
    return "main" + suffix


def argument(path: str) -> str:
    """A relative path as a compiler argument, never read as an option or an
    argument file.
    """
    return path if PLAIN_START.match(path) else f"./{path}"


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
        if error.errno == errno.E2BIG:
            return Tool(1, "the source folder's paths are too long to compile\n")
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
