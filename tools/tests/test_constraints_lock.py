"""app/constraints.txt is the tested resolution of app/requirements.txt.

The Dockerfile installs `-c constraints.txt -r requirements.txt`, so a package
named in requirements but absent from constraints would resolve afresh on
every build — the drift the lock exists to stop. And a pin outside the range
requirements declares means one of the two files was edited without the other.

Pure text assertions; the CI image job does the real check (the image's
`pip freeze` must equal the file). This one runs everywhere, with no Docker.
"""

from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "app" / "requirements.txt"
CONSTRAINTS = ROOT / "app" / "constraints.txt"
DOCKERFILE = ROOT / "app" / "Dockerfile"


def _requirements() -> list[Requirement]:
    out = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(Requirement(line))
    return out


def _pins() -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, version = line.partition("==")
        assert version, f"constraints.txt line is not an exact pin: {line!r}"
        pins[name.lower().replace("_", "-")] = Version(version)
    return pins


def test_every_requirement_is_pinned_within_its_range():
    pins = _pins()
    for req in _requirements():
        name = req.name.lower().replace("_", "-")
        assert name in pins, f"{req.name} is in requirements.txt but not pinned in constraints.txt — run `make lock`"
        assert pins[name] in req.specifier, (
            f"constraints.txt pins {req.name}=={pins[name]}, outside requirements.txt's {req.specifier}"
        )


def test_constraints_are_exact_pins_only():
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        body = line.split("#", 1)[0].strip()
        if body:
            assert re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[A-Za-z0-9_.+!-]+", body), f"not an exact pin: {line!r}"


def test_dockerfile_installs_with_the_constraints_and_a_digest_pinned_base():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"pip install[^\n]*-c /app/app/constraints\.txt", text), (
        "the Dockerfile must install with `-c constraints.txt` or the lock is decorative"
    )
    assert re.search(r"^FROM python:[0-9.]+-slim@sha256:[0-9a-f]{64}", text, re.MULTILINE), (
        "the base image must be pinned by digest"
    )
