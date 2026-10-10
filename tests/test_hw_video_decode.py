"""frame.hw_video_decode: the shared Iris codec plugin, chosen per game (it used to be tied to Batman's package)."""
import zipfile
from pathlib import Path

from test_patches import _analysis, _apk

from frameport.analysis import detect
from frameport.apk.workspace import ApkWorkspace
from frameport.core import library
from frameport.core.events import Reporter
from frameport.patches import base
from frameport.patches.frame.adapter import needs_xrshim
from frameport.recommend import catalog


def _dex_apk(path, manifest, dex=b"", libs=()):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("classes.dex", b"dex\n035\0" + dex)
        for lib in libs:
            z.writestr(f"lib/arm64-v8a/{lib}", b"\x7fELF")
    return path


def test_detect_finds_android_video_decoding(tmp_path, quest_manifest):
    assert detect.ANALYSIS_VERSION >= 8
    cases = [(b"...Landroid/media/MediaCodec;...", (), True),
             (b"...Landroidx/media3/exoplayer/ExoPlayer;...", (), True),
             (b"...Lcom/google/android/exoplayer2/SimpleExoPlayer;...", (), True),
             (b"", ("libvlc.so",), True),
             (b"...Landroid/media/MediaPlayer;...", (), False)]
    for i, (dex, libs, expected) in enumerate(cases):
        a = detect.analyze(_dex_apk(tmp_path / f"{i}.apk", quest_manifest, dex, libs))
        assert a.extra["media_codec"] is expected, dex
    patch = base.get("frame.hw_video_decode")
    assert patch.detect(_analysis(extra={"media_codec": True})).recommended
    assert patch.detect(_analysis(extra={})) is None
    assert patch.detect(_analysis(extra={"media_codec": True}, abis=["armeabi-v7a"])) is None
    assert not patch.needs_vr  # 2D video apps too


def test_the_patch_changes_no_apk_and_old_codec_assets_go(tmp_path, quest_manifest):
    """The codec is installed once per Frame (agent install_video_codec); the patch only reaches the agent through the
    recipe (deployment.json), so a build never carries codec assets, and older bundled ones are removed."""
    patch = base.get("frame.hw_video_decode")
    assert patch.stage == "install"
    apk = _apk(tmp_path, quest_manifest)
    with zipfile.ZipFile(apk, "a") as z:
        z.writestr("assets/frameport/hevc/libstagefrighthw.so", b"old codec")
        z.writestr("assets/video/intro.mp4", b"game video")
    with ApkWorkspace(apk) as ws:
        ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), {"frame.adapter": {}, "frame.hw_video_decode": {}})
        assert base.get("frame.adapter").apply(ctx)
        assert not any(n.startswith("assets/frameport/hevc/") for n in ws.names())
        assert ws.has("assets/video/intro.mp4")


def test_the_recipe_carries_it_to_the_agent():
    """installer.install sends the whole patch selection (install-stage patches too); the agent's finalize decides
    the launcher line from it (tests/test_shared_video_codec.py)."""
    source = (Path(__file__).resolve().parents[1] / "src/frameport/install/installer.py").read_text()
    assert 'recipe={"patches": sorted(plan.recipe.patches)' in source
    agent = (Path(__file__).resolve().parents[1] / "agent/frameport_agent.py").read_text()
    assert 'HW_VIDEO_PATCH = "frame.hw_video_decode"' in agent


def test_surface_native_is_a_hidden_recipe_setting_that_needs_the_shim():
    from frameport.patches.settings import HIDDEN, UI, adapter_settings

    patch = base.get("adapter.surface_native")
    assert patch.default == 0 and "surface_native" in HIDDEN and "surface_native" not in UI
    assert "surface_native" not in adapter_settings({})
    assert adapter_settings({"adapter.surface_native": {"value": 1}})["surface_native"] == 1
    assert needs_xrshim({"adapter.surface_native": {"value": 1}}) and not needs_xrshim({})
    assert patch.detect(_analysis()) is None


def test_migration_gives_existing_batman_recipes_the_video_patches():
    user = {"source": "user", "patches": {"frame.adapter": {}, "adapter.scale": {"value": 0.8}}, "reasons": {}}
    other = {"source": "user", "patches": {"frame.adapter": {}}, "reasons": {}}
    data = {"settings": {"migrations": []}, "games": {"com.camouflaj.manta": {"recipe": user},
                                                      "com.example.other": {"recipe": other}}}
    assert library._migrate(data)
    assert user["patches"]["frame.hw_video_decode"] == {}
    assert user["patches"]["adapter.surface_native"] == {"value": 1}
    assert user["patches"]["adapter.scale"] == {"value": 0.8}  # the user's own choices stay
    assert not {"frame.hw_video_decode", "adapter.surface_native"} & set(other["patches"])
    assert "batman_video_patches" in data["settings"]["migrations"]
    user["patches"].pop("frame.hw_video_decode")
    library._migrate(data)  # runs once
    assert "frame.hw_video_decode" not in user["patches"]


def test_batman_catalog_recipe_has_the_video_patches():
    entry = catalog.load()["com.camouflaj.manta"]
    assert "frame.hw_video_decode" in entry.frame and entry.adapter.get("surface_native") == 1
