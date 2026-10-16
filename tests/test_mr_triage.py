import pytest

from frameport.validate.triage import triage


@pytest.mark.parametrize("line", ["scene: capture requested -> guardian box",
    "scene: capture requested -> using guardian room", "scene: room from guardian",
    "scene: room 1.50 x 1.50 x 2.50 m (from guardian)",
    "scene: room 1.50 x 1.50 x 2.50 m (configured/default size)"])
def test_fallback_room_is_an_informational_hint(line):
    result = triage("FrameBridge: " + line, "RUNNING", "com.example.mr")
    finding = next(f for f in result.findings if f.id == "mr-no-room")
    assert finding.severity == "info" and "Scan your room" in finding.diagnosis
    assert finding.suggest == []
    assert result.verdict == "pass"


@pytest.mark.parametrize("line", ["overportOXR: Unknown proc addr: xrDiscoverSpacesMETA",
                                   "OVRPlugin: Unknown proc addr: xrDiscoverSpacesMETA", "MRUK: NoRoomsFound"])
def test_discovery_suggests_scene_emulation(line):
    result = triage(line, "RUNNING", "com.example.mr")
    finding = next(f for f in result.findings if f.id == "mr-discovery-unsupported")
    assert finding.suggest == ["adapter.scene_emul"]


def test_loaded_scan_is_not_a_missing_room():
    result = triage("FrameBridge: scene: loaded room.txt (source scan)", "RUNNING")
    assert not any(f.id.startswith("mr-") for f in result.findings)
