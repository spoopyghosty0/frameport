"""Oculus Rift (PC VR) support: PE detection, scanning, recipes, Revive unpacking, triage."""
import json
import struct
import zlib
from pathlib import Path

import pytest

from frameport.analysis import rift
from frameport.core import library
from frameport.recommend import engine
from frameport.sources import rift_dump


def make_pe(machine=0x8664, imports=(), extra=b"", subsystem=2, delay=()) -> bytes:
    """A minimal PE (one section holding the import directory + name strings) — enough for rift.read_pe."""
    pe32plus = machine == 0x8664
    opt_size = 240 if pe32plus else 224
    sect_rva, sect_raw = 0x1000, 0x400
    body = bytearray()
    desc_size = 20 * (len(imports) + 1)
    names = bytearray()
    descs = bytearray()
    for name in imports:
        descs += struct.pack("<IIIII", 0, 0, 0, sect_rva + desc_size + len(names), 0)
        names += name.encode() + b"\0"
    descs += b"\0" * 20
    delay_off = desc_size + len(names) + sum(len(n) + 1 for n in delay)
    delay_descs = bytearray()
    for name in delay:  # ImgDelayDescr: attributes, then the DLL name RVA
        delay_descs += struct.pack("<II", 1, sect_rva + desc_size + len(names)) + b"\0" * 24
        names += name.encode() + b"\0"
    if delay:
        delay_descs += b"\0" * 32
    body += descs + names + delay_descs + extra
    hdr = bytearray(b"MZ" + b"\0" * 0x3A + struct.pack("<I", 0x80))
    hdr += b"\0" * (0x80 - len(hdr))
    hdr += b"PE\0\0" + struct.pack("<HHIIIHH", machine, 1, 0, 0, 0, opt_size, 0x22)
    opt = bytearray(opt_size)
    struct.pack_into("<H", opt, 0, 0x20B if pe32plus else 0x10B)
    struct.pack_into("<H", opt, 68, subsystem)  # 2 = Windows GUI (games), 3 = console
    dd = 112 if pe32plus else 96
    struct.pack_into("<II", opt, dd + 8, sect_rva if imports else 0, desc_size)
    if delay:
        struct.pack_into("<II", opt, dd + 13 * 8, sect_rva + delay_off, len(delay_descs))
    hdr += opt
    hdr += b".idata\0\0" + struct.pack("<IIII", len(body), sect_rva, len(body), sect_raw) + b"\0" * 16
    hdr += b"\0" * (sect_raw - len(hdr))
    return bytes(hdr + body)


def unity_game(root: Path, name="Space Game", imports=("d3d11.dll", "kernel32.dll"), platform=False, extra=b"") -> Path:
    g = root / name
    (g / f"{name}_Data" / "Plugins").mkdir(parents=True)
    (g / f"{name}.exe").write_bytes(make_pe(imports=("kernel32.dll",)))
    (g / "UnityPlayer.dll").write_bytes(make_pe(imports=imports, extra=extra))
    (g / "UnityCrashHandler64.exe").write_bytes(make_pe())
    (g / f"{name}_Data" / "Plugins" / "OVRPlugin.dll").write_bytes(make_pe(extra=b"ovr_Initialize"))
    if platform:
        (g / f"{name}_Data" / "Plugins" / "LibOVRPlatform64_1.dll").write_bytes(make_pe())
    return g


def test_read_pe_machine_and_imports(tmp_path):
    p = tmp_path / "a.exe"
    p.write_bytes(make_pe(0x14C, ("d3d11.dll", "LibOVRPlatform32_1.dll")))
    info, _ = rift.read_pe(p)
    assert info.machine == "x86"
    assert info.imports == ["d3d11.dll", "libovrplatform32_1.dll"]


def test_unity_rift_game(tmp_path):
    g = unity_game(tmp_path)
    a = rift.analyze(g)
    assert a.package == "rift.space_game"
    assert (a.engine, a.xr, a.graphics, a.abis) == ("Unity", "LibOVR", "D3D11", ["x86_64"])
    assert a.extra["exe"] == "Space Game.exe" and a.extra["kind"] == "rift"
    assert not a.extra["platform_sdk"] and a.extra["needs_revive"]


def test_platform_sdk_and_openxr_native(tmp_path):
    a = rift.analyze(unity_game(tmp_path, "Owned", platform=True))
    assert a.extra["platform_sdk"]
    g = tmp_path / "XR"
    g.mkdir()
    (g / "XR.exe").write_bytes(make_pe(imports=("openxr_loader.dll", "d3d12.dll")))
    b = rift.analyze(g)
    assert b.xr == "OpenXR" and b.graphics == "D3D12" and b.extra["openxr_native"]


def test_unreal_layout_picks_shipping_exe(tmp_path):
    g = tmp_path / "Climb Game"
    (g / "Climb" / "Binaries" / "Win64").mkdir(parents=True)
    (g / "Engine").mkdir()
    (g / "Climb.exe").write_bytes(make_pe())
    (g / "Climb" / "Binaries" / "Win64" / "Climb-Win64-Shipping.exe").write_bytes(
        make_pe(imports=("d3d11.dll",), extra=b"LibOVRRT%hs_%d.dll" + b"\0" * 4096))
    a = rift.analyze(g)
    assert a.engine == "Unreal" and a.xr == "LibOVR"
    assert a.extra["exe"] == "Climb/Binaries/Win64/Climb-Win64-Shipping.exe"
    r = engine.suggest(a)  # Oculus/LibOVR Unreal: Revive on (PC OpenVR backend), files unchanged, crash reporter off
    assert r.as_is and set(r.patches) == {"pcvr.revive", "pcvr.revive_openvr", "pcvr.libovr_redirect",
                                          "pcvr.steamvr_tuning", "pcvr.no_crash_reporter", "pcvr.oculus_unreal",
                                          "pcvr.xr_timefix"}


def test_scan_finds_rift_games_not_parents_or_quest(tmp_path):
    unity_game(tmp_path / "dumps")
    q = tmp_path / "dumps" / "Quest Game"
    q.mkdir()
    (q / "game.apk").write_bytes(b"PK")
    (q / "setup.exe").write_bytes(make_pe())
    found = rift_dump.scan(tmp_path / "dumps")
    assert [p.name for p in found] == ["Space Game"]
    assert [p.name for p in rift_dump.scan(tmp_path)] == ["Space Game"]  # dumps/Space Game: 2 levels down
    assert rift_dump.scan(tmp_path, depth=1) == []


def test_recipe_only_pcvr_patches(tmp_path):
    a = rift.analyze(unity_game(tmp_path))
    r = engine.suggest(a)
    assert r.as_is and "pcvr.revive" in r.patches  # Oculus game: Revive gives it VR
    assert all(pid.startswith("pcvr.") for pid in r.patches), r.patches
    shown, hidden = engine.visible_patches(a, r)
    assert shown and all(p.category == "pcvr" for p in shown + hidden)
    assert engine.warnings(r) == []


def test_quest_games_never_see_pcvr_patches(quest_manifest, tmp_path):
    import zipfile

    from frameport.analysis.detect import analyze

    apk = tmp_path / "q.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", quest_manifest)
        z.writestr("lib/arm64-v8a/libunity.so", b"\x7fELF")
    a = analyze(apk, deep=False)
    r = engine.suggest(a)
    shown, hidden = engine.visible_patches(a, r)
    assert not any(p.category == "pcvr" for p in shown + hidden)
    assert not any(pid.startswith("pcvr.") for pid in r.patches)


def test_platform_sdk_note(tmp_path):
    r = engine.suggest(rift.analyze(unity_game(tmp_path, "Owned", platform=True)))
    assert "Oculus app" in r.notes


def test_pipeline_add_path_and_prepare(tmp_path, monkeypatch):
    from frameport import pipeline
    from frameport.core.events import Reporter

    unity_game(tmp_path / "dumps")
    added = pipeline.add_path(tmp_path / "dumps")
    assert [g["package"] for g in added] == ["rift.space_game"]
    g = library.game("rift.space_game")
    assert g["kind"] == "rift" and g["exe"] == "Space Game.exe"
    monkeypatch.setenv("FRAMEPORT_REVIVE_DIR", str(fake_revive(tmp_path / "revive")))
    monkeypatch.setattr("frameport.artwork.fetch.cache.http_get", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    info = pipeline.build_game("rift.space_game", Reporter())
    assert info["ok"] and info["sha256"] and info["kind"] == "rift"


def test_launch_env():
    from frameport.core.models import Recipe
    from frameport.patches.pcvr import launch_env

    r = Recipe("rift.x", {"pcvr.proton_log": {}, "pcvr.proton_env": {"env": "DXVK_HUD=fps\n\nA = b"}})
    assert launch_env(r) == {"PROTON_LOG": "1", "DXVK_HUD": "fps", "A": "b"}


# ------------------------------------------------------------------------------------------ Revive / NSIS
def fake_revive(d: Path) -> Path:
    from frameport.tools import revive

    d.mkdir(parents=True, exist_ok=True)
    for n in revive.RUNTIME_FILES:
        (d / n).write_bytes(b"x")
    return d


def make_nsis(files: dict[str, bytes], outdir_var=21) -> bytes:
    """A tiny Unicode NSIS 3 installer: SetOutPath $INSTDIR[\\sub] + File entries, non-solid deflate."""
    strings = bytearray(b"\0\0")

    def add(s, var=None):
        off = len(strings) // 2
        if var is not None:
            strings.extend(struct.pack("<HH", 3, (var & 0x7F) | ((var & 0x3F80) << 1) | 0x8080))
        strings.extend(s.encode("utf-16-le") + b"\0\0")
        return off

    data = bytearray()
    entries = []

    def block(raw):
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        c = comp.compress(raw) + comp.flush()
        off = len(data)
        data.extend(struct.pack("<I", len(c) | 0x80000000) + c)
        return off

    dirs = {}
    for path, content in files.items():
        folder, _, name = path.rpartition("/")
        if folder not in dirs:
            dirs[folder] = add("\\" + folder.replace("/", "\\") if folder else "", outdir_var)
        entries.append((11, dirs[folder], 0, 0, 0, 0, 0))
        entries.append((20, 0, add(name), block(content), 0, 0, 0))
    entries.append((11, add("", 26), 0, 0, 0, 0, 0))  # $PLUGINSDIR: ignored
    entries.append((20, 0, add("plugin.dll"), block(b"p"), 0, 0, 0))
    ent = b"".join(struct.pack("<7I", *e) for e in entries)
    head_len = 4 + 8 * 8
    ent_off, str_off = head_len, head_len + len(ent)
    lang_off = str_off + len(strings)
    header = struct.pack("<I", 0) + struct.pack("<16I", 0, 0, 0, 0, ent_off, len(entries), str_off, 0, lang_off, 0,
                                                 lang_off, 0, 0, 0, 0, 0) + ent + bytes(strings)
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    ch = comp.compress(header) + comp.flush()
    body = struct.pack("<I", len(ch) | 0x80000000) + ch + data
    first = struct.pack("<I", 0) + b"\xef\xbe\xad\xdeNullsoftInst" + struct.pack("<II", len(header), len(body) + 28)
    return b"MZ" + b"\0" * 510 + first + body


def test_nsis_extract(tmp_path):
    from frameport.tools import revive

    files = {n: n.encode() for n in revive.RUNTIME_FILES}
    files["Input/oculus_touch_default.json"] = b"{}"
    files["Qt5Core.dll"] = b"qt"
    inst = tmp_path / "ReviveInstaller.exe"
    inst.write_bytes(make_nsis(files))
    written = revive.extract(inst, tmp_path / "out")
    assert set(written) == set(revive.RUNTIME_FILES) | {"Input/oculus_touch_default.json"}
    assert (tmp_path / "out" / "ReviveInjector.exe").read_bytes() == b"ReviveInjector.exe"
    assert not (tmp_path / "out" / "plugin.dll").exists()
    assert [p.name for p in revive.runtime_files(tmp_path / "out")][:1] == ["oculus_touch_default.json"]


def test_nsis_missing_runtime_files(tmp_path):
    from frameport.tools import revive

    inst = tmp_path / "i.exe"
    inst.write_bytes(make_nsis({"LICENSE": b"l"}))
    with pytest.raises(revive.NsisError):
        revive.extract(inst, tmp_path / "out")


@pytest.mark.skipif(not Path(__file__).with_name("fixtures").joinpath("ReviveInstaller.exe").exists(),
                    reason="real installer not present")
def test_real_revive_installer(tmp_path):  # pragma: no cover - optional
    from frameport.tools import revive

    written = revive.extract(Path(__file__).with_name("fixtures") / "ReviveInstaller.exe", tmp_path)
    assert "LibReviveXR64.dll" in written


# ------------------------------------------------------------------------------------------ triage
def test_pcvr_triage_uses_pcvr_signatures_only():
    from frameport.validate.triage import triage

    log = ("FramePort: launching rift.x with proton_11-arm64\nLaunched injector with: x\nSuccesfully injected!\n"
           "ovrPlatformInitialize_NotEntitled\n")
    r = triage(log, "EXITED", "rift.x")
    assert r.milestone == "Revive injected into the game"
    assert [f.id for f in r.findings] == ["oculus-entitlement"]
    q = triage("Failed to create process\nFrameBridge: scale=1", "RUNNING", "com.x.y")
    assert "revive-inject-failed" not in [f.id for f in q.findings]


def test_system_revive_preferred(tmp_path, monkeypatch):
    """A Revive the user installed (official installer) wins over FramePort's copy and is never replaced."""
    from frameport.core import winhost
    from frameport.tools import revive, toolchain

    managed = fake_revive(tmp_path / "managed")
    revive._state_file().write_text(json.dumps({"version": "3.2.0", "path": managed.as_posix()}))
    system = fake_revive(tmp_path / "Program Files" / "Revive")
    monkeypatch.delenv("FRAMEPORT_REVIVE_DIR", raising=False)
    monkeypatch.setattr(winhost, "available", lambda: True)
    monkeypatch.setattr(winhost, "is_windows", lambda: False)
    monkeypatch.setattr(winhost, "to_local", lambda p: system if "Program Files" in p else tmp_path / "nope")
    monkeypatch.setattr(winhost, "reg_query", lambda key, value: None)
    monkeypatch.setattr(revive, "_system_cache", None)
    monkeypatch.setattr(revive, "release_for", lambda p: "3.2.0")
    assert revive.source() == "system" and revive.revive_dir() == system
    assert revive.install() == system  # no download when the user has Revive
    assert revive.installed_version() == "3.2.0"
    assert toolchain.revive_is_users()
    # not in the default folder: found through the registry (HKLM\Software\Revive default value)
    other = fake_revive(tmp_path / "D" / "Tools" / "Revive")
    monkeypatch.setattr(winhost, "to_local", lambda p: other if p.startswith("D:") else tmp_path / "nope")
    monkeypatch.setattr(winhost, "reg_query",
                        lambda key, value: "D:\\Tools\\Revive" if key.startswith("HKLM") else None)
    monkeypatch.setattr(revive, "_system_cache", None)
    assert revive.revive_dir() == other
    # nothing installed on the system: FramePort's copy
    monkeypatch.setattr(winhost, "reg_query", lambda key, value: None)
    monkeypatch.setattr(revive, "_system_cache", None)
    assert revive.source() == "managed" and revive.revive_dir() == managed


def test_release_for_matches_build_date(tmp_path, monkeypatch):
    import os

    from frameport.tools import revive

    d = fake_revive(tmp_path / "rv")
    os.utime(d / "LibReviveXR64.dll", (1679184000, 1679184000))  # 2023-03-19
    rels = [{"tag_name": "3.2.0", "published_at": "2023-03-20T01:56:30Z"},
            {"tag_name": "3.1.2", "published_at": "2022-10-01T00:00:00Z"},
            {"tag_name": "3.3.0", "published_at": "2024-01-01T00:00:00Z"}]
    monkeypatch.setattr(revive.cache, "cached_json", lambda *a, **k: rels)
    assert revive.release_for(d) == "3.2.0"


# ------------------------------------------------------------------------------------------ nested collections
def collection_tree(root: Path) -> Path:
    """A collection folder: one folder per game, the game nested inside, installers around it."""
    col = root / "PCVR"
    # Unity game one level down, with an uninstaller and a top-level Setup.exe next to archive parts
    a = col / "Space Game v1.6.0 -GRP"
    (a / "Space Game" / "Space Game_Data").mkdir(parents=True)
    (a / "Setup.exe").write_bytes(make_pe())
    (a / "Space Game v1.6.0 -GRP.7z.001").write_bytes(b"7z")
    (a / "Space Game" / "Space Game.exe").write_bytes(make_pe(extra=b"ovr_Initialize"))
    (a / "Space Game" / "UnityPlayer.dll").write_bytes(make_pe(imports=("d3d11.dll",)))
    (a / "Space Game" / "_UnInstall").mkdir()
    (a / "Space Game" / "_UnInstall" / "unins000.exe").write_bytes(make_pe())
    # Unreal: bootstrap exe + the shipping build that really runs (UTF-16 marker, as Unreal stores strings)
    b = col / "Wrath Game v008 [XYZ Repacks]" / "Wrath" / "WindowsNoEditor"
    (b / "WrathGame" / "Binaries" / "Win64").mkdir(parents=True)
    (b / "WrathGame.exe").write_bytes(make_pe())
    (b / "WrathGame" / "Binaries" / "Win64" / "WrathGame-Win64-Shipping.exe").write_bytes(
        make_pe(imports=("d3d11.dll",), extra="LibOVRRT%hs_%d.dll".encode("utf-16-le")))
    (b.parent / "Engine" / "Binaries" / "Win64").mkdir(parents=True)
    (b.parent / "Engine" / "Binaries" / "Win64" / "CrashReportClient.exe").write_bytes(make_pe())
    # two builds of the same game: ambiguous
    c = col / "The Climber v1.5.0.16 -GRP" / "The Climber" / "bin"
    for build in ("win_x64", "win_x64-steam"):
        (c / build).mkdir(parents=True)
        (c / build / "Climber.exe").write_bytes(make_pe(extra=b"LibOVRRT64_1.dll"))
    # not games: a console tool and a Quest dump
    tool = col / "Some Tool"
    tool.mkdir()
    (tool / "tool.exe").write_bytes(make_pe(subsystem=3))
    q = col / "Quest Game -RLS"
    q.mkdir()
    (q / "game.apk").write_bytes(b"PK")
    return col


def test_scan_collection_finds_nested_games(tmp_path):
    col = collection_tree(tmp_path)
    games = rift_dump.scan(col)
    assert sorted(p.name for p in games) == ["Space Game v1.6.0 -GRP", "The Climber v1.5.0.16 -GRP",
                                             "Wrath Game v008 [XYZ Repacks]"]
    assert [p.name for p in rift_dump.scan(tmp_path)] == [p.name for p in games]  # a level above works too


def test_ranking_and_ambiguity(tmp_path):
    col = collection_tree(tmp_path)
    a = rift.analyze(col / "Space Game v1.6.0 -GRP")
    assert a.extra["exe"] == "Space Game/Space Game.exe" and a.extra["exe_confirmed"] and a.label == "Space Game"
    w = rift.analyze(col / "Wrath Game v008 [XYZ Repacks]")
    assert w.extra["exe"].endswith("WrathGame-Win64-Shipping.exe") and w.xr == "LibOVR" and w.engine == "Unreal"
    assert "Unreal launcher (starts the real game build)" in w.extra["exe_candidates"][1]["reasons"]
    c = rift.analyze(col / "The Climber v1.5.0.16 -GRP")
    assert c.extra["exe_confirmed"] is False and c.extra["exe"] == "The Climber/bin/win_x64/Climber.exe"
    assert "Steam build" in c.extra["exe_candidates"][1]["reasons"]
    assert c.label == "The Climber" and c.package == "rift.the_climber"


@pytest.mark.parametrize("name,title", [
    ("Asgards Wrath v1.6.0 -GRP", "Asgards Wrath"),
    ("Arktika 1 (v1.0.0.7) -RLS", "Arktika 1"),
    ("Vader Immortal - Episode II v2.0.2+236948 Shipping", "Vader Immortal - Episode II"),
    ("Vader Immortal - Episode I v1.1.0+236956 Shipping -[ABC Repacks]", "Vader Immortal - Episode I"),
    ("Vader Immortal - Episode III v3.0.2+236944 [XYZ Repacks]", "Vader Immortal - Episode III"),
    ("Lies Beneath 4.15.20 -GRP", "Lies Beneath"),
    ("Defector v21.11.08.358012 -GRP", "Defector"),
    ("Lone Echo 2", "Lone Echo 2"),
    ("Rick and Morty - Virtual Rick-ality v2288226 -GRP", "Rick and Morty - Virtual Rick-ality"),
    ("Rick and Morty - Virtual Rick-ality", "Rick and Morty - Virtual Rick-ality"),  # no tag: nothing cut
    ("Half-Life Alyx -GRP v12", "Half-Life Alyx"),
    ("Some Game (Oculus Release) -GRP", "Some Game"),
])
def test_clean_title(name, title):
    assert rift.clean_title(name) == title


def test_set_exe_and_rescan_keep_choice(tmp_path):
    from frameport import pipeline

    col = collection_tree(tmp_path)
    added = {g["title"]: g for g in pipeline.add_path(col)}
    assert set(added) == {"Space Game", "Wrath Game", "The Climber"}  # the fake Quest dump isn't a real APK
    climber = added["The Climber"]
    assert climber["exe_confirmed"] is False
    pipeline.set_exe(climber["package"], "The Climber/bin/win_x64-steam/Climber.exe")
    g = library.game(climber["package"])
    assert g["exe_confirmed"] is True and g["exe"].endswith("win_x64-steam/Climber.exe")
    library.upsert_game(g["package"], tags=["Mine"])
    pipeline.add_path(col)  # rescan: the choice and the tags stay
    g = library.game(climber["package"])
    assert g["exe"].endswith("win_x64-steam/Climber.exe") and g["tags"] == ["Mine"]


def test_unchanged_folder_not_reanalyzed(tmp_path, monkeypatch):
    from frameport import pipeline

    col = collection_tree(tmp_path)
    pipeline.add_path(col)
    calls = []
    real = rift.analyze
    monkeypatch.setattr(rift, "analyze", lambda *a, **k: calls.append(a) or real(*a, **k))
    pipeline.add_path(col)
    assert [c for c in calls if "Space Game" in str(c[0])] == []  # skipped via the fingerprint


def test_bundled_revive_dll_still_uses_framework_revive(tmp_path):
    """Repacks carry a LibRevive64.dll for their own launcher; it isn't loaded by itself, so Revive stays on."""
    g = unity_game(tmp_path, "Repacked Game")
    (g / "LibRevive64.dll").write_bytes(make_pe())
    a = rift.analyze(g)
    assert a.extra["revive_bundled"]
    r = engine.suggest(a)
    assert r.as_is and "pcvr.revive" in r.patches  # don't reuse the repack's DLL; Revive provides the runtime


def test_quest_as_is_skips_patching(quest_manifest, tmp_path, monkeypatch):
    import zipfile

    from frameport import pipeline
    from frameport.core.events import Reporter

    apk = tmp_path / "patched.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", quest_manifest)
        z.writestr("lib/arm64-v8a/libframe_settings.so", b"\x7fELF")
    monkeypatch.setattr("frameport.artwork.fetch.cache.http_get", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    from frameport.analysis.detect import analyze

    a = analyze(apk, deep=False)
    assert a.extra["frame_patched"] and engine.suggest(a).as_is
    library.upsert_game(a.package, apk=str(apk), analysis=a.to_dict(),
                        recipe=library.recipe_to_dict(engine.suggest(a)))
    called = []
    monkeypatch.setattr(pipeline.builder, "build", lambda *x, **k: called.append(1))
    info = pipeline.build_game(a.package, Reporter())
    assert info["as_is"] and info["apk"] == str(apk) and not called  # the original APK, overport never runs


def test_same_game_scanned_from_wrapper_or_inner_folder(tmp_path):
    from frameport import pipeline

    col = collection_tree(tmp_path)
    pipeline.add_path(col)
    pkg = "rift.space_game"
    library.upsert_game(pkg, title="Space Game: Remastered", art_source="oculusdb")
    pipeline.add_path(col / "Space Game v1.6.0 -GRP")  # scanning the game's own folder finds the inner one
    games = [g for g in library.games() if g["package"].startswith("rift.space")]
    assert len(games) == 1 and games[0]["title"] == "Space Game: Remastered"


def test_upload_files_big_small_and_cancel(tmp_path):
    from frameport.core.events import Cancelled, Reporter
    from frameport.install import installer

    class FakeFrame:
        def __init__(self):
            self.puts, self.tars, self.dirs = [], [], []

        def mkdirs(self, dirs):
            self.dirs += list(dirs)

        def put(self, local, remote, progress=None, resume=True, mkdir=True):
            progress(Path(local).stat().st_size // 2, Path(local).stat().st_size)
            self.puts.append(remote)

        def put_tar(self, files, root, progress=None):
            for _local, rel in files:
                progress(0, rel)
            self.tars.append([rel for _, rel in files])
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (installer.BIG_FILE + 1))
    smalls = []
    for i in range(3):
        p = tmp_path / f"s{i}.txt"
        p.write_bytes(b"y" * 10)
        smalls.append(p)
    items = [(big, "game/big.bin", big.stat().st_size)] + [(p, f"game/sub/{p.name}", 10) for p in smalls]
    f = FakeFrame()
    installer.upload_files(f, items, "/r/incoming", Reporter(), 10 ** 9)
    assert f.puts == ["/r/incoming/game/big.bin"] and f.tars == [[f"game/sub/s{i}.txt" for i in range(3)]]
    assert f.dirs == ["/r/incoming/game"]
    rep = Reporter()
    rep.cancelled.set()
    with pytest.raises(Cancelled):
        installer.upload_files(FakeFrame(), items, "/r/incoming", rep, 10 ** 9)


def test_platform_sdk_delay_loaded(tmp_path):
    """Oculus Store Unreal builds delay-load the Platform SDK (the exe or a modular build's OnlineSubsystemOculus
    plugin): it has to count as the Platform SDK (the game crashes with 0xc06d007e without the Oculus app)."""
    exe = tmp_path / "a.exe"
    exe.write_bytes(make_pe(imports=("kernel32.dll",), delay=("LibOVRPlatform64_1.dll", "OVRPlugin.dll")))
    info, _ = rift.read_pe(exe)
    assert info.imports == ["kernel32.dll"] and info.delay_imports == ["libovrplatform64_1.dll", "ovrplugin.dll"]
    g = tmp_path / "Game"
    (g / "Game/Binaries/Win64").mkdir(parents=True)
    (g / "Game/Binaries/Win64/Game-Win64-Shipping.exe").write_bytes(make_pe(imports=("kernel32.dll",)))
    plug = g / "Engine/Plugins/Online/OnlineSubsystemOculus/Binaries/Win64"
    plug.mkdir(parents=True)
    (plug / "Game-OnlineSubsystemOculus-Win64-Shipping.dll").write_bytes(make_pe(delay=("LibOVRPlatform64_1.dll",)))
    (g / "Engine/Plugins/Runtime/OculusVR/Binaries/Win64").mkdir(parents=True)
    (g / "Engine/Plugins/Runtime/OculusVR/Binaries/Win64/Game-OculusHMD-Win64-Shipping.dll").write_bytes(make_pe())
    assert rift.analyze(g).extra["platform_sdk"]


def test_openvr_native_routing(tmp_path):
    """A game that links OpenVR (openvr_api) and has no Oculus/LibOVR code is Frame-native (no Revive). A game that
    also has LibOVR is treated as Oculus (needs Revive) — static analysis can't tell which runtime a dual build
    picks."""
    g = tmp_path / "SteamVR Game"
    (g / f"{g.name}_Data").mkdir(parents=True)
    (g / "SteamVR Game.exe").write_bytes(make_pe(imports=("openvr_api64.dll", "kernel32.dll")))
    (g / "openvr_api64.dll").write_bytes(make_pe())
    a = rift.analyze(g)
    assert a.extra["openvr_native"] and a.extra["frame_native"] and not a.extra["needs_revive"]
    r = engine.suggest(a)
    assert "pcvr.revive" not in r.patches and "OpenVR" in a.xr
    # add LibOVR markers -> a dual-API Unity build: runs natively, SteamVR selected by argument
    (g / "OVRPlugin.dll").write_bytes(make_pe(extra=b"LibOVRRT%hs_%d.dll"))
    a2 = rift.analyze(g)
    assert a2.extra["launch"] == "native" and a2.extra["launch_args"] == "-vrmode OpenVR"
    r2 = engine.suggest(a2)
    assert "pcvr.revive" not in r2.patches and r2.patches["pcvr.launch_args"] == {"args": "-vrmode OpenVR"}
    # without openvr_api: Oculus only -> FramePort's Revive
    (g / "openvr_api64.dll").unlink()
    (g / "SteamVR Game.exe").write_bytes(make_pe(imports=("kernel32.dll",)))
    a3 = rift.analyze(g)
    assert a3.extra["launch"] == "revive" and a3.extra["needs_revive"] and not a3.extra["frame_native"]
    assert "pcvr.revive" in engine.suggest(a3).patches


def test_repack_launcher_mode(tmp_path):
    """A repack set up for SteamVR (bundled LibRevive64.dll + xinput loader next to the exe) runs directly: no second
    Revive (that broke e.g. the Oculus Platform entitlement check), Wine loads the loader DLLs on the Frame."""
    from frameport.core.models import Recipe
    from frameport.patches import pcvr

    g = tmp_path / "Wilsons Heart v1 -X"
    (g / "Wilsons Heart").mkdir(parents=True)
    (g / "Wilsons Heart/WHVR.exe").write_bytes(make_pe(imports=("libovrplatform64_1.dll", "kernel32.dll"),
                                                      extra=b"LibOVRRT%hs_%d.dll"))
    for n in ("LibRevive64.dll", "openvr_api64.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        (g / "Wilsons Heart" / n).write_bytes(make_pe())
    a = rift.analyze(g)
    assert a.extra["launch"] == "repack" and a.extra["loader_dlls"] == ["xinput1_3.dll", "xinput9_1_0.dll"]
    r = engine.suggest(a)
    assert "pcvr.revive" not in r.patches and "pcvr.revive_openvr" not in r.patches
    assert r.patches["pcvr.repack_launcher"] == {"dlls": "xinput1_3.dll,xinput9_1_0.dll"}
    assert "pcvr.libovr_redirect" in r.patches  # the Frame points LibOVRRT at the bundled LibRevive
    assert pcvr.launch_env(r)["WINEDLLOVERRIDES"] == "xinput1_3,xinput9_1_0=n,b"
    assert engine.warnings(r) == []
    r.patches["pcvr.revive"] = {}
    assert any("conflicts" in w for w in engine.warnings(r))
    # the user's extra overrides extend the loader's
    r2 = Recipe("x", patches={"pcvr.repack_launcher": {"dlls": "xinput1_3.dll"},
                              "pcvr.proton_env": {"env": "WINEDLLOVERRIDES=d3d11=n\nDXVK_HUD=fps"}})
    assert pcvr.launch_env(r2) == {"WINEDLLOVERRIDES": "xinput1_3=n,b;d3d11=n", "DXVK_HUD": "fps"}


def test_unreal_dual_api_uses_vd_bat_args(tmp_path):
    """Unreal builds shipping the OpenXR plugin run natively; the repack's VD.bat arguments are reused with the
    Oculus HMD module swapped for OpenXR (no desktop shortcuts needed)."""
    from frameport.patches import pcvr

    g = tmp_path / "Behemoth"
    exe_dir = g / "BHM/Binaries/Win64"
    exe_dir.mkdir(parents=True)
    (exe_dir / "BHM-Win64-Shipping.exe").write_bytes(make_pe(extra=b"OculusHMD"))
    (g / "Engine/Binaries/ThirdParty/OpenXR/win64").mkdir(parents=True)
    (g / "Engine/Binaries/ThirdParty/OpenXR/win64/openxr_loader.dll").write_bytes(make_pe())
    (exe_dir / "VD.bat").write_text('"C:\\Program Files\\VD\\VirtualDesktop.Streamer.exe" "BHM-Win64-Shipping.exe" '
                                    "-steam -hmd=OculusXRHMD\r\n")
    a = rift.analyze(g)
    assert a.extra["launch"] == "native" and a.extra["launch_args"] == "-steam -hmd=OpenXR"
    r = engine.suggest(a)
    assert "pcvr.revive" not in r.patches and "pcvr.oculus_unreal" not in r.patches
    assert pcvr.game_args(r) == ["-steam", "-hmd=OpenXR", "-nocrashreports"]
    (exe_dir / "VD.bat").unlink()
    assert rift.analyze(g).extra["launch_args"] == "-hmd=OpenXR"  # engine default without VD.bat


@pytest.mark.parametrize("folder,name", [
    ("Some Game v3055+2.5.0 -RLS v76", "Some Game v3055+2.5.0"),
    ("Some Game v974+2.2 -RLS R2", "Some Game v974+2.2"),
    ("Some Game - Episode One v12+1.0", "Some Game - Episode One v12+1.0"),  # a subtitle isn't a release tag
])
def test_quest_folder_display_name(folder, name):
    from frameport.sources.quest_dump import display_name

    assert display_name(folder) == name


def _dual_unity(tmp_path):
    """SUPERHOT VR's older build (GitHub #105): Unity built-in VR with the Oculus and the OpenVR SDK + an Electron
    launcher (SHVR.exe) next to it."""
    g = tmp_path / "SUPERHOT VR"
    (g / "SUPERHOTVR_Data/Plugins").mkdir(parents=True)
    (g / "SUPERHOTVR.exe").write_bytes(make_pe(imports=("unityplayer.dll", "kernel32.dll")))
    (g / "UnityPlayer.dll").write_bytes(make_pe())
    (g / "SUPERHOTVR_Data/Plugins/openvr_api.dll").write_bytes(make_pe())
    (g / "SUPERHOTVR_Data/Plugins/OVRPlugin.dll").write_bytes(make_pe(extra=b"LibOVRRT%hs_%d.dll"))
    (g / "SHVR.exe").write_bytes(make_pe() + b"\0" * 1024)
    (g / "resources").mkdir()
    (g / "resources/app.asar").write_bytes(b"asar")
    (g / "icudtl.dat").write_bytes(b"icu")
    (g / "resources.pak").write_bytes(b"pak")
    return g


def test_electron_launcher_ranked_below_the_game(tmp_path):
    g = _dual_unity(tmp_path)
    assert rift.is_electron(g / "SHVR.exe") and not rift.is_electron(g / "SUPERHOTVR.exe")
    ranked = rift.rank_exes(g, [g / "SUPERHOTVR.exe", g / "SHVR.exe"])
    assert ranked[0]["path"] == "SUPERHOTVR.exe"
    shvr = ranked[1]
    assert "Electron launcher (starts the game)" in shvr["reasons"]
    assert "Unity game (next to its data)" not in shvr["reasons"]
    assert not rift.is_ambiguous(ranked)


def test_catalog_from_another_build_keeps_launch_args(tmp_path, monkeypatch):
    """A catalog recipe verified with another build (other VR APIs) must not drop this build's own launch arguments:
    SUPERHOT VR's OpenXR build is in the catalog; the older Oculus + OpenVR build needs -vrmode OpenVR (#105)."""
    from frameport.recommend import catalog

    a = rift.analyze(_dual_unity(tmp_path))
    assert a.xr == "LibOVR+OpenVR" and a.extra["launch_args"] == "-vrmode OpenVR"
    a.package = "rift.superhot_vr"
    entry = catalog.CatalogEntry(package="rift.superhot_vr", title="SUPERHOT VR", status="works", xr="OpenXR",
                                 kind="rift", pcvr=["pcvr.xr_timefix"], pcvr_remove=["pcvr.revive"], as_is=True,
                                 verified={"date": "2026-10-05"})
    monkeypatch.setattr(catalog, "lookup", lambda pkg: entry)
    r = engine.suggest(a)
    assert r.patches["pcvr.launch_args"] == {"args": "-vrmode OpenVR"}
    assert "another build" in r.notes and "-vrmode OpenVR" in r.notes
    # the same build (same VR APIs): the verified recipe decides how the game starts, as before
    entry.xr = "LibOVR+OpenVR"
    r = engine.suggest(a)
    assert "pcvr.launch_args" not in r.patches and "another build" not in r.notes
    # a catalog entry that lists the arguments: the build's own ones, none for a build without any
    entry.pcvr = ["pcvr.launch_args", "pcvr.xr_timefix"]
    assert engine.suggest(a).patches["pcvr.launch_args"] == {"args": "-vrmode OpenVR"}
    a.extra["launch_args"] = ""
    assert "pcvr.launch_args" not in engine.suggest(a).patches


def test_superhot_catalog_entry_fits_both_builds(tmp_path, monkeypatch):
    from frameport.recommend import catalog

    monkeypatch.setenv("FRAMEPORT_NO_CATALOG_UPDATE", "1")
    entry = catalog.load(refresh=True).get("rift.superhot_vr")
    assert entry and "pcvr.launch_args" in entry.pcvr
    a = rift.analyze(_dual_unity(tmp_path))
    a.package = "rift.superhot_vr"
    monkeypatch.setattr(catalog, "lookup", lambda pkg: entry)
    assert engine.suggest(a).patches["pcvr.launch_args"] == {"args": "-vrmode OpenVR"}
