import pytest

from louver import instance


@pytest.fixture(autouse=True)
def _private_runtime(tmp_path_factory, monkeypatch):
    """Every test gets a runtime dir and a machine-wide lock dir of its own,
    so nothing here can reach, replace or delete the session's real Louver
    socket ($XDG_RUNTIME_DIR/louver/api.sock) or take /run/louver/hardware.lock.
    Short paths: a Unix socket's path must fit in 108 bytes."""
    run = tmp_path_factory.mktemp("run")
    run.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    monkeypatch.setattr(instance, "SYSTEM_LOCK_DIR", tmp_path_factory.mktemp("lock"))
