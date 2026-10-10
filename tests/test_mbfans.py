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
    r = Rig(mb_cfg({"index": 2, "name": "Top", "mode": "standard", "sensor": "cpu"}, {"index": 7, "mode": "bios"}))
    r.ctrl.mb = MotherboardFans(tmp_path)
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
    r = Rig(mb_cfg({"index": 2, "mode": "manual", "manual_percent": 70}))
    r.ctrl.mb = MotherboardFans(tmp_path)
    r.ctrl.tick()
    assert read(d, "pwm2") == round(70 * 2.55)
    cfg = r.ctrl.cfg
    cfg["motherboard"]["headers"][0]["mode"] = "bios"
    r.ctrl.apply_config(cfg)
    assert read(d, "pwm2_enable") == 5


def test_header_failsafe_without_temperature(tmp_path):
    d = fake_chip(tmp_path)
    r = Rig(mb_cfg({"index": 2, "mode": "quiet", "sensor": "cpu"}), temps={"cpu": None})
    r.ctrl.mb = MotherboardFans(tmp_path)
    r.ctrl.tick()
    assert read(d, "pwm2") == 255


def test_unwritable_headers_report_an_error_instead_of_crashing(tmp_path):
    d = fake_chip(tmp_path)
    (d / "pwm2_enable").chmod(0o444)
    r = Rig(mb_cfg({"index": 2, "mode": "full"}))
    r.ctrl.mb = MotherboardFans(tmp_path)
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
    )
    r.ctrl.mb = MotherboardFans(tmp_path)
    r.ctrl.tick()
    st = {h["index"]: h for h in r.ctrl.state()["motherboard"]["headers"]}
    assert st[2]["percent"] == 40 and read(d, "pwm2") == round(40 * 2.55)  # manual 10%, floor 40%
    assert st[7]["percent"] == 60  # quiet @ 50 C is below the floor
    r.sensors.values["cpu"] = None
    r.now += 1
    r.ctrl.tick()
    st = {h["index"]: h for h in r.ctrl.state()["motherboard"]["headers"]}
    assert st[7]["percent"] == 100 and st[7]["failsafe"]  # the failsafe still wins
