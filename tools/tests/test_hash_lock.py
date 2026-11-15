"""app/requirements.lock: the hashed twin of app/constraints.txt (audit O-2).

The Dockerfile installs the lock with `--require-hashes`, so the image takes the
exact bytes recorded here or fails to build. These tests keep the two files in
step, and pin the wheel filter in tools/hash_lock.py, which decides which files'
hashes are recorded at all -- drop the aarch64 wheels by mistake and the arm64
leg of publish.yml fails after merge, which is the expensive place to find out.

No network: the generator is driven with canned PyPI responses.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "app" / "requirements.lock"
CONSTRAINTS = ROOT / "app" / "constraints.txt"
DOCKERFILE = ROOT / "app" / "Dockerfile"

sys.path.insert(0, str(ROOT / "tools"))
import hash_lock  # noqa: E402

_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_entries() -> dict[str, tuple[str, list[str]]]:
    out = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        pin, *hashes = line.split()
        name, _, version = pin.partition("==")
        out[_norm(name)] = (version, hashes)
    return out


def test_lock_pins_exactly_what_constraints_pins():
    constraints = {_norm(n): v for n, v in hash_lock.read_pins(CONSTRAINTS.read_text(encoding="utf-8").splitlines())}
    lock = {n: v for n, (v, _) in _lock_entries().items()}
    assert lock == constraints, (
        "app/requirements.lock and app/constraints.txt disagree — run `make lock`, "
        "which rewrites both together"
    )


def test_every_locked_package_carries_at_least_one_sha256():
    for name, (_, hashes) in _lock_entries().items():
        assert hashes, f"{name} has no --hash in app/requirements.lock"
        assert all(_HASH.fullmatch(h) for h in hashes), f"{name}: malformed hash entry {hashes}"


def test_dockerfile_installs_the_lock_with_require_hashes_by_default():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ARG PIP_LOCK=hashes$", text, re.MULTILINE), "hash-checked install must be the default"
    assert re.search(r"pip install --require-hashes[^\n]*-r /app/app/requirements\.lock", text)
    assert "COPY app/requirements.lock /app/app/requirements.lock" in text


@pytest.mark.parametrize(
    "filename,ok",
    [
        ("numpy-2.5.4-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl", True),
        ("numpy-2.5.4-cp314-cp314-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl", True),
        ("numpy-2.5.4-cp314-cp314t-manylinux_2_28_x86_64.whl", False),   # free-threaded
        ("numpy-2.5.4-cp313-cp313-manylinux_2_28_x86_64.whl", False),    # other interpreter
        ("numpy-2.5.4-cp314-cp314-musllinux_1_2_x86_64.whl", False),     # not glibc
        ("numpy-2.5.4-cp314-cp314-win_amd64.whl", False),
        ("numpy-2.5.4-cp314-cp314-macosx_14_0_arm64.whl", False),
        ("cryptography-50.0.2-cp311-abi3-manylinux_2_34_aarch64.whl", True),  # stable ABI
        ("cryptography-50.0.2-cp315-abi3-manylinux_2_34_x86_64.whl", False),  # needs a newer CPython
        ("requests-2.34.2-py3-none-any.whl", True),
        ("six-1.17.0-py2.py3-none-any.whl", True),
        ("not-a-wheel-1.0.tar.gz", False),
    ],
)
def test_wheel_filter_keeps_exactly_what_the_image_can_install(filename, ok):
    assert hash_lock.wheel_is_installable(filename) is ok


def _release(*files):
    return {"urls": [dict(f) for f in files]}


def _file(filename, digest, packagetype="bdist_wheel", yanked=False):
    return {"filename": filename, "packagetype": packagetype, "yanked": yanked, "digests": {"sha256": digest}}


def test_build_lock_records_every_installable_wheel_and_nothing_else():
    a, b, c, d = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
    releases = {
        ("demo", "1.0"): _release(
            _file("demo-1.0-cp314-cp314-manylinux_2_28_x86_64.whl", b),
            _file("demo-1.0-cp314-cp314-manylinux_2_28_aarch64.whl", a),
            _file("demo-1.0-cp314-cp314-win_amd64.whl", c),
            _file("demo-1.0.tar.gz", d, packagetype="sdist"),
        ),
        ("pip", "26.3"): _release(_file("pip-26.3-py3-none-any.whl", c)),
    }
    text = hash_lock.build_lock([("demo", "1.0"), ("pip", "26.3")], fetch=lambda n, v: releases[(n, v)])
    body = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert body == [
        f"demo==1.0 --hash=sha256:{a} --hash=sha256:{b}",
        f"pip==26.3 --hash=sha256:{c}",
    ]


def test_build_lock_refuses_a_pin_with_no_installable_wheel():
    releases = {("old", "0.1"): _release(_file("old-0.1-cp312-cp312-manylinux_2_28_x86_64.whl", "e" * 64),
                                         _file("old-0.1-cp314-cp314-manylinux_2_28_x86_64.whl", "f" * 64, yanked=True))}
    with pytest.raises(SystemExit, match="old==0.1"):
        hash_lock.build_lock([("old", "0.1")], fetch=lambda n, v: releases[(n, v)])


def test_read_pins_rejects_anything_but_exact_pins():
    assert hash_lock.read_pins(["# c", "", "a==1 # why", "B_c==2.0"]) == [("a", "1"), ("B_c", "2.0")]
    with pytest.raises(ValueError):
        hash_lock.read_pins(["a>=1"])
