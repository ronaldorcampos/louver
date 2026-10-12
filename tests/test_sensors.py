from louver.sensors import RESCAN_EVERY, SensorHub


def coretemp(root, n=2, milli=50000):
    d = root / f"hwmon{n}"
    d.mkdir(parents=True)
    (d / "name").write_text("coretemp\n")
    (d / "temp1_input").write_text(f"{milli}\n")
    (d / "temp1_label").write_text("Package id 0\n")
    return d


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def hub(root, clock):
    return SensorHub(root, nvidia_smi=None, clock=clock)


def test_sensor_that_appears_after_start_is_found_on_a_later_read(tmp_path):
    clock = Clock()
    s = hub(tmp_path, clock)
    assert s.read("cpu") is None and s.missing("cpu") == ["cpu"]
    coretemp(tmp_path)
    clock.now += RESCAN_EVERY - 1
    assert s.read("cpu") is None  # scanned too recently: not every read walks /sys
    clock.now += 1
    assert s.read("cpu") == 50.0 and s.missing("cpu") == []
    assert [x["id"] for x in s.list()][:1] == ["cpu"]


def test_sensor_whose_hwmon_index_changed_is_found_again(tmp_path):
    clock = Clock()
    d = coretemp(tmp_path, n=2)
    s = hub(tmp_path, clock)
    assert s.read("hwmon:coretemp:Package id 0") == 50.0
    d.rename(tmp_path / "hwmon7")  # the driver reloaded under a new index
    clock.now += RESCAN_EVERY
    assert s.read("cpu") == 50.0 and s.read("hwmon:coretemp:Package id 0") == 50.0


def test_noise_reading_does_not_rescan(tmp_path):
    clock = Clock()
    coretemp(tmp_path, milli=127000)  # an unconnected probe: readable, implausible
    s = hub(tmp_path, clock)
    scanned = s._scanned
    clock.now += RESCAN_EVERY
    assert s.read("cpu") is None and s._scanned == scanned


def nvidia_smi(tmp_path):
    """A stub nvidia-smi printing nv.out (one temperature per GPU), or failing without it."""
    exe = tmp_path / "nvidia-smi"
    exe.write_text('#!/bin/sh\ncat "$(dirname "$0")/nv.out" || exit 1\n')
    exe.chmod(0o755)
    return exe, tmp_path / "nv.out"


def k10temp(root):
    d = root / "hwmon0"
    d.mkdir(parents=True)
    (d / "name").write_text("k10temp\n")
    (d / "temp1_input").write_text("55000\n")
    (d / "temp1_label").write_text("Tctl\n")


def amdgpu(root):
    d = root / "hwmon3"
    d.mkdir(parents=True)
    (d / "name").write_text("amdgpu\n")
    (d / "temp1_input").write_text("40000\n")
    (d / "temp1_label").write_text("edge\n")


def test_amd_cpu_reads_beside_an_nvidia_gpu(tmp_path):
    sysfs = tmp_path / "hwmon"
    k10temp(sysfs)
    exe, out = nvidia_smi(tmp_path)
    out.write_text("70\n")
    s = SensorHub(sysfs, nvidia_smi=str(exe), clock=Clock())
    assert "nvidia:0" in s._sensors
    assert s.read("cpu") == 55.0 and s.read("gpu") == 70.0 and s.read("max:cpu,gpu") == 70.0


def test_nvidia_gpu_that_comes_up_after_start_is_found(tmp_path):
    sysfs = tmp_path / "hwmon"
    k10temp(sysfs)
    exe, out = nvidia_smi(tmp_path)
    clock = Clock()
    s = SensorHub(sysfs, nvidia_smi=str(exe), clock=clock)
    assert s.read("gpu") is None  # driver not up yet
    out.write_text("70\n")
    clock.now += RESCAN_EVERY
    assert s.read("gpu") == 70.0 and s.read("cpu") == 55.0


def test_failing_nvidia_smi_keeps_gpu_failing_safe_not_on_the_igpu(tmp_path):
    sysfs = tmp_path / "hwmon"
    k10temp(sysfs)
    amdgpu(sysfs)
    exe, out = nvidia_smi(tmp_path)
    out.write_text("78\n")
    clock = Clock()
    s = SensorHub(sysfs, nvidia_smi=str(exe), clock=clock)
    assert s.read("gpu") == 78.0
    out.unlink()  # e.g. "Driver/library version mismatch" after an upgrade
    clock.now += RESCAN_EVERY
    assert s.read("gpu") is None and "nvidia:0" in s._sensors  # failsafe, not the iGPU's 40
    out.write_text("80\n")
    clock.now += RESCAN_EVERY
    assert s.read("gpu") == 80.0


def test_unparseable_reading_does_not_rescan(tmp_path):
    clock = Clock()
    d = coretemp(tmp_path)
    s = hub(tmp_path, clock)
    (d / "temp1_input").write_text("garbage\n")
    scanned = s._scanned
    clock.now += RESCAN_EVERY
    assert s.read("cpu") is None and s._scanned == scanned
