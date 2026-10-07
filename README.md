# primitive-compile

The `unicon/compile` primitive: it turns a contestant's folder of source files
into one binary that the `unicon/sandbox-run` primitive can run, and gives
back the compiler's log. It is the first step of the `unicon/classic`
workflow.

This repo holds the image, `ghcr.io/uniconhq/primitive-compile`, built from
the `Dockerfile`; the program the image runs, `src/compiler.py`; and
`primitive.yaml`, the declaration the forge compiler reads to type-check a
workflow that uses the primitive. The program is one Python file with no
dependencies. It speaks the primitive contract, `primitive.schema.json`
version 5, published by the [runner](https://github.com/uniconhq/runner).

## What it takes and returns

| | Name | Type | What it is |
|---|---|---|---|
| Input | `source` | folder, `runs: true` | The contestant's source files with their layout. One uploaded file arrives as a folder holding that file, placed so by the harness |
| Input | `language` | enum | `c`, `cpp`, `java` or `python` |
| Input | `entry` | text, optional | The file to start from, by its path in the folder, such as `src/Main.java` |
| Output | `binary` | file | `out/binary`, written when the compile succeeded |
| Output | `compile_log` | text | Everything the compilers printed, standard output and error together, and any note of why the compile failed |
| Output | `outcome` | outcome | `accepted` when the source compiled, `compile_error` when it did not |

The step runs once per submission (`batch: false`).

**Nothing the contestant's folder does can fault the run.** `source` is the
contestant's, and the compilers run over it, so it is marked `runs: true`.
Whatever it makes them do ends as `compile_error`, with a line in the log
saying why: a source that does not compile; one that makes a compiler go
over its time, memory or file-size limit, crash or print without end; a
folder that is empty, holds too many files, does not fit the scratch space
or takes too long to read; an entry that is not in the folder or not clear;
a binary over 32 MB. None of these is `error`, a container killed at its limit, or an
output over the harness's caps. The log kept from the compilers is cut to
the size of the `compile_log` port, 64 KB (see Limits).

On `compile_error` there is no binary: the harness then stops grading, the
verdict is `compile_error`, and nothing reads `binary`.

`outputs.json` carries `error` instead, and nothing else, only when the
primitive could not work at all: `inputs.json` is missing, is not JSON or is
for another contract version, `source` is not a folder, is missing or lies
outside `in/`, the language is not one of the four, `entry` is not text, or
a compiler could not be started. The harness turns an error into a
`system_error` verdict, so no contestant is graded by a broken step.

## Which files are compiled, and the entry

**A folder holding one file** is that file, whatever its name, and it is the
entry. It is compiled under its own file name when that name is plain
(letters, digits, `.`, `_` and `-`, starting with a letter or digit, with
the language's usual extension), so the log names the contestant's file;
anything else is compiled as `main.c`, `main.cpp` or `main.py`, and Java as
below. This is how a single uploaded file is compiled.

**A larger folder** is copied with its layout. Its sources are its files
with the language's extensions; every other file, such as a header or a
data file, is copied beside them.

| Language | Sources |
|---|---|
| `c` | `.c` |
| `cpp` | `.cpp`, `.cc`, `.cxx` |
| `java` | `.java` |
| `python` | `.py` |

The entry is the file `entry` names, by its path from the top of the folder,
`/` between folders; a leading `./` is allowed and `..` is not. It must be a
file of the folder and, in a larger folder, one of its sources. Spaces around
it are ignored, and an entry that is empty is taken as not given.

With no entry given, the entry is the folder's one source, or else the one
source that holds main:

| Language | A source holds main when it |
|---|---|
| `c`, `cpp` | Defines a function named `main`: `main(...)` followed by a body. A member such as `Game::main` or a call such as `game.main()` does not count |
| `java` | Declares a `static void main` method |
| `python` | Has a top-level line `if __name__ == "__main__":`, either quotes, either side first |

Comments and string and character literals (and Java's text blocks) are
ignored for C, C++ and Java. A block comment or text block left open runs to
the end of the file, and a string or character literal left open to the end
of its line. When no source holds main, or more than one does, the outcome is `compile_error` and the log
says so and asks for the entry, for example:

```
the entry is not clear: a.py, b/b.py each check __name__ == "__main__"; name the file to start from as the entry
```

What the entry decides, per language:

- **C and C++.** The entry and every other source are compiled and linked
  into one binary, except a source other than the entry that defines its own
  `main`, so a folder can keep a test driver beside the solution.
- **Java.** Every source is compiled together. The entry gives the class the
  jar runs (below).
- **Python.** Every source is checked with `py_compile`, so a file with a
  syntax error fails the compile even when nothing imports it. The binary
  holds every file of the folder and runs the entry as the main module, with
  the entry's own folder first on the import path, as `python3 <entry>`
  does from the top of the folder.

## How each language is compiled

| Language | Toolchain in the image | What the program runs |
|---|---|---|
| `c` | gcc 14 | `gcc -x c -std=gnu17 -O2 -pipe -static -DONLINE_JUDGE -iquote . -o out/binary <entry> <sources> -lm` |
| `cpp` | g++ 14 | `g++ -x c++ -std=gnu++20 -O2 -pipe -static -DONLINE_JUDGE -iquote . -o out/binary <entry> <sources> -lm` |
| `java` | OpenJDK 21 | `javac -encoding UTF-8 -proc:none -Xlint:none -XDsuppressNotes -d classes <sources>`, then packs the classes into a jar |
| `python` | Python 3.14 | `python3 -I -m py_compile <sources>`, then packs the folder into a zip application |

The compilers run in the top of the folder's copy, and each file is named
by its path from there. A path that does not start with a letter, a digit or
`_` is written with `./` in front, so no file name is read as an option or,
starting with `@`, as a file of arguments. `-iquote .` lets
`#include "lib/util.h"` name a header by its path from the top of the
folder, as well as from the folder of the file that includes it.

`ONLINE_JUDGE` is defined because contest code commonly checks for it to
skip reading from a local file. `-proc:none` means javac runs no annotation
processors, so compiling never runs code. `-XDsuppressNotes` and
`-Xlint:none` keep a successful Java compile quiet, and gcc runs without
`-Wall`: in the classic workflow a non-empty compile log becomes the
verdict's summary, so a clean compile should print nothing.

**Java class names.** javac requires a public class to be in a file of the
same name. The only file of a one-file folder is named after its public
top-level type, or `Main.java` when it has none; in a larger folder each file
keeps its name and its place, so a package tree compiles as it is laid out.
The class the jar runs is the entry's public top-level type; if there is
none, its top-level type named `Main`; otherwise its first top-level type.
The entry's `package` line is followed. Comments and string literals are
ignored when finding these names. When the chosen class did not come out of
the compile, the outcome is `compile_error` and the log says which class it
looked for.

## The binary format

`binary` is one file, in one of three formats. compile writes them, and
sandbox-run tells them apart by their content.

| Language | The binary | How sandbox-run runs it |
|---|---|---|
| `c`, `cpp` | A statically linked ELF executable | Copied to a scratch directory, marked executable and run directly |
| `python` | A Python zip application behind the line `#!/usr/bin/env python3`: the folder under `source/` with its layout, and a `__main__.py` that runs the entry | `python3 -I -B binary`, on the image's Python 3.14 |
| `java` | A runnable jar: the compiled classes and a manifest naming the main class | `java -Xmx<memory_limit>m -Xss64m -XX:+UseSerialGC -XX:-UsePerfData -XX:+ExitOnOutOfMemoryError -Djava.io.tmpdir=. -jar binary`, on the image's OpenJDK 21 |

The Python binary's `__main__.py` puts the entry's folder inside the archive
first on `sys.path` and runs the entry's code as a fresh `__main__` module
whose `__file__` is the entry's path in the archive, so the entry imports
the files beside it as it would from the folder.

Static linking means a native binary needs nothing from the image it runs
in. A Python or Java binary needs the interpreter it was checked against, so
both images are built from the same pinned `python:3.14-slim` base and
install the same Debian OpenJDK 21 packages: the JDK in compile, the runtime
in sandbox-run. A change to any of the three formats, or to either image's
Python or Java, is made in both repos together.

## Limits

The container's limits are in `primitive.yaml`: 60 s of wall and CPU time,
1024 MB of memory, 128 processes, 64 MB of output and no GPU. Inside them the
program holds the contestant's folder and each compiler to limits of its
own, smaller ones, so a folder that makes the compiler run away ends as a
`compile_error` with a note in the log rather than the container being
killed, which would be a `system_error`:

- 1000 files and folders in the source folder, counted before anything is
  copied;
- 10 seconds for preparing the folder before any compiler runs: listing it,
  copying it, and reading its sources for the entry and the Java class
  names. Each source is read in one pass, in time linear in its size
  whatever it holds, so only a folder of megabytes made to be slow reaches
  the limit; past it the outcome is `compile_error` and the log says so;
- the scratch space under `/tmp` the folder is copied into: a folder that
  does not fit is a `compile_error`, the log saying the copy failed;
- 30 seconds of wall time for each compiler, after which its whole process
  group is killed;
- 768 MB of address space for gcc, g++ and py_compile, and a 512 MB heap for
  javac, which reserves more address space than it uses;
- 32 MB for any single file a compiler writes, and for the binary: the
  zip application and the jar, which this program writes itself, are checked
  against it here, and one over it is `compile_error` with the log saying
  so;
- a command line too long for the system, from the folder's paths, is a
  `compile_error` too.

The compile log keeps the first 64 KB of what the compiler printed and ends
with a line saying how much more was left out.

## Inside the sandbox

The harness starts the container with no network, a read-only root
filesystem, every capability dropped, `no-new-privileges`, Docker's built-in
seccomp profile and a non-root user; the image's user is 65532. The only
writable places are `/work` and a small tmpfs at `/tmp`. The program copies
the source folder into a fresh directory under `/tmp`, leaving `in/` as it
was, and the compilers write everything they produce (object files, class
files, `__pycache__`) there; it removes that directory before it exits. It
writes `out/binary` and `outputs.json` under `/work`, and `outputs.json`
whole, under a temporary name first, so the harness never reads half a file.
Links in the source folder are neither followed nor copied.

## Layout

```
Dockerfile                  the image: python:3.14-slim, gcc, g++, OpenJDK 21
src/compiler.py             the program, installed as /usr/local/bin/compile, the image's entrypoint
primitive.yaml              the declaration, without the image line
tests/                      unit tests, and image tests that run it on Docker
```

`primitive.yaml` has no `image` line here: bootstrap writes the image by
digest, from the release manifest, into the version it creates at the forge.
It names neither the primitive nor its version: the repo at the forge is the
name and the release tag the version.

## Running it locally

Python 3.14 with [uv](https://docs.astral.sh/uv/), and Docker for the image
tests.

```
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run python ../runner/scripts/check_declaration.py ../runner/schemas/primitive.schema.json .
uv run pytest
```

The declaration check is the runner's, so it runs from a `runner` checkout
beside this one, at the release named in `.github/workflows/ci.yaml`.

`uv run pytest` runs everything: the unit tests, which need Linux because the
program does, and the image tests (marked `image`), which build the image and
run it under the harness's sandbox flags. Elsewhere only the image tests are
collected. Without Docker the image tests are skipped. They use
`PRIMITIVE_IMAGE` instead of building when it is set. The checks on every
`inputs.json` and `outputs.json` the tests see read the schema from
`PRIMITIVE_SCHEMA`, or from a `runner` checkout beside this one.

To run one compile by hand:

```
docker build -t primitive-compile:dev .
mkdir -p work/in/1/source/lib
printf 'from lib.twice import twice\nprint(twice(int(input())))\n' > work/in/1/source/main.py
printf 'def twice(n):\n    return 2 * n\n' > work/in/1/source/lib/twice.py
echo '{"schema_version": 5, "inputs": {"source": {"folder": "in/1/source"}, "language": "python", "entry": "main.py"}}' > work/inputs.json
chmod -R a+rwX work
docker run --rm --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --security-opt=seccomp=builtin \
  --memory=1g --memory-swap=1g --pids-limit=128 \
  --tmpfs=/tmp:rw,noexec,nosuid,size=64m -v "$PWD/work:/work" primitive-compile:dev
cat work/outputs.json
```

`.github/workflows/ci.yaml` and `release.yaml` call the workflows every
primitive shares, `primitive-ci.yaml` and `primitive-release.yaml` in the
[runner](https://github.com/uniconhq/runner) repo, at the runner release this
primitive is built against, and name the same release as `runner-ref`. They
check this repo out beside the runner at that release, so the checks read
its `primitive.schema.json` and run its `scripts/check_declaration.py`. CI
runs the checks and unit tests in one job, and builds the image and runs the
image tests in another. Moving to a new runner release is a change to the
two `uses:` lines and `runner-ref` together.

## Releasing

Push a tag `v2.3.4` on `main`. The shared release workflow refuses a tag
whose commit is not on `main` and a tag that differs from the version in
`pyproject.toml`. The tag's major is the version at the forge: `v2.3.4` is a
release of `unicon/compile@v2`, so a change to the ports is a new major. It
runs the same checks as CI, pushes the image as
`ghcr.io/uniconhq/primitive-compile:v2.3.4`, and creates a GitHub release
with `images.json`, which names the image by digest in the same shape as the
runner's, and `primitive.yaml` attached, and the digest in the notes.
`deploy/images.json` pins that digest, and bootstrap writes it into the
forge's copy of `primitive.yaml`.

The first push creates the package on the organisation as **private**.
Grading machines pull it anonymously, so someone has to open the package on
the organisation's Packages page once, set its visibility to public, and add
this repo under Manage Actions access so later releases can keep pushing to
it.
