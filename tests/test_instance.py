import fcntl
import os

from louver import __main__ as cli
from louver import instance


def test_second_holder_is_refused_until_the_first_lets_go(tmp_path):
    p = tmp_path / "louver.lock"
    first = instance.acquire(p)
    assert first is not None
    assert instance.try_acquire(p) == instance.Busy("Louver", os.getpid())
    assert instance.acquire(p) is None
    first.close()  # what process exit does, crash included
    again = instance.acquire(p)
    assert again is not None
    again.close()


def test_hardware_commands_refuse_while_another_copy_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    held = instance.acquire()
    try:
        assert cli.main(["daemon"]) == cli.ALREADY_RUNNING
        assert cli.main(["probe"]) == cli.ALREADY_RUNNING
        assert cli.main(["mb-test", "2", "30"]) == cli.ALREADY_RUNNING
    finally:
        held.close()
    err = capsys.readouterr().err
    assert err.count("already running") == 3 and f"pid {os.getpid()}" in err


def test_a_still_running_unihub_blocks_louver(tmp_path):
    p = tmp_path / "louver.lock"
    old = open(tmp_path / instance.LEGACY_NAME, "a+")  # what the old app's daemon holds
    fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
    old.write("4242\n")
    old.flush()
    try:
        assert instance.try_acquire(p) == instance.Busy("UniHub", 4242)
        assert instance.acquire(p) is None
        mine = open(p, "a+")  # the refused attempts must not have left louver.lock held
        fcntl.flock(mine, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mine.close()
    finally:
        old.close()
    lock = instance.acquire(p)
    assert lock is not None
    lock.close()
