"""This repository is public. The API key belongs in .env.local (ignored), never in a tracked file."""
import subprocess

from app.config import ROOT


def test_env_example_carries_no_key():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("MODEL_API_KEY="):
            assert line == "MODEL_API_KEY=", "put the key in .env.local, not .env.example"


def test_env_local_is_ignored_by_git():
    result = subprocess.run(["git", "check-ignore", "-q", ".env.local"], cwd=ROOT)
    assert result.returncode == 0
