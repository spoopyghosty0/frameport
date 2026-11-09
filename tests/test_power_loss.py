"""#598: a Frame that restarted after a sudden power loss (the power chip's PON reason) isn't blamed on a game."""
from test_steam_gamepad import agent_module

from frameport.ui.restart import crashed_game, power_loss_game
from frameport.validate import triage

# from a #598 diagnostics zip (frame/this-boot-kernel.txt)
KERNEL = """Nov 07 05:32:11 frame kernel: SPI driver spidev has no spi_device_id for linux,spidev
Nov 07 05:32:11 frame kernel: qcom-pon c400000.spmi:pmic@0:pon@1300: PON_PON_REASON1 0x02
Nov 07 05:32:11 frame kernel: qcom-pon c400000.spmi:pmic@0:pon@1300: PON_WARM_RESET_REASON1 0x00
Nov 07 05:32:11 frame kernel: qcom-pon c400000.spmi:pmic@0:pon@1300: PON_ON_REASON 0x80
Nov 07 05:32:11 frame kernel: qcom-pon c400000.spmi:pmic@0:pon@1300: PON_POFF_REASON1 0x00
Nov 07 05:32:11 frame kernel: qcom-pon c400000.spmi:pmic@0:pon@1300: PON_OFF_REASON1 0x00000048
Nov 07 05:32:11 frame kernel: qcom-pon c400000.spmi:pmic@0:pon@1300: PON_PBS_FAULT_REASON1 0x00000000
Nov 07 05:32:11 frame kernel: qcom-pon c400000.spmi:pmic@0:pon@1300: PON: type: PON reset seq
Nov 07 05:32:11 frame kernel: qcom-pon c400000.spmi:pmic@0:pon@1300: PON: trigger: SPML
Nov 07 05:32:11 frame kernel: qcom_wdt 17411000.watchdog: QCOM WDT probe OK
"""
# kernel 6.18 / SteamOS 0.5.6 (dev Frame, 2026-11-09): no driver prefix, a code per reason
NEW = """Nov 09 10:00:01 frame kernel: PON: trigger [0x80]: KPDPWR_N
Nov 09 10:00:01 frame kernel: PON: warm_reset_reason [0x02]: RESIN_N
Nov 09 10:00:01 frame kernel: PON: poff_reason [0x02]: PS_HOLD
Nov 09 10:00:01 frame kernel: PON: off_reason [0x80]: POFF_SEQ
Nov 09 10:00:01 frame kernel: PON: fault_reason1 [0x00]: none
Nov 09 10:00:01 frame kernel: PON: fault_reason2 [0x00]: none
"""
LAUNCH = {"package": "com.x", "title": "X", "time": 1000.0}


def test_pon_reason_from_the_kernel_log(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    assert agent.pon_reason(KERNEL) == {"kind": "power_loss", "trigger": "SPML", "poff_reason": None,
                                        "fault_reason": None}
    assert agent.pon_reason(NEW) == {"kind": "power_key", "trigger": "KPDPWR_N", "poff_reason": "PS_HOLD",
                                     "fault_reason": None}
    for trigger, kind in (("SMPL", "power_loss"), ("UVLO", "power_loss"), ("KPDPWR_N", "power_key"),
                          ("Hard Reset", "hard_reset"), ("HARD_RESET", "hard_reset"), ("PS_HOLD", "reboot"),
                          ("USB_CHG", "charger"), ("DC_CHG", "charger"), ("RTC", "other")):
        for line in (f"qcom-pon c400000.spmi:pmic@0:pon@1300: PON: trigger: {trigger}\n",
                     f"frame kernel: PON: trigger [0x08]: {trigger}\n"):
            assert agent.pon_reason(line)["kind"] == kind and agent.pon_reason(line)["trigger"] == trigger
    # how the previous boot ended: a power-off or fault reason naming a power loss wins over the trigger
    uvlo = NEW.replace("poff_reason [0x02]: PS_HOLD", "poff_reason [0x20]: UVLO")
    assert agent.pon_reason(uvlo)["kind"] == "power_loss"
    smpl = NEW.replace("fault_reason2 [0x00]: none", "fault_reason2 [0x08]: SMPL")
    assert agent.pon_reason(smpl) == {"kind": "power_loss", "trigger": "KPDPWR_N", "poff_reason": "PS_HOLD",
                                      "fault_reason": "SMPL"}
    # no trigger line: PON_PON_REASON1's bits (bit 1 = SMPL, bit 7 = power key)
    assert agent.pon_reason("qcom-pon x: PON_PON_REASON1 0x02\n")["kind"] == "power_loss"
    assert agent.pon_reason("qcom-pon x: PON_PON_REASON1 0x80\n")["kind"] == "power_key"
    assert agent.pon_reason("kernel: nothing here\n") is None and agent.pon_reason("") is None


def test_boot_cache_reads_an_unknown_reason_again(monkeypatch, tmp_path):
    import json

    agent = agent_module(monkeypatch, tmp_path)
    cache = tmp_path / "boot.json"
    monkeypatch.setattr(agent, "BOOT_STATE", str(cache))
    real_open = open

    def fake_open(path, *args, **kw):
        if path == "/proc/sys/kernel/random/boot_id":
            from io import StringIO
            return StringIO("abc\n")
        return real_open(path, *args, **kw)
    monkeypatch.setattr(agent, "open", fake_open, raising=False)
    # written by the first dev build (no pon_agent), reason unknown: read again, the rest kept
    cache.write_text(json.dumps({"boot_id": "abc", "boot_time": 1, "prev_clean": False, "prev_reason": None,
                                 "last_launch": None}))
    monkeypatch.setattr(agent, "boot_pon_reason", lambda: agent.pon_reason(NEW))
    st = agent.boot_state()
    assert st["prev_reason"] == "power_key" and st["pon_poff_reason"] == "PS_HOLD" and st["prev_clean"] is False
    assert json.loads(cache.read_text())["prev_reason"] == "power_key"
    # known now: never read again this boot
    monkeypatch.setattr(agent, "boot_pon_reason", lambda: 1 / 0)
    assert agent.boot_state()["prev_reason"] == "power_key"
    # still unknown with this agent, long after the boot: kept
    cache.write_text(json.dumps({**st, "prev_reason": None}))
    assert agent.boot_state()["prev_reason"] is None


def boot(**kw):
    return {"boot_id": "b2", "boot_time": 2000, "prev_clean": False, "last_launch": LAUNCH, **kw}


def test_power_loss_names_no_game():
    assert crashed_game(boot(prev_reason="power_loss"), "b1") is None
    assert power_loss_game(boot(prev_reason="power_loss"), "b1") == LAUNCH
    assert crashed_game(boot(prev_reason="power_key"), "b1") == LAUNCH  # a held power key / crash: as before
    assert power_loss_game(boot(prev_reason=None), "b1") is None
    assert power_loss_game(boot(prev_reason="power_loss"), "b2") is None  # same boot: already said


def test_triage_signature_matches_the_kernel_line():
    r = triage.triage("", "UNKNOWN", None, kernel=KERNEL, applied=set())
    hit = [f for f in r.findings if f.id == "frame-power-loss"]
    assert hit and "PON: trigger: SPML" in hit[0].evidence
    for text in (NEW.replace("poff_reason [0x02]: PS_HOLD", "poff_reason [0x20]: UVLO"),
                 NEW.replace("trigger [0x80]: KPDPWR_N", "trigger [0x02]: SMPL")):
        assert "frame-power-loss" in [f.id for f in triage.triage("", "UNKNOWN", None, kernel=text,
                                                                  applied=set()).findings]
    assert "frame-power-loss" not in [f.id for f in triage.triage("", "UNKNOWN", None, kernel=NEW,
                                                                  applied=set()).findings]
    other = KERNEL.replace("trigger: SPML", "trigger: KPDPWR_N")
    assert not [f for f in triage.triage("", "UNKNOWN", None, kernel=other, applied=set()).findings
                if f.id == "frame-power-loss"]


def test_diag_inspect_triages_the_frames_kernel_log(tmp_path):
    import json
    import zipfile

    from frameport.diag import bundle

    path = tmp_path / "d.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps({"games": []}))
        z.writestr("frame/this-boot-kernel.txt", KERNEL)
    out = bundle.read(path)
    assert "frame-power-loss" in [f["id"] for f in out["frame"]["triage"]["findings"]]
