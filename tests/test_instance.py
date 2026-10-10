import logging
import os
import signal
import stat

import pytest

from louver import __main__ as cli
from louver import daemon, instance, mbfans


@pytest.fixture
def no_hardware(monkeypatch):
    """Should the lock ever let these commands through, they fail here
    instead of reaching the hub, the Aura controller or the fan headers."""

    def reached(*a, **kw):
        pytest.fail("a hardware command got past the lock")

    monkeypatch.setattr(cli, "enumerate_hidraw", reached)
    monkeypatch.setattr(cli, "HidrawTransport", reached)
    monkeypatch.setattr(cli.aura, "find_controllers", reached)
    monkeypatch.setattr(mbfans, "MotherboardFans", reached)
    monkeypatch.setattr(daemon, "Controller", reached)


def test_second_holder_is_refused_until_the_first_lets_go(tmp_path):
    p = tmp_path / "louver.lock"
    first = instance.acquire(p)
    assert first is not None
    assert instance.try_acquire(p) == instance.Busy(os.getpid())
    assert instance.acquire(p) is None
    first.close()  # what process exit does, crash included
    again = instance.acquire(p)
    assert again is not None
    again.close()


def test_hardware_commands_refuse_while_another_copy_runs(tmp_path, monkeypatch, capsys, no_hardware):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    held = instance.acquire()
    try:
        assert cli.main(["daemon"]) == cli.ALREADY_RUNNING
        assert cli.main(["probe"]) == cli.ALREADY_RUNNING
        assert cli.main(["aura-probe"]) == cli.ALREADY_RUNNING
        assert cli.main(["mb-test", "2", "30"]) == cli.ALREADY_RUNNING
    finally:
        held.close()
    err = capsys.readouterr().err
    assert err.count("already running") == 4 and f"pid {os.getpid()}" in err


# ---------------- the machine-wide lock ----------------
def test_machine_wide_lock_refuses_a_second_copy_even_with_another_user_lock(tmp_path):
    shared = tmp_path / "lock"
    shared.mkdir()
    first = instance.try_acquire_hardware(tmp_path / "a.lock", shared)
    assert isinstance(first, instance.Lock)
    # Another account: its own per-user lock is free, the machine-wide one isn't.
    assert instance.try_acquire_hardware(tmp_path / "b.lock", shared) == instance.Busy(os.getpid(), elsewhere=True)
    assert instance.acquire(tmp_path / "b.lock") is not None  # the per-user lock was let go again
    first.close()
    again = instance.try_acquire_hardware(tmp_path / "b.lock", shared)
    assert isinstance(again, instance.Lock)
    again.close()


def test_machine_wide_lock_file_is_created_for_the_group(tmp_path):
    shared = tmp_path / "lock"
    shared.mkdir()
    old = os.umask(0o022)
    try:
        held = instance.try_acquire_hardware(tmp_path / "a.lock", shared)
    finally:
        os.umask(old)
    st = os.stat(shared / "hardware.lock")
    assert stat.S_IMODE(st.st_mode) == 0o660 and (shared / "hardware.lock").read_text() == f"{os.getpid()}\n"
    held.close()


def test_existing_machine_wide_lock_is_opened_without_o_creat(tmp_path, monkeypatch):
    shared = tmp_path / "lock"
    shared.mkdir()
    (shared / "hardware.lock").write_text("")
    real, calls = os.open, []

    def spy(path, flags, *a):
        calls.append((str(path), flags))
        return real(path, flags, *a)

    monkeypatch.setattr(instance.os, "open", spy)
    held = instance.try_acquire_hardware(tmp_path / "a.lock", shared)
    assert isinstance(held, instance.Lock)
    mine = [f for p, f in calls if p == str(shared / "hardware.lock")]
    assert mine and not any(f & os.O_CREAT for f in mine) and all(f & os.O_NOFOLLOW for f in mine)
    held.close()


def test_machine_wide_lock_another_account_made_read_only_still_locks(tmp_path, monkeypatch):
    shared = tmp_path / "lock"
    shared.mkdir()
    (shared / "hardware.lock").write_text("4242\n")
    (shared / "hardware.lock").chmod(0o444)  # what a file of another account's looks like to us
    if os.access(shared / "hardware.lock", os.W_OK):
        pytest.skip("running as root: the file stays writable")
    first = instance.try_acquire_hardware(tmp_path / "a.lock", shared)
    assert isinstance(first, instance.Lock)
    assert (shared / "hardware.lock").read_text() == "4242\n"  # no pid written where we can't
    assert instance.try_acquire_hardware(tmp_path / "b.lock", shared) == instance.Busy(4242, elsewhere=True)
    first.close()


def test_machine_wide_lock_symlink_is_not_followed(tmp_path):
    shared = tmp_path / "lock"
    shared.mkdir()
    target = tmp_path / "victim"
    target.write_text("keep\n")
    (shared / "hardware.lock").symlink_to(target)
    held = instance.try_acquire_hardware(tmp_path / "a.lock", shared)
    assert isinstance(held, instance.Lock)  # the per-user lock alone
    assert target.read_text() == "keep\n"
    held.close()


def test_machine_wide_lock_dir_we_cant_enter_falls_back_to_the_user_lock(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="louver")
    shared = tmp_path / "lock"
    shared.mkdir()
    (shared / "hardware.lock").write_text("")
    shared.chmod(0)  # /run/louver (3770 root:plugdev) to an account outside plugdev
    try:
        if os.access(shared, os.X_OK):
            pytest.skip("running as root: every directory can be entered")
        held = instance.try_acquire_hardware(tmp_path / "a.lock", shared)
    finally:
        shared.chmod(0o700)
    assert isinstance(held, instance.Lock) and "not in plugdev" in caplog.text
    held.close()


def test_no_machine_wide_lock_dir_falls_back_to_the_user_lock(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="louver")
    held = instance.try_acquire_hardware(tmp_path / "a.lock", tmp_path / "missing")
    assert isinstance(held, instance.Lock)
    assert "no machine-wide lock" in caplog.text
    assert instance.try_acquire(tmp_path / "a.lock") == instance.Busy(os.getpid())  # still held
    held.close()


def test_hardware_commands_name_another_accounts_copy(tmp_path, monkeypatch, capsys, no_hardware):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    other = instance.try_acquire_hardware(tmp_path / "other-user.lock")  # conftest's machine-wide dir
    try:
        for cmd in (["daemon"], ["probe"], ["aura-probe"], ["mb-test", "2", "30"]):
            assert cli.main(cmd) == cli.ALREADY_RUNNING, cmd
    finally:
        other.close()
    err = capsys.readouterr().err
    assert err.count(f"already running (pid {os.getpid()}, maybe another account's)") == 4


@pytest.mark.parametrize("mode", [0o666, 0o444])  # 0o444: what made the read-only open hang
def test_a_fifo_as_the_machine_wide_lock_is_refused_without_hanging(tmp_path, caplog, mode):
    caplog.set_level(logging.DEBUG, logger="louver")
    shared = tmp_path / "lock"
    shared.mkdir()
    os.mkfifo(shared / "hardware.lock")
    (shared / "hardware.lock").chmod(mode)
    old = signal.signal(signal.SIGALRM, lambda *_: pytest.fail("open hung on the FIFO"))
    signal.alarm(5)
    try:
        held = instance.try_acquire_hardware(tmp_path / "a.lock", shared)
        pid = instance._shared_pid(shared / "hardware.lock")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert isinstance(held, instance.Lock) and pid is None  # the per-user lock alone
    assert "not a regular file" in caplog.text
    held.close()
