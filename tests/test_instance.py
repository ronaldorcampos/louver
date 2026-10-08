import os

from unihub import __main__ as cli
from unihub import instance


def test_second_holder_is_refused_until_the_first_lets_go(tmp_path):
    p = tmp_path / "unihub.lock"
    first = instance.acquire(p)
    assert first is not None and instance.holder(p) == os.getpid()
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
