"""Shared release errors and repository path guard."""
from pathlib import Path
import re

class ReleaseError(RuntimeError):
    pass


class CommandError(ReleaseError):
    pass


def reserved_test_version(repository, tag):
    """True for Vault Spend's reserved test versions, v1.3.90 to v1.3.99. Leading zeros count, so v1.3.095 is 95. Other applications may use these versions."""
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag or "")
    return bool(match) and repository.lower() == "onetrueasian/vault-spend" and (int(match[1]), int(match[2])) == (1, 3) and 90 <= int(match[3]) <= 99


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ReleaseError(f"Profile path escapes the repository: {relative}")
    return path


