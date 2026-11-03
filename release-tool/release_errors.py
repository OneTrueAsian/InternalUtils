"""Shared release errors and repository path guard."""
from pathlib import Path

class ReleaseError(RuntimeError):
    pass


class CommandError(ReleaseError):
    pass


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ReleaseError(f"Profile path escapes the repository: {relative}")
    return path


