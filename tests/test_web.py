"""Runs the UI's pure-JS tests (tests/web) under pytest when Node is installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_web_model():
    files = sorted(str(f) for f in Path(__file__).with_name("web").glob("*.test.mjs"))
    assert files
    r = subprocess.run([NODE, "--test", *files], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
