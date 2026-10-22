"""Re-vendors the official tscircuit agent skill into .claude/skills/tscircuit (portable: no rm / git shell tools)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

URL = "https://github.com/tscircuit/skill"
TARGET = Path(".claude/skills/tscircuit")


def _writable(func, path, _exc):  # git's object files are read-only (Windows refuses to delete them)
    os.chmod(path, stat.S_IWRITE)
    func(path)


def main() -> int:
    shutil.rmtree(TARGET, ignore_errors=True)
    result = subprocess.run(["git", "clone", "--depth", "1", URL, str(TARGET)], check=False)
    if result.returncode != 0:
        return result.returncode
    shutil.rmtree(TARGET / ".git", onerror=_writable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
