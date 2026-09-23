"""Guards against packaging mistakes (e.g. a .gitignore pattern swallowing source code)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(),
                                reason="needs a git checkout")


def test_no_source_file_is_gitignored():
    files = [str(p.relative_to(ROOT)) for d in ("agent", "scripts", "deploy", "config", "tests")
             for p in (ROOT / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    r = subprocess.run(["git", "check-ignore", "--no-index", *files], cwd=ROOT, capture_output=True, text=True)
    ignored = [f for f in r.stdout.splitlines() if f]
    assert ignored == [], f"source files ignored by .gitignore: {ignored}"


def test_runtime_state_is_ignored():
    for f in ("data/agent.db", "logs/agent.log", ".env"):
        assert subprocess.run(["git", "check-ignore", "-q", "--no-index", f], cwd=ROOT).returncode == 0, f
