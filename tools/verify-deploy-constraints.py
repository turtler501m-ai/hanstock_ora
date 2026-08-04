"""Verify every deploy requirement has one compatible exact constraint."""

from __future__ import annotations

from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parent.parent
CONSTRAINTS = ROOT / "constraints-deploy.txt"
VM_LOCK = ROOT / "constraints" / "vm-python.lock"
VOICE_LOCK = ROOT / "constraints" / "voice-windows.lock"
REQUIREMENTS = (
    ROOT / "requirements-core.txt",
    ROOT / "requirements-integrations.txt",
)


def _requirement_lines(path: Path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith(("#", "-r")):
            yield Requirement(line)


def _lock_entries(path: Path) -> dict[str, tuple[Requirement, int]]:
    """Return locked requirements and hash counts, rejecting unhashed entries."""
    entries: dict[str, tuple[Requirement, int]] = {}
    current: Requirement | None = None
    hash_count = 0
    for raw in [*path.read_text(encoding="utf-8").splitlines(), ""]:
        stripped = raw.strip()
        if stripped and not stripped.startswith(("#", "--hash=")) and not raw[:1].isspace():
            if current is not None:
                name = canonicalize_name(current.name)
                if hash_count == 0:
                    raise SystemExit(f"unhashed lock entry in {path.name}: {current}")
                entries[name] = (current, hash_count)
            current = Requirement(stripped.removesuffix("\\").strip())
            hash_count = 0
        elif stripped.startswith("--hash=sha256:"):
            hash_count += 1
        elif not stripped and current is not None:
            name = canonicalize_name(current.name)
            if hash_count == 0:
                raise SystemExit(f"unhashed lock entry in {path.name}: {current}")
            entries[name] = (current, hash_count)
            current = None
            hash_count = 0
    return entries


def main() -> int:
    lock_text = VM_LOCK.read_text(encoding="utf-8")
    if "pip-compile with Python 3.10" not in lock_text:
        raise SystemExit("VM lock must be generated with Python 3.10 on the Linux deployment target")
    vm_entries = _lock_entries(VM_LOCK)
    _lock_entries(VOICE_LOCK)
    pins: dict[str, Requirement] = {}
    for requirement in _requirement_lines(CONSTRAINTS):
        name = canonicalize_name(requirement.name)
        exact = [item for item in requirement.specifier if item.operator == "=="]
        if len(exact) != 1 or len(list(requirement.specifier)) != 1:
            raise SystemExit(f"constraint must be one exact pin: {requirement}")
        if name in pins:
            raise SystemExit(f"duplicate constraint: {name}")
        pins[name] = requirement

    missing = []
    incompatible = []
    for path in REQUIREMENTS:
        for requirement in _requirement_lines(path):
            name = canonicalize_name(requirement.name)
            pin = pins.get(name)
            if pin is None:
                missing.append(f"{path.name}:{requirement}")
                continue
            version = next(iter(pin.specifier)).version
            if version not in requirement.specifier:
                incompatible.append(f"{requirement} vs {pin}")
            locked = vm_entries.get(name)
            if locked is None:
                missing.append(f"{VM_LOCK.name}:{requirement}")
            elif version not in locked[0].specifier:
                incompatible.append(f"lock {locked[0]} vs constraint {pin}")

    for name, (locked, _hash_count) in vm_entries.items():
        pin = pins.get(name)
        if pin is None:
            continue
        version = next(iter(pin.specifier)).version
        if version not in locked.specifier:
            incompatible.append(f"lock {locked} vs constraint {pin}")

    if missing or incompatible:
        details = [*(f"missing {item}" for item in missing), *(f"incompatible {item}" for item in incompatible)]
        raise SystemExit("\n".join(details))
    print(f"deploy constraints verified: {len(pins)} exact pins")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
