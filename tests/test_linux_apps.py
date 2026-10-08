"""Linux apps (GitHub #31; arm64, x86_64 through FEX): detection of AppImages, folders and archives."""
import tarfile
import zipfile

import pytest

from frameport import pipeline
from frameport.analysis import linux


def elf(machine: int, appimage: bool = False, extra: bytes = b"") -> bytes:
    head = bytearray(64)
    head[:4] = b"\x7fELF"
    head[4], head[5] = 2, 1  # 64-bit, little endian
    if appimage:
        head[8:11] = b"AI\x02"
    head[18:20] = machine.to_bytes(2, "little")
    return bytes(head) + extra


def test_appimage_detection_and_title(tmp_path):
    p = tmp_path / "Venera-Prime-2.4.2-aarch64.AppImage"
    p.write_bytes(elf(linux.EM_AARCH64, appimage=True))
    info = linux.inspect(p)
    assert info["appimage"] and info["arch_ok"] and info["files"] == [p.name] and info["title"] == "Venera Prime"
    assert not info["openxr"]


def test_x86_64_builds_run_through_fex(tmp_path):
    p = tmp_path / "Thing-1.0-x86_64.AppImage"
    p.write_bytes(elf(linux.EM_X86_64, appimage=True))
    g = pipeline.add_linux_app(p)
    extra = g["analysis"]["extra"]
    assert extra["x86_64"] and g["analysis"]["abis"] == ["x86_64"]
    other = tmp_path / "riscv-tool"
    other.write_bytes(elf(243))
    with pytest.raises(ValueError, match="machine 243"):
        pipeline.add_linux_app(other)


def test_arm64_program_wins_over_x86_64(tmp_path):
    root = tmp_path / "Both"
    (root / "x86").mkdir(parents=True)
    (root / "x86" / "Both").write_bytes(elf(linux.EM_X86_64, extra=b"\0" * 4096))  # bigger: ranked first by size
    (root / "Both").write_bytes(elf(linux.EM_AARCH64))
    info = linux.inspect(root)
    assert info["exe"] == "Both" and info["arch_ok"] and info["candidates"] == ["Both"]
    (root / "Both").unlink()
    info = linux.inspect(root)
    assert info["exe"] == "x86/Both" and info["machine"] == linux.EM_X86_64 and not info["arch_ok"]


def test_archive_program_ranking_and_openxr(tmp_path):
    src = tmp_path / "src" / "MatineeVR"
    (src / "lib").mkdir(parents=True)
    (src / "MatineeVR").write_bytes(elf(linux.EM_AARCH64))
    (src / "crashpad_handler").write_bytes(elf(linux.EM_AARCH64))
    (src / "lib" / "libopenxr_loader.so.1").write_bytes(elf(linux.EM_AARCH64, extra=b"xrCreateInstance"))
    (src / "data" / "deep").mkdir(parents=True)
    archive = tmp_path / "MatineeVR-0.8-linux-arm64.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(src, arcname="MatineeVR")
    info = linux.inspect(archive)
    assert info["exe"] == "MatineeVR/MatineeVR" and info["openxr"] and info["arch_ok"]
    assert linux.inspect(archive)["root"] == info["root"]  # unpacked once


def test_bundled_data_libraries_dont_make_an_app_vr(tmp_path):
    root = tmp_path / "FramePort"
    (root / "data" / "artifacts" / "arm64-v8a").mkdir(parents=True)
    (root / "FramePort").write_bytes(elf(linux.EM_AARCH64))
    (root / "data" / "artifacts" / "arm64-v8a" / "libopenxr_loader.so").write_bytes(b"xrCreateInstance")
    assert not linux.inspect(root)["openxr"]


def test_zip_with_unsafe_paths_is_refused(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../evil", b"x")
    with pytest.raises(ValueError, match="unsafe"):
        linux.inspect(archive)


def test_add_linux_app_library_entry(tmp_path):
    p = tmp_path / "Tool-1.2-aarch64.AppImage"
    p.write_bytes(elf(linux.EM_AARCH64, appimage=True))
    g = pipeline.add_linux_app(p)
    assert g["package"] == "linux.tool" and g["kind"] == "linux" and pipeline.is_linux(g)
    assert g["exe"] == p.name and g["analysis"]["extra"]["appimage"]


def test_local_files_of_a_lone_appimage_never_include_its_folder(tmp_path):
    a, b = tmp_path / "Tool-1.2-aarch64.AppImage", tmp_path / "Other-1.0-aarch64.AppImage"
    a.write_bytes(elf(linux.EM_AARCH64, appimage=True))
    b.write_bytes(elf(linux.EM_AARCH64, appimage=True))
    (tmp_path / "keep.txt").write_text("the user's file")
    g = pipeline.add_linux_app(a)
    pipeline.add_linux_app(b)
    assert g["data_bytes"] == a.stat().st_size
    assert pipeline.local_game_files(g["package"]) == [a]  # another AppImage in the same folder doesn't block it
    pipeline.delete_local_files(g["package"])
    assert not a.exists() and b.exists() and (tmp_path / "keep.txt").exists()


def test_change_program_of_a_linux_folder(tmp_path):
    root = tmp_path / "Matinee"
    root.mkdir()
    (root / "Matinee").write_bytes(elf(linux.EM_AARCH64))
    (root / "helper").write_bytes(elf(linux.EM_AARCH64))
    g = pipeline.add_linux_app(root)
    assert g["exe"] == "Matinee" and pipeline.local_game_files(g["package"]) == [root]
    assert pipeline.set_exe(g["package"], "helper")["exe"] == "helper"


def test_linux_recipes_dont_follow_the_catalog(tmp_path, monkeypatch):
    from frameport.core import library

    p = tmp_path / "Tool-1.2-aarch64.AppImage"
    p.write_bytes(elf(linux.EM_AARCH64, appimage=True))
    g = pipeline.add_linux_app(p)
    monkeypatch.setattr(library, "REFRESH_ON_UPDATE", True)
    data = library.load()
    data["settings"]["recipes.app_version"] = "0.0.0"  # as after an app update
    library._follow_catalog(data)
    assert data["games"][g["package"]]["recipe"]["as_is"] is True


def test_failed_pc_game_on_stable_suggests_experimental_proton():
    from frameport.core import library

    library.upsert_game("rift.x", kind="rift", recipe={"package": "rift.x", "patches": {}},
                        installs={"frame": {"result": {"proton": "proton_11-arm64"}}})
    assert pipeline.proton_alternative_worth_trying("rift.x", "fail")
    assert not pipeline.proton_alternative_worth_trying("rift.x", "pass")
    recipe = pipeline.apply_suggestions("rift.x", [pipeline.PROTON_TOOL])
    assert recipe.patches[pipeline.PROTON_TOOL] == {"tool": "proton-experimental"}
    assert not pipeline.proton_alternative_worth_trying("rift.x", "fail")  # already on Experimental


# ------------------------------------------------------------------------------------------ the app's own icon (#99)
def png_bytes(size: int, colour=(200, 40, 40, 255)) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (size, size), colour).save(buf, "PNG")
    return buf.getvalue()


def test_find_icon_in_a_folder_app(tmp_path):
    root = tmp_path / "Tool-1.0-arm64"
    (root / "share/applications").mkdir(parents=True)
    (root / "share/applications/tool.desktop").write_text("[Desktop Entry]\nExec=tool\nIcon=org.tool\n")
    for size in (48, 256):
        d = root / f"share/icons/hicolor/{size}x{size}/apps"
        d.mkdir(parents=True)
        (d / "org.tool.png").write_bytes(png_bytes(size))
    (root / "share/icons/hicolor/scalable/apps").mkdir(parents=True)
    (root / "share/icons/hicolor/scalable/apps/org.tool.svg").write_text("<svg/>")
    assert linux.find_icon(root) == (root / "share/icons/hicolor/256x256/apps/org.tool.png").resolve()
    # nothing outside the app folder, also through a symlink
    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes(512))
    for p in list(root.rglob("org.tool.png")):
        p.unlink()
    (root / ".DirIcon").symlink_to(outside)
    assert linux.find_icon(root) is None
    assert linux.find_icon(tmp_path / "missing") is None


def test_add_linux_folder_uses_its_own_icon(tmp_path):
    from frameport.artwork import fetch, sources

    root = tmp_path / "Tool"
    root.mkdir()
    (root / "tool").write_bytes(elf(linux.EM_AARCH64))
    (root / "tool.desktop").write_text("[Desktop Entry]\nExec=tool\nIcon=tool\n")
    (root / "tool.png").write_bytes(png_bytes(128))
    g = pipeline.add_linux_app(root)
    d = fetch.artwork_dir(g["package"])
    assert (d / "icon.png").exists() and not (d / fetch.PICKED).exists()  # automatic: not the user's pick
    assert sources.icon_source(g["package"]) == "app"


def test_app_icon_never_replaces_a_chosen_icon(tmp_path):
    from frameport.artwork import fetch, sources

    pkg = "linux.tool"
    assert sources.icon_source(pkg) == "generated"
    assert sources.apply_app_icon(pkg, png_bytes(64)) is not None and sources.icon_source(pkg) == "app"
    assert sources.apply_app_icon(pkg, png_bytes(64)) is None  # unchanged
    assert sources.apply_app_icon(pkg, png_bytes(64, (0, 0, 255, 255))) is not None  # a new version's icon
    assert sources.apply_app_icon(pkg, b"not an image") is None and sources.apply_app_icon(pkg, png_bytes(8)) is None
    custom = tmp_path / "mine.png"
    custom.write_bytes(png_bytes(128, (0, 255, 0, 255)))
    sources.apply_custom(pkg, "icon", custom)
    assert sources.icon_source(pkg) == "custom"
    before = (fetch.artwork_dir(pkg) / "icon.png").read_bytes()
    assert sources.apply_app_icon(pkg, png_bytes(64)) is None
    assert (fetch.artwork_dir(pkg) / "icon.png").read_bytes() == before
    # store art the user picked (with no icon) also keeps the app's icon out of the library
    other = "linux.other"
    (fetch.artwork_dir(other) / fetch.PICKED).write_text("meta")
    assert sources.apply_app_icon(other, png_bytes(64)) is None


def test_install_takes_the_icon_the_frame_found(tmp_path):
    import base64

    from frameport.artwork import fetch, sources
    from frameport.core import library
    from frameport.core.events import Reporter
    from frameport.install import installer

    class Frame:
        def __init__(self):
            self.puts, self.runs = [], []

        def put(self, local, remote, resume=True):
            self.puts.append(remote)

        def run(self, command, stdin=None):
            self.runs.append(command)

    pkg = "linux.tool"
    library.upsert_game(pkg, kind="linux", title="Tool")
    frame = Frame()
    result = {"app_icon": {"file": "app-icon.png", "png": base64.b64encode(png_bytes(64)).decode()}}
    installer.apply_app_icon(frame, pkg, "/anchor", result, Reporter())
    assert sources.icon_source(pkg) == "app" and not (fetch.artwork_dir(pkg) / fetch.PICKED).exists()
    assert "/anchor/artwork/icon.png" in frame.puts  # the Frame's art set is sent again
    assert frame.runs == ["printf %s 'app' > '/anchor/artwork/.icon-source'"]
    frame.puts.clear()
    installer.apply_app_icon(frame, pkg, "/anchor", result, Reporter())  # unchanged: nothing sent
    installer.apply_app_icon(frame, pkg, "/anchor", {"app_icon": {"file": "app-icon.svg"}}, Reporter())
    assert frame.puts == []
