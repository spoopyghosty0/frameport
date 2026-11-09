"""Meta's Horizon Platform SDK (GitHub #511, TMNT: Empire City 1.0.9): detection and triage."""
from conftest import build_axml

from frameport.apk import axml
from frameport.validate.triage import triage

SUPPLEMENT = "horizonos-supplement-hzplatformclientcore"


def _manifest(*supplements):
    entries = [e for name in supplements for e in (
        ("start", "uses-horizonos-supplement", [("minSupplementVersion", "str", "1"), ("name", "str", name)]),
        ("end", "uses-horizonos-supplement"))]
    return build_axml([("start", "manifest", [("package", "str", "com.CortopiaStudios.draxPrototype")]),
                       *entries, ("start", "application", []), ("end", "application"), ("end", "manifest")])


def test_reads_horizonos_supplements():
    assert axml.horizonos_supplements(_manifest(SUPPLEMENT, SUPPLEMENT)) == [SUPPLEMENT]
    assert axml.horizonos_supplements(_manifest()) == []


def test_triage_horizon_platform_core_missing():
    log = ("10-26 12:50:29.760  1227  1357 E Unity   : Failed to initialize HorizonPlatformCore: "
           "java.lang.ClassNotFoundException: horizonos.supplement.hzplatformclientcore.HorizonPlatformCore\n"
           "10-26 12:50:29.760  1227  1357 E Unity   : Oculus.Platform.<>c__DisplayClass5_0:<InitAsync>b__0() (at "
           "E:/x/Library/PackageCache/com.meta.xr.sdk.platform@77232b66891b/Scripts/AndroidClient.cs:85)\n")
    r = triage(log, "RUNNING")
    assert "horizon-platform-core-missing" in [f.id for f in r.findings]
    assert not any(f.id == "horizon-platform-core-missing" for f in triage(
        "E Unity   : Failed to initialize HorizonPlatformCore: setup failed\n", "RUNNING").findings)
