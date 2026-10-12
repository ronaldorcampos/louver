import pytest

from louver import config as cfgmod
from louver.mbfans import MotherboardFans

from .test_control import Rig


def fake_chip(root, name="nct6798", headers=((1, 0, 169), (2, 930, 83), (7, 3060, 255))):
    d = root / "hwmon6"
    d.mkdir(parents=True)
    (d / "name").write_text(name + "\n")
    for i, rpm, pwm in headers:
        (d / f"fan{i}_input").write_text(f"{rpm}\n")
        (d / f"pwm{i}").write_text(f"{pwm}\n")
        (d / f"pwm{i}_enable").write_text("5\n")
    (root / "hwmon1").mkdir()
    (root / "hwmon1" / "name").write_text("nvme\n")
    return d


def read(d, f):
    return int((d / f).read_text())


def test_discovers_chip_and_only_headers_with_tach(tmp_path):
    fake_chip(tmp_path)
    mb = MotherboardFans(tmp_path)
    assert mb.chip == "nct6798" and sorted(mb.headers) == [1, 2, 7]
    assert mb.headers[2].rpm == 930


def test_set_percent_goes_manual_and_release_restores_bios_mode(tmp_path):
    d = fake_chip(tmp_path)
    mb = MotherboardFans(tmp_path)
    mb.set_percent(2, 60)
    assert read(d, "pwm2_enable") == 1 and read(d, "pwm2") == 153
    mb.set_percent(2, 100)
    assert mb.headers[2].original_enable == 5  # remembered once, not overwritten by our manual mode
    mb.release(2)
    assert read(d, "pwm2_enable") == 5 and mb.headers[2].original_enable is None


def test_crash_left_manual_falls_back_to_chip_auto_mode(tmp_path):
    d = fake_chip(tmp_path, name="it8688")
    (d / "pwm2_enable").write_text("1\n")
    mb = MotherboardFans(tmp_path)
    mb.set_percent(2, 50)
    mb.release(2)
    assert read(d, "pwm2_enable") == 2


def mb_cfg(*entries):
    cfg = cfgmod.validate({})
    cfg["motherboard"]["headers"] = list(entries)
    return cfgmod.validate(cfg)


def test_controller_drives_only_opted_in_headers_and_releases_on_shutdown(tmp_path):
    d = fake_chip(tmp_path)
    r = Rig(
        mb_cfg({"index": 2, "name": "Top", "mode": "standard", "sensor": "cpu"}, {"index": 7, "mode": "bios"}),
        mbfans=MotherboardFans(tmp_path),
    )
    r.ctrl.tick()
    assert read(d, "pwm2_enable") == 1 and read(d, "pwm2") == round(45 * 2.55)  # standard @ 50 C
    assert read(d, "pwm7_enable") == 5 and read(d, "pwm7") == 255  # pump on BIOS: untouched
    assert read(d, "pwm1_enable") == 5  # not configured at all: untouched
    st = {h["index"]: h for h in r.ctrl.state()["motherboard"]["headers"]}
    assert st[2]["percent"] == 45 and st[7]["percent"] is None and st[2]["rpm"] == 930
    r.ctrl.shutdown()
    assert read(d, "pwm2_enable") == 5


def test_switching_header_back_to_bios_releases_it(tmp_path):
    d = fake_chip(tmp_path)
    r = Rig(mb_cfg({"index": 2, "mode": "manual", "manual_percent": 70}), mbfans=MotherboardFans(tmp_path))
    r.ctrl.tick()
    assert read(d, "pwm2") == round(70 * 2.55)
    cfg = r.ctrl.cfg
    cfg["motherboard"]["headers"][0]["mode"] = "bios"
    r.ctrl.apply_config(cfg)
    assert read(d, "pwm2_enable") == 5


def test_header_failsafe_without_temperature(tmp_path):
    d = fake_chip(tmp_path)
    r = Rig(
        mb_cfg({"index": 2, "mode": "quiet", "sensor": "cpu"}), temps={"cpu": None}, mbfans=MotherboardFans(tmp_path)
    )
    r.ctrl.tick()
    assert read(d, "pwm2") == 255


def test_unwritable_headers_report_an_error_instead_of_crashing(tmp_path):
    d = fake_chip(tmp_path)
    (d / "pwm2_enable").chmod(0o444)
    r = Rig(mb_cfg({"index": 2, "mode": "full"}), mbfans=MotherboardFans(tmp_path))
    r.ctrl.tick()
    assert "udev" in r.ctrl.state()["motherboard"]["error"]


def test_bad_header_config_rejected():
    with pytest.raises(cfgmod.ConfigError):
        mb_cfg({"index": 2, "mode": "mb_sync"})
    with pytest.raises(cfgmod.ConfigError):
        mb_cfg({"index": 2}, {"index": 2})


def test_pump_header_is_only_bios_or_full():
    for mode in ("quiet", "standard", "high", "manual", "curve"):
        with pytest.raises(cfgmod.ConfigError, match="is a pump"):
            mb_cfg({"index": 2, "mode": mode, "pump": True})
    for mode in ("bios", "full"):
        assert mb_cfg({"index": 2, "mode": mode, "pump": True})["motherboard"]["headers"][0]["pump"] is True
    h = mb_cfg({"index": 2})["motherboard"]["headers"][0]
    assert h["pump"] is False and h["min_percent"] == 0  # defaults, for headers saved before these existed
    with pytest.raises(cfgmod.ConfigError, match="pump must be true or false"):
        mb_cfg({"index": 2, "pump": "yes"})


@pytest.mark.parametrize("bad", [-1, 101, "40", True, None, float("nan")])
def test_header_min_percent_must_be_a_percentage(bad):
    with pytest.raises(cfgmod.ConfigError, match="min_percent must be 0..100"):
        mb_cfg({"index": 2, "mode": "quiet", "min_percent": bad})


def test_header_floor_lifts_manual_and_curve_but_not_the_failsafe(tmp_path):
    d = fake_chip(tmp_path)
    r = Rig(
        mb_cfg(
            {"index": 2, "mode": "manual", "manual_percent": 10, "min_percent": 40},
            {"index": 7, "mode": "quiet", "sensor": "cpu", "min_percent": 60},
        ),
        temps={"cpu": 50.0},
        mbfans=MotherboardFans(tmp_path),
    )
    r.ctrl.tick()
    st = {h["index"]: h for h in r.ctrl.state()["motherboard"]["headers"]}
    assert st[2]["percent"] == 40 and read(d, "pwm2") == round(40 * 2.55)  # manual 10%, floor 40%
    assert st[7]["percent"] == 60  # quiet @ 50 C is below the floor
    r.sensors.values["cpu"] = None
    r.now += 1
    r.ctrl.tick()
    st = {h["index"]: h for h in r.ctrl.state()["motherboard"]["headers"]}
    assert st[7]["percent"] == 100 and st[7]["failsafe"]  # the failsafe still wins


def test_chip_that_appears_after_start_is_picked_up(tmp_path):
    mb = MotherboardFans(tmp_path)
    r = Rig(mb_cfg({"index": 2, "mode": "manual", "manual_percent": 70}), mbfans=mb)
    r.ctrl.tick()
    assert not r.ctrl.state()["motherboard"]["present"]
    d = fake_chip(tmp_path)  # nct6775 loaded after the service started
    r.now += 5
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard"]["present"]
    assert read(d, "pwm2_enable") == 1 and read(d, "pwm2") == round(70 * 2.55)
    assert read(d, "pwm1_enable") == 5  # not opted in: left on BIOS control


def test_new_hwmon_index_keeps_the_bios_mode_to_hand_back(tmp_path):
    d = fake_chip(tmp_path)
    (d / "pwm2_enable").write_text("3\n")  # the BIOS's mode: not the fallback 5
    r = Rig(mb_cfg({"index": 2, "mode": "manual", "manual_percent": 70}), mbfans=MotherboardFans(tmp_path))
    r.ctrl.tick()
    assert read(d, "pwm2_enable") == 1
    d = d.rename(tmp_path / "hwmon9")  # the driver reloaded under a new index...
    (d / "pwm2_enable").write_text("5\n")  # ...and the chip went back to its own curve
    (d / "pwm2").write_text("0\n")
    r.now += 5
    r.ctrl.tick()
    assert read(d, "pwm2_enable") == 1 and read(d, "pwm2") == round(70 * 2.55)
    assert r.ctrl.state()["motherboard"]["error"] == ""
    r.ctrl.shutdown()
    assert read(d, "pwm2_enable") == 3


def test_chip_that_goes_away_and_comes_back_is_still_handed_back(tmp_path):
    d = fake_chip(tmp_path)
    (d / "pwm2_enable").write_text("3\n")
    r = Rig(mb_cfg({"index": 2, "mode": "full"}), mbfans=MotherboardFans(tmp_path))
    r.ctrl.tick()
    kept = d.rename(tmp_path.parent / f"{tmp_path.name}-unloaded")  # driver unloaded
    r.now += 5
    r.ctrl.tick()
    st = r.ctrl.state()["motherboard"]
    assert not st["present"] and st["headers"] == []
    d = kept.rename(tmp_path / "hwmon8")  # loaded again; its registers kept manual mode
    r.now += 5
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard"]["present"] and read(d, "pwm2") == 255
    r.ctrl.shutdown()
    assert read(d, "pwm2_enable") == 3


def test_stale_until_the_directory_names_the_chip_again(tmp_path):
    d = fake_chip(tmp_path)
    mb = MotherboardFans(tmp_path)
    assert not mb.stale()
    (d / "name").write_text("nvme\n")  # hwmon6 now belongs to another device
    assert mb.stale()


def test_a_second_chip_never_takes_over_held_headers(tmp_path):
    d = fake_chip(tmp_path, name="it8688")
    (d / "pwm2_enable").write_text("2\n")
    other = tmp_path / "hwmon7"
    other.mkdir()
    (other / "name").write_text("it8792\n")
    for f, v in (("fan2_input", 900), ("pwm2", 100), ("pwm2_enable", 2)):
        (other / f).write_text(f"{v}\n")
    r = Rig(mb_cfg({"index": 2, "mode": "manual", "manual_percent": 30}), mbfans=MotherboardFans(tmp_path))
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard"]["chip"] == "it8688"
    d = d.rename(tmp_path / "hwmon9")  # renumbered: now it8792 sorts first
    r.now += 5
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard"]["chip"] == "it8688" and read(d, "pwm2_enable") == 1
    assert read(other, "pwm2_enable") == 2 and read(other, "pwm2") == 100  # never opted in: untouched
    r.ctrl.shutdown()
    assert read(d, "pwm2_enable") == 2


def test_shutdown_right_after_a_renumber_still_hands_back(tmp_path):
    d = fake_chip(tmp_path)
    (d / "pwm2_enable").write_text("3\n")
    r = Rig(mb_cfg({"index": 2, "mode": "full"}), mbfans=MotherboardFans(tmp_path))
    r.ctrl.tick()
    d = d.rename(tmp_path / "hwmon9")
    r.now += 2  # before the next 5 s check
    r.ctrl.tick()
    r.ctrl.shutdown()
    assert read(d, "pwm2_enable") == 3


def test_held_header_that_comes_back_without_its_tach_is_still_handed_back(tmp_path):
    d = fake_chip(tmp_path)
    (d / "pwm2_enable").write_text("3\n")
    r = Rig(mb_cfg({"index": 2, "mode": "full"}), mbfans=MotherboardFans(tmp_path))
    r.ctrl.tick()
    d = d.rename(tmp_path / "hwmon9")
    (d / "fan2_input").unlink()
    r.now += 5
    r.ctrl.tick()
    r.ctrl.shutdown()
    assert read(d, "pwm2_enable") == 3


def test_held_chip_gone_leaves_the_other_chip_alone_until_it_returns(tmp_path):
    d = fake_chip(tmp_path, name="it8688")
    (d / "pwm2_enable").write_text("4\n")  # not the ITE fallback 2
    other = tmp_path / "hwmon7"
    other.mkdir()
    (other / "name").write_text("it8792\n")
    for f, v in (("fan2_input", 900), ("pwm2", 100), ("pwm2_enable", 2)):
        (other / f).write_text(f"{v}\n")
    r = Rig(mb_cfg({"index": 2, "mode": "manual", "manual_percent": 30}), mbfans=MotherboardFans(tmp_path))
    r.ctrl.tick()
    kept = d.rename(tmp_path.parent / f"{tmp_path.name}-unloaded")
    r.now += 5
    r.ctrl.tick()
    assert not r.ctrl.state()["motherboard"]["present"]
    assert read(other, "pwm2_enable") == 2 and read(other, "pwm2") == 100
    d = kept.rename(tmp_path / "hwmon9")
    r.now += 5
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard"]["chip"] == "it8688"
    r.ctrl.shutdown()
    assert read(d, "pwm2_enable") == 4 and read(other, "pwm2_enable") == 2
