# primitive-compile

The `unicon/compile` primitive: it turns one contestant source file into one
binary that the `unicon/sandbox-run` primitive can run, and gives back the
compiler's log. It is the first step of the `unicon/classic` workflow.

This repo holds the image, `ghcr.io/uniconhq/primitive-compile`, built from
the `Dockerfile`; the program the image runs, `src/compiler.py`; and
`primitive.yaml`, the declaration the forge compiler reads to type-check a
workflow that uses the primitive. The program is one Python file with no
dependencies. It speaks the primitive contract, `primitive.schema.json`
version 4, published by the [runner](https://github.com/uniconhq/runner).

## What it takes and returns

| | Name | Type | What it is |
|---|---|---|---|
| Input | `source` | file | The source file |
| Input | `language` | enum | `python`, `c`, `cpp` or `java` |
| Output | `binary` | file, optional | `out/binary`, present only when the compile succeeded |
| Output | `compile_log` | text | Everything the compiler printed, standard output and error together |
| Output | `outcome` | outcome | `accepted` when the source compiled, `compile_error` when it did not |

The step runs once per submission (`batch: false`).

A source that does not compile is an ordinary result: the outcome is
`compile_error`, the log says why, and there is no binary. The harness then
stops grading and the verdict is `compile_error`.

`outputs.json` carries `error` instead, and nothing else, only when the
primitive could not work at all: `inputs.json` is missing, is not JSON or is
for another contract version, the source is missing or lies outside `in/`,
the language is not one of the four, or a compiler could not be started. The
harness turns an error into a `system_error` verdict, so no contestant is
graded by a broken step.

## How each language is compiled

| Language | Toolchain in the image | What the program runs |
|---|---|---|
| `python` | Python 3.14 | `python3 -I -m py_compile <file>`, then packs the source into a zip application |
| `c` | gcc 14 | `gcc -x c -std=gnu17 -O2 -pipe -static -DONLINE_JUDGE -o out/binary <file> -lm` |
| `cpp` | g++ 14 | `g++ -x c++ -std=gnu++20 -O2 -pipe -static -DONLINE_JUDGE -o out/binary <file> -lm` |
| `java` | OpenJDK 21 | `javac -encoding UTF-8 -proc:none -Xlint:none -XDsuppressNotes -d classes <Name>.java`, then packs the classes into a jar |

The source is compiled under its own file name when that name is plain
(letters, digits, `.`, `_` and `-`, starting with a letter or digit, with
the language's usual extension), so the log names the contestant's file.
Anything else is compiled as `main.c`, `main.cpp` or `main.py`.

`ONLINE_JUDGE` is defined because contest code commonly checks for it to
skip reading from a local file. `-proc:none` means javac runs no annotation
processors, so compiling never runs code. `-XDsuppressNotes` and
`-Xlint:none` keep a successful Java compile quiet, and gcc runs without
`-Wall`: in the classic workflow a non-empty compile log becomes the
verdict's summary, so a clean compile should print nothing.

**Java class names.** javac requires a public class to be in a file of the
same name, so the file is named after the source's public top-level type, or
`Main.java` when it has none. The class the jar runs is that public type; if
there is none, a top-level type named `Main`; otherwise the first top-level
type. A `package` line is followed. Comments and string literals are ignored
when finding these names. When the chosen class did not come out of the
compile, the outcome is `compile_error` and the log says which class it
looked for.

## The binary format

`binary` is one file, in one of three formats. compile writes them, and
sandbox-run tells them apart by their content.

| Language | The binary | How sandbox-run runs it |
|---|---|---|
| `c`, `cpp` | A statically linked ELF executable | Copied to a scratch directory, marked executable and run directly |
| `python` | A Python zip application: a zip holding the source as `__main__.py`, behind the line `#!/usr/bin/env python3` | `python3 -I -B binary`, on the image's Python 3.14 |
| `java` | A runnable jar: the compiled classes and a manifest naming the main class | `java -Xmx<memory_limit>m -Xss64m -XX:+UseSerialGC -XX:-UsePerfData -XX:+ExitOnOutOfMemoryError -Djava.io.tmpdir=. -jar binary`, on the image's OpenJDK 21 |

Static linking means a native binary needs nothing from the image it runs
in. A Python or Java binary needs the interpreter it was checked against, so
both images are built from the same pinned `python:3.14-slim` base and
install the same Debian OpenJDK 21 packages: the JDK in compile, the runtime
in sandbox-run. A change to any of the three formats, or to either image's
Python or Java, is made in both repos together.

## Limits

The container's limits are in `primitive.yaml`: 60 s of wall and CPU time,
1024 MB of memory, 128 processes and 64 MB of output. Inside them the
program gives each compiler its own, smaller limits, so a source that makes
the compiler run away ends as a `compile_error` with a note in the log
rather than the container being killed, which would be a `system_error`:

- 30 seconds of wall time, after which the compiler's whole process group is
  killed;
- 768 MB of address space for gcc, g++ and py_compile, and a 512 MB heap for
  javac, which reserves more address space than it uses;
- 32 MB for any single file a compiler writes, and for the binary: the
  zip application and the jar, which this program writes itself, are checked
  against it here, and one over it is `compile_error` with the log saying
  so.

The compile log keeps the first 64 KB of what the compiler printed and ends
with a line saying how much more was left out.

## Inside the sandbox

The harness starts the container with no network, a read-only root
filesystem, every capability dropped, `no-new-privileges`, Docker's built-in
seccomp profile and a non-root user; the image's user is 65532. The only
writable places are `/work` and a small tmpfs at `/tmp`. The program writes
`out/binary` and `outputs.json` under `/work`, and everything else the
compilers produce (object files, class files, `__pycache__`) in a fresh
directory under `/tmp` that it removes before it exits. It writes
`outputs.json` whole, under a temporary name first, so the harness never
reads half a file.

## Layout

```
Dockerfile                  the image: python:3.14-slim, gcc, g++, OpenJDK 21
src/compiler.py             the program, installed as /usr/local/bin/compile, the image's entrypoint
primitive.yaml              the declaration, without the image line
tests/                      unit tests, and image tests that run it on Docker
```

`primitive.yaml` has no `image` line here: bootstrap writes the image by
digest, from the release manifest, into the version it creates at the forge.

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
mkdir -p work/in && echo 'print(int(input()) * 2)' > work/in/main.py
echo '{"schema_version": 4, "inputs": {"source": {"file": "in/main.py"}, "language": "python"}}' > work/inputs.json
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

Push a tag `v1.2.3` on `main`. The shared release workflow refuses a tag whose
commit is not on `main`, a tag that differs from the version in
`pyproject.toml`, and a tag that is not a release of the version
`primitive.yaml` declares (`v1.2.3` is a release of `v1`). It runs the same
checks as CI, pushes the image as `ghcr.io/uniconhq/primitive-compile:v1.2.3`,
and creates a GitHub release with `images.json`, which names the image by
digest in the same shape as the runner's, and `primitive.yaml` attached, and
the digest in the notes. `deploy/images.json` pins that digest, and bootstrap
writes it into the forge's copy of `primitive.yaml`.

The first push creates the package on the organisation as **private**.
Grading machines pull it anonymously, so someone has to open the package on
the organisation's Packages page once, set its visibility to public, and add
this repo under Manage Actions access so later releases can keep pushing to
it.
