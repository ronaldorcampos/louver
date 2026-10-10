from louver.sim import sim_motherboard


def test_demo_motherboard_is_writable_and_never_real_sysfs():
    mb = sim_motherboard()
    assert mb.present and sorted(mb.headers) == [1, 2, 7] and mb.writable()
    assert not str(mb.dir).startswith("/sys")
    mb.set_percent(2, 50)
    assert mb.headers[2].enable == 1 and mb.headers[2].pwm == round(50 * 2.55)
    mb.release(2)
    assert mb.headers[2].enable == 5
