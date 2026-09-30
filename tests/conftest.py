"""Fixtures for running the image the way the harness does, and the contract checks.

The image tests build the image from this checkout (or use the one named by
`PRIMITIVE_IMAGE`) and start it with the flags every step container gets: no
network, a read-only root, every capability dropped, no new privileges,
Docker's built-in seccomp profile, user 65532, no swap, the declared memory
and pids limits, one CPU, a small noexec tmpfs at /tmp and the working
directory at /work. They are skipped when Docker is not reachable.

The contract checks use `primitive.schema.json` from `PRIMITIVE_SCHEMA`, or
from a runner checkout beside this one when it has the version 3 declaration.

The in-process tests import the program, which needs Linux, so elsewhere only
the image and declaration tests are collected.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from support import (
    NAME,
    ROOT,
    SIBLING_SCHEMA,
    Check,
    RunImage,
    declaration,
    open_up,
    sandbox_flags,
)

collect_ignore = [] if sys.platform == "linux" else ["test_compile.py"]


@pytest.fixture(scope="session")
def image() -> str:
    """The image under test: `PRIMITIVE_IMAGE`, or built from this checkout."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker is not reachable")
    named = os.environ.get("PRIMITIVE_IMAGE")
    if named:
        return named
    tag = f"{NAME}:test"
    subprocess.run(["docker", "build", "--quiet", "--tag", tag, str(ROOT)], check=True)
    return tag


@pytest.fixture
def run_image(image: str) -> RunImage:
    """Run the image once over a working directory and return its outputs.json."""

    def run(work: Path, limits: dict[str, int] | None = None) -> dict[str, Any]:
        limits = limits or declaration()["limits"]
        open_up(work)
        command = ["docker", "run", "--rm", *sandbox_flags(limits)]
        command += ["--volume", f"{work}:/work", image]
        subprocess.run(command, check=True, timeout=limits["time_ms"] / 1000 + 60)
        document: dict[str, Any] = json.loads(
            (work / "outputs.json").read_text(encoding="utf-8")
        )
        return document

    return run


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    """The runner's primitive.schema.json at contract version 3."""
    named = os.environ.get("PRIMITIVE_SCHEMA")
    path = Path(named) if named else SIBLING_SCHEMA
    if not path.is_file():
        pytest.skip("no primitive.schema.json; set PRIMITIVE_SCHEMA")
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if "declaration" not in document.get("$defs", {}):
        pytest.skip(f"{path} is not the version 3 contract")
    return document


@pytest.fixture
def check(schema: dict[str, Any]) -> Check:
    """Validate a document against one part of the contract.

    For `outputs_file`, every entry must also carry each output the
    declaration names as required, and nothing it does not name.
    """
    registry: Registry[Any] = Registry().with_resource(
        schema["$id"], Resource.from_contents(schema)
    )

    def validate(document: dict[str, Any], part: str) -> None:
        reference = {"$ref": f"{schema['$id']}#/$defs/{part}"}
        Draft202012Validator(reference, registry=registry).validate(document)
        if part != "outputs_file" or "error" in document:
            return
        declared = declaration()["outputs"]
        required = {name for name, port in declared.items() if not port.get("optional")}
        if "batch" in document:
            entries = [entry["outputs"] for entry in document["batch"]]
        else:
            entries = [document["outputs"]]
        for outputs in entries:
            assert required <= set(outputs) <= set(declared), outputs

    return validate
