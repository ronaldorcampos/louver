from louver.sim import SimAuraAccess, sim_motherboard


def test_demo_motherboard_is_writable_and_never_real_sysfs():
    mb = sim_motherboard()
    assert mb.present and sorted(mb.headers) == [1, 2, 7] and mb.writable()
    assert not str(mb.dir).startswith("/sys")
    mb.set_percent(2, 50)
    assert mb.headers[2].enable == 1 and mb.headers[2].pwm == round(50 * 2.55)
    mb.release(2)
    assert mb.headers[2].enable == 5


def test_demo_aura_board_has_one_onboard_channel_and_three_argb_headers():
    acc = SimAuraAccess()
    dev = acc.open(acc.find())
    assert acc.node_id(acc.find()) == acc.node_id(acc.find())  # never re-enumerates
    assert dev.read_firmware() == "AULA3-DEMO-0001"
    chans = dev.read_config()
    assert [(c.key, c.num_leds, c.rgb_headers) for c in chans] == [
        ("onboard", 5, 1),
        ("argb-1", 1, 0),
        ("argb-2", 1, 0),
        ("argb-3", 1, 0),
    ]
    dev.set_gen1()
    dev.set_effect(0, 1, (255, 0, 0))  # writes go nowhere
    assert [w[1] for w in dev.t.writes] == [0x82, 0xB0, 0x52, 0x35, 0x36]
