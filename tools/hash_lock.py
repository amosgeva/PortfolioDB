"""Write app/requirements.lock: app/constraints.txt with a sha256 per wheel.

app/constraints.txt says *which* versions the image contains. It does not say
*which bytes*: pip downloads whatever file the index serves under that name and
version. With `--require-hashes`, pip refuses any file whose digest is not
listed, so a tampered upload, a compromised mirror or a man in the middle gets a
build failure instead of an image.

This reads the pins, asks PyPI's JSON API for the files of each exact release,
and keeps the wheels the image can install: CPython 3.14 (or pure Python, or the
stable ABI) on glibc Linux for amd64 and arm64 -- the two platforms publish.yml
builds. One line per package, so the Dockerfile can pull pip's line out with
grep and upgrade pip first.

Standard library only, on purpose: `make lock` runs it inside the application
image right after the freeze (the image carries a copy at
/app/tools/hash_lock.py), and nothing here may depend on what that freeze
contains.

    python tools/hash_lock.py app/constraints.txt > app/requirements.lock
    python tools/hash_lock.py - < app/constraints.txt > app/requirements.lock

Exit status is non-zero, and nothing is printed to stdout, if any pin has no
installable wheel -- the image build would fail on it anyway, later and less
clearly.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from typing import Callable, Iterable

INDEX = "https://pypi.org/pypi/{name}/{version}/json"

# The interpreter and platforms the image is built for. The Dockerfile's FROM
# line names python:3.14; publish.yml builds linux/amd64 and linux/arm64.
PYTHON_TAG = "cp314"
ARCHES = ("x86_64", "aarch64")

HEADER = """\
# Hash-locked install set for the application image. GENERATED -- do not edit.
#
# The same pins as app/constraints.txt, each with the sha256 of every wheel the
# image may install (CPython 3.14 / pure Python / abi3, glibc Linux, amd64 and
# arm64). The Dockerfile installs this file with --require-hashes, so pip
# rejects any file whose bytes differ from the ones recorded here.
#
# Regenerate with `make lock` (it rewrites both files together). CI fails if the
# pins here and in app/constraints.txt ever disagree.
"""

_WHEEL = re.compile(
    r"^(?P<dist>[^-]+)-(?P<ver>[^-]+)(?:-(?P<build>\d[^-]*))?"
    r"-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$"
)


def wheel_is_installable(filename: str) -> bool:
    """True if pip in the image (cp314, manylinux, amd64/arm64) could pick it."""
    m = _WHEEL.match(filename)
    if not m:
        return False
    pys = m["py"].split(".")
    abis = m["abi"].split(".")
    plats = m["plat"].split(".")

    platform_ok = any(
        p == "any" or (p.startswith("manylinux") and p.endswith(ARCHES)) for p in plats
    )
    if not platform_ok:
        return False

    if "none" in abis and any(p == "py3" or p.startswith("py3") or p == PYTHON_TAG for p in pys):
        return True
    if PYTHON_TAG in abis and PYTHON_TAG in pys:
        return True
    if "abi3" in abis:
        # abi3 wheels are tagged with the *oldest* CPython they support.
        for p in pys:
            mm = re.fullmatch(r"cp3(\d+)", p)
            if mm and int(mm.group(1)) <= int(PYTHON_TAG[3:]):
                return True
    return False


def read_pins(lines: Iterable[str]) -> list[tuple[str, str]]:
    pins = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        if not sep or not version:
            raise ValueError(f"not an exact pin: {raw.strip()!r}")
        pins.append((name.strip(), version.strip()))
    return pins


def _fetch(name: str, version: str) -> dict:
    req = urllib.request.Request(
        INDEX.format(name=name, version=version),
        headers={"Accept": "application/json", "User-Agent": "portfoliodb-hash-lock"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https URL
        return json.load(resp)


def hashes_for(release: dict) -> list[str]:
    out = set()
    for f in release.get("urls", []):
        if f.get("packagetype") != "bdist_wheel" or f.get("yanked"):
            continue
        if wheel_is_installable(f.get("filename", "")):
            digest = (f.get("digests") or {}).get("sha256")
            if digest:
                out.add(digest)
    return sorted(out)


def build_lock(pins: list[tuple[str, str]], fetch: Callable[[str, str], dict] = _fetch) -> str:
    lines = [HEADER]
    missing = []
    for name, version in pins:
        digests = hashes_for(fetch(name, version))
        if not digests:
            missing.append(f"{name}=={version}")
            continue
        lines.append(f"{name}=={version} " + " ".join(f"--hash=sha256:{d}" for d in digests) + "\n")
    if missing:
        raise SystemExit(
            "no installable cp314 manylinux wheel on PyPI for: " + ", ".join(missing)
        )
    return "".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[1] == "-":
        # `make lock` feeds the freshly frozen pins on stdin: the image it runs
        # in was built before they existed.
        pins = read_pins(sys.stdin)
    else:
        with open(argv[1], encoding="utf-8") as fh:
            pins = read_pins(fh)
    # Build the whole text first: a failure half-way must not leave a truncated
    # lock behind on the caller's redirect. Bytes, so a Windows host does not
    # turn the line endings into CRLF on the way out.
    text = build_lock(pins)
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
