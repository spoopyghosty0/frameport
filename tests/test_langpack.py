"""Language packs (native/langpack + patch frame.langpacks).

The C library is compiled for this host and driven through ctypes against a stand-in for OVRPort's platform loader
(tests/fixtures/src/fakeloader.c) that went through the real ELF patch (elf.hide_exports): lookups for the hidden
names no longer find the loader's functions, and the library still reaches them by reading the loader's .dynsym.
Needs a C compiler on Linux; the device side (bionic) is not covered here."""
import ctypes as C
import platform
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from frameport.analysis import elf, langpacks
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.frame import langpack as patch_mod

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "native/langpack/langpack.c"
FAKE = Path(__file__).with_name("fixtures") / "src" / "fakeloader.c"
CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
pytestmark = pytest.mark.skipif(not CC or platform.system() != "Linux", reason="needs a C compiler on Linux")

GETLIST, STATUSBYID, GETCURRENT, SETCURRENT = 0x4AFC6F74, 0x5D955D38, 0x1F90F0D5, 0x5B4FBBE0
VOIDP = C.c_void_p


def _analysis(**kw):
    d = dict(package="com.example.questgame", version="1.0", label="Quest Game", abis=["arm64-v8a"], engine="Unity",
             xr="OpenXR", graphics="Vulkan (declared in manifest)", direct_vrapi=False, libs=[], launcher_activity=None,
             has_info_category=True, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
             oculus_os_classes=False, is_overport_output=True, debuggable=True, extra={"size": 1})
    d.update(kw)
    return Analysis(**d)


def _compile(src, out, *extra):
    subprocess.run([CC, "-shared", "-fPIC", "-O1", "-Wall", "-Wextra", "-Werror", *extra, str(src), "-o", str(out),
                    "-ldl", "-lpthread"], check=True, capture_output=True)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("langpack-build")
    _compile(FAKE, d / "fakeloader.so")
    _compile(SRC, d / "langpack.so", "-fvisibility=hidden")
    _compile(SRC, d / "langpack_fast.so", "-fvisibility=hidden", "-DFP_LIST_TIMEOUT_MS=100")
    original = (d / "fakeloader.so").read_bytes()
    patched, hidden = elf.hide_exports(original, patch_mod.EXPORTS)
    return {"lib": d / "langpack.so", "fast": d / "langpack_fast.so", "original": original, "patched": patched,
            "hidden": hidden}


def _dlclose(lib):
    libdl = C.CDLL(None)
    libdl.dlclose.argtypes = [VOIDP]
    libdl.dlclose(lib._handle)


class Api:
    """The library under test (`lp`) and the stand-in loader behind it (`loader`)."""

    def __init__(self, lp, loader):
        self.lp, self.loader = lp, loader

    def f(self, name, res, *args, lib=None):
        fn = getattr(lib or self.lp, name)
        fn.restype, fn.argtypes = res, list(args)
        return fn

    def pop(self):
        return self.f("ovr_PopMessage", VOIDP)()

    def msg_type(self, m):
        return self.f("ovr_Message_GetType", C.c_uint32, VOIDP)(m)

    def req_id(self, m):
        return self.f("ovr_Message_GetRequestID", C.c_uint64, VOIDP)(m)

    def is_error(self, m):
        return bool(self.f("ovr_Message_IsError", C.c_bool, VOIDP)(m))

    def free(self, m):
        self.f("ovr_FreeMessage", None, VOIDP)(m)

    def details(self, d):
        s = lambda name: self.f(name, C.c_char_p, VOIDP)(d)  # noqa: E731
        lang = self.f("ovr_AssetDetails_GetLanguage", VOIDP, VOIDP)(d)
        out = {"id": self.f("ovr_AssetDetails_GetAssetId", C.c_uint64, VOIDP)(d),
               "type": s("ovr_AssetDetails_GetAssetType"), "status": s("ovr_AssetDetails_GetDownloadStatus"),
               "path": s("ovr_AssetDetails_GetFilepath")}
        if lang:
            ls = lambda name: self.f(name, C.c_char_p, VOIDP)(lang)  # noqa: E731
            out.update(tag=ls("ovr_LanguagePackInfo_GetTag"), en=ls("ovr_LanguagePackInfo_GetEnglishName"),
                       native=ls("ovr_LanguagePackInfo_GetNativeName"))
        return out

    def ask(self, name, *args):
        """Send a request, pop the answer, return (message type, error?, payload getter result)."""
        argtypes = [C.c_char_p] if args and isinstance(args[0], bytes) else ([C.c_uint64] if args else [])
        req = self.f(name, C.c_uint64, *argtypes)(*args)
        m = self.pop()
        assert m and self.req_id(m) == req
        return m


def _api(built, tmp_path, monkeypatch, which):
    monkeypatch.delenv("FRAMEPORT_LANGPACK", raising=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "libovrplatformloader.so").write_bytes(built["patched"])  # the name the library looks for
    shutil.copy(built[which], lib / "libfp_langpack.so")
    loader = C.CDLL(str(lib / "libovrplatformloader.so"))
    lp = C.CDLL(str(lib / "libfp_langpack.so"))
    yield Api(lp, loader)
    _dlclose(lp)
    _dlclose(loader)


@pytest.fixture
def api(built, tmp_path, monkeypatch):
    yield from _api(built, tmp_path, monkeypatch, "lib")


@pytest.fixture
def api_fast(built, tmp_path, monkeypatch):  # same library with a 100 ms GetList timeout
    yield from _api(built, tmp_path, monkeypatch, "fast")


@pytest.fixture
def packs(tmp_path, monkeypatch):
    root = tmp_path / "obb"
    (root / "sub").mkdir(parents=True)
    for rel in ("de.lang", "en-us.lang", "sub/fr.lang", "notes.txt", "x y.lang"):
        (root / rel).write_bytes(b"data")
    monkeypatch.setenv("FRAMEPORT_LANGPACK_DIRS", str(root))
    return root


def test_exports_match_the_c_source():
    text = SRC.read_text()
    defined = set(re.findall(r"^EXPORT\s+[^;{(]*?\b(ovr_\w+)\s*\(", text, re.M))
    assert defined == set(patch_mod.EXPORTS)
    assert len(patch_mod.EXPORTS) == len(defined)


def test_hide_exports_keeps_symbols_but_hides_them_from_lookup(built, tmp_path):
    assert set(built["hidden"]) >= {"ovr_PopMessage", "ovr_LanguagePack_GetCurrent", "ovr_AssetFile_GetList"}
    assert "fake_freed" not in built["hidden"]
    d = tmp_path / "orig"
    d.mkdir()
    (d / "a.so").write_bytes(built["original"])
    (d / "b.so").write_bytes(built["patched"])
    orig, patched = C.CDLL(str(d / "a.so")), C.CDLL(str(d / "b.so"))
    try:
        assert orig.ovr_PopMessage and patched.fake_freed  # visible before, others untouched
        with pytest.raises(AttributeError):
            patched.ovr_PopMessage  # noqa: B018  hidden: a lookup goes on to the next library
        # the entries are still defined (the library reads their addresses from .dynsym)
        assert "ovr_PopMessage" in elf.dyn_symbols(built["patched"], True)
        assert elf.hide_exports(built["patched"], patch_mod.EXPORTS)[1] == []  # idempotent
    finally:
        _dlclose(orig)
        _dlclose(patched)


def test_get_current_without_a_choice_is_an_error_when_several_packs(api, packs):
    m = api.ask("ovr_LanguagePack_GetCurrent")
    assert api.msg_type(m) == GETCURRENT and api.is_error(m)
    err = api.f("ovr_Message_GetError", VOIDP, VOIDP)(m)
    assert api.f("ovr_Error_GetCode", C.c_int, VOIDP)(err) == 404
    assert b"no language pack" in api.f("ovr_Error_GetMessage", C.c_char_p, VOIDP)(err)
    assert not api.f("ovr_Message_GetAssetDetails", VOIDP, VOIDP)(m)
    api.free(m)


def test_get_current_by_environment_and_single_pack(api, packs, monkeypatch, tmp_path):
    monkeypatch.setenv("FRAMEPORT_LANGPACK", "DE")
    m = api.ask("ovr_LanguagePack_GetCurrent")
    assert not api.is_error(m)
    d = api.details(api.f("ovr_Message_GetAssetDetails", VOIDP, VOIDP)(m))
    assert d["type"] == b"language_pack" and d["status"] == b"installed"
    assert d["path"] == str(packs / "de.lang").encode()
    assert (d["tag"], d["en"], d["native"]) == (b"de", b"German", b"Deutsch")
    assert d["id"] >> 48 == 0x4650
    api.free(m)
    # exactly one pack and nothing chosen: that one
    monkeypatch.delenv("FRAMEPORT_LANGPACK")
    only = tmp_path / "only"
    only.mkdir()
    (only / "de.lang").write_bytes(b"x")
    monkeypatch.setenv("FRAMEPORT_LANGPACK_DIRS", str(only))
    m = api.ask("ovr_LanguagePack_GetCurrent")
    assert api.details(api.f("ovr_Message_GetAssetDetails", VOIDP, VOIDP)(m))["tag"] == b"de"
    api.free(m)


def test_set_current_then_status_then_get_current(api, packs):
    m = api.ask("ovr_LanguagePack_SetCurrent", b"de-DE")  # the base language matches
    assert api.msg_type(m) == SETCURRENT and not api.is_error(m)
    res = api.f("ovr_Message_GetAssetFileDownloadResult", VOIDP, VOIDP)(m)
    asset = api.f("ovr_AssetFileDownloadResult_GetAssetId", C.c_uint64, VOIDP)(res)
    path = api.f("ovr_AssetFileDownloadResult_GetFilepath", C.c_char_p, VOIDP)(res)
    assert asset and path == str(packs / "de.lang").encode()
    api.free(m)

    m = api.ask("ovr_AssetFile_StatusById", asset)  # the confirm step of the documented flow
    assert api.msg_type(m) == STATUSBYID and not api.is_error(m)
    d = api.details(api.f("ovr_Message_GetAssetDetails", VOIDP, VOIDP)(m))
    assert (d["status"], d["path"], d["id"]) == (b"installed", path, asset)
    api.free(m)

    m = api.ask("ovr_LanguagePack_GetCurrent")  # the choice sticks
    assert api.details(api.f("ovr_Message_GetAssetDetails", VOIDP, VOIDP)(m))["tag"] == b"de"
    api.free(m)

    m = api.ask("ovr_AssetFile_StatusById", asset + 1)  # ours by prefix, but unknown
    assert api.is_error(m)
    api.free(m)


def test_set_current_unknown_pack_is_an_error(api, packs):
    for tag in (b"xx", b""):
        m = api.ask("ovr_LanguagePack_SetCurrent", tag)
        assert api.is_error(m) and not api.f("ovr_Message_GetAssetFileDownloadResult", VOIDP, VOIDP)(m)
        api.free(m)


def test_asset_list_adds_our_packs_to_the_loaders_list(api, packs):
    req = api.f("ovr_AssetFile_GetList", C.c_uint64)()
    assert req == 100  # the loader's own request id
    m = api.pop()
    assert api.msg_type(m) == GETLIST and api.req_id(m) == req and not api.is_error(m)
    arr = api.f("ovr_Message_GetAssetDetailsArray", VOIDP, VOIDP)(m)
    size = api.f("ovr_AssetDetailsArray_GetSize", C.c_size_t, VOIDP)(arr)
    items = [api.details(api.f("ovr_AssetDetailsArray_GetElement", VOIDP, VOIDP, C.c_size_t)(arr, i))
             for i in range(size)]
    assert size == 4
    ours = [i for i in items if i["type"] == b"language_pack"]
    assert {i["tag"] for i in ours} == {b"de", b"en-us", b"fr"} and {i["status"] for i in ours} == {b"installed"}
    store = [i for i in items if i["type"] == b"store"]  # the loader's own entry, served by the loader's accessors
    assert store == [{"id": 7, "type": b"store", "status": b"available", "path": b"/store/a"}]
    assert api.f("ovr_AssetDetailsArray_GetElement", VOIDP, VOIDP, C.c_size_t)(arr, 4) is None
    before = api.loader.fake_freed()
    api.free(m)  # frees the loader's message behind ours too
    assert api.loader.fake_freed() == before + 1


def _our_metadata(api):
    api.f("ovr_AssetFile_GetList", C.c_uint64)()
    m = api.pop()
    arr = api.f("ovr_Message_GetAssetDetailsArray", VOIDP, VOIDP)(m)
    get = api.f("ovr_AssetDetailsArray_GetElement", VOIDP, VOIDP, C.c_size_t)
    out = {api.f("ovr_AssetDetails_GetMetadata", C.c_char_p, VOIDP)(get(arr, i))
           for i in range(api.f("ovr_AssetDetailsArray_GetSize", C.c_size_t, VOIDP)(arr))
           if api.f("ovr_AssetDetails_GetAssetType", C.c_char_p, VOIDP)(get(arr, i)) == b"language_pack"}
    api.free(m)
    return out


def test_metadata_is_empty_without_the_games_version(api, packs, monkeypatch):
    monkeypatch.delenv("FRAMEPORT_LANGPACK_META", raising=False)
    assert _our_metadata(api) == {b""}


def test_metadata_from_the_environment(api, packs, monkeypatch):
    monkeypatch.setenv("FRAMEPORT_LANGPACK_META", "2.0.1.77.Quest")
    assert _our_metadata(api) == {b"2.0.1.77.Quest"}


def test_patch_writes_the_games_version_as_the_metadata(built, tmp_path, monkeypatch):
    monkeypatch.delenv("FRAMEPORT_LANGPACK_META", raising=False)
    lib = patch_mod.with_metadata(built["lib"].read_bytes(), "1.0.40.356975.Quest")
    assert len(lib) == built["lib"].stat().st_size and b"1.0.40.356975.Quest" in lib
    assert patch_mod.with_metadata(lib, "") == lib and patch_mod.with_metadata(b"no marker", "1") == b"no marker"
    assert patch_mod.with_metadata(lib, "x" * 200) == lib  # too long for the slot: left as it was
    d = tmp_path / "meta"
    d.mkdir()
    (d / "libovrplatformloader.so").write_bytes(built["patched"])
    (d / "libfp_langpack.so").write_bytes(lib)
    root = tmp_path / "obb"
    root.mkdir()
    (root / "de.lang").write_bytes(b"1")
    monkeypatch.setenv("FRAMEPORT_LANGPACK_DIRS", str(root))
    loader, lp = C.CDLL(str(d / "libovrplatformloader.so")), C.CDLL(str(d / "libfp_langpack.so"))
    try:
        assert _our_metadata(Api(lp, loader)) == {b"1.0.40.356975.Quest"}
    finally:
        _dlclose(lp)
        _dlclose(loader)


def _tags(api):
    api.f("ovr_AssetFile_GetList", C.c_uint64)()
    m = api.pop()
    arr = api.f("ovr_Message_GetAssetDetailsArray", VOIDP, VOIDP)(m)
    get = api.f("ovr_AssetDetailsArray_GetElement", VOIDP, VOIDP, C.c_size_t)
    size = api.f("ovr_AssetDetailsArray_GetSize", C.c_size_t, VOIDP)(arr)
    out = {api.details(get(arr, i)).get("tag") for i in range(size)}
    api.free(m)
    out.discard(None)
    return out


def test_packs_can_be_left_out_by_environment_or_file(api, packs, monkeypatch):
    assert _tags(api) == {b"de", b"en-us", b"fr"}
    monkeypatch.setenv("FRAMEPORT_LANGPACK_SKIP", "EN-US, x")
    assert _tags(api) == {b"de", b"fr"}
    monkeypatch.delenv("FRAMEPORT_LANGPACK_SKIP")
    (packs / "fp_langpack_skip").write_text("de\nfr\n")
    assert _tags(api) == {b"en-us"}
    (packs / "fp_langpack_skip").unlink()
    assert _tags(api) == {b"de", b"en-us", b"fr"}  # nothing remembered between lists


def test_everything_else_reaches_the_loader_unchanged(api, packs):
    # a status request for the loader's own asset: its message comes back as it is
    req = api.f("ovr_AssetFile_StatusById", C.c_uint64, C.c_uint64)(7)
    assert req == 100
    m = api.pop()
    assert api.msg_type(m) == STATUSBYID and not api.is_error(m)
    d = api.details(api.f("ovr_Message_GetAssetDetails", VOIDP, VOIDP)(m))
    assert d["path"] == b"/store/a"
    api.free(m)
    assert api.loader.fake_freed() == 1
    assert api.pop() is None


def test_asset_list_without_packs_is_the_loaders_message(api, tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEPORT_LANGPACK_DIRS", str(tmp_path / "nothing-here"))
    api.f("ovr_AssetFile_GetList", C.c_uint64)()
    m = api.pop()
    arr = api.f("ovr_Message_GetAssetDetailsArray", VOIDP, VOIDP)(m)
    assert api.f("ovr_AssetDetailsArray_GetSize", C.c_size_t, VOIDP)(arr) == 1
    api.free(m)
    assert api.loader.fake_freed() == 1


def test_asset_list_the_loader_never_answers_gets_our_packs_after_the_timeout(api_fast, packs, monkeypatch):
    monkeypatch.setenv("FAKE_SILENT_LIST", "1")
    req = api_fast.f("ovr_AssetFile_GetList", C.c_uint64)()
    assert req == 100
    assert api_fast.pop() is None  # not overdue yet
    time.sleep(0.2)
    m = api_fast.pop()
    assert m and api_fast.msg_type(m) == GETLIST and api_fast.req_id(m) == req and not api_fast.is_error(m)
    arr = api_fast.f("ovr_Message_GetAssetDetailsArray", VOIDP, VOIDP)(m)
    size = api_fast.f("ovr_AssetDetailsArray_GetSize", C.c_size_t, VOIDP)(arr)
    tags = {api_fast.details(api_fast.f("ovr_AssetDetailsArray_GetElement", VOIDP, VOIDP, C.c_size_t)(arr, i))["tag"]
            for i in range(size)}
    assert tags == {b"de", b"en-us", b"fr"}
    api_fast.free(m)
    assert api_fast.pop() is None  # answered once
    before = api_fast.loader.fake_freed()
    # the dispatcher's answer arrives after all: dropped, not a second answer
    api_fast.loader.fake_late_list(C.c_uint64(req))
    assert api_fast.pop() is None
    assert api_fast.loader.fake_freed() == before + 1


def test_find_tags_in_the_data_folder(tmp_path):
    root = tmp_path / "game"
    (root / "a" / "b" / "c" / "d").mkdir(parents=True)
    for rel in ("de.lang", "a/EN-us.LANG", "a/b/c/fr.lang", "a/b/c/d/too-deep.lang", "bad name.lang", "x.txt"):
        (root / rel).write_bytes(b"1")
    assert langpacks.find_tags(root) == ["de", "EN-us", "fr"]
    assert langpacks.find_tags(None) == [] and langpacks.find_tags(tmp_path / "missing") == []


def _apk(tmp_path, built):
    p = tmp_path / "in.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", b"manifest")
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr("lib/arm64-v8a/libovrplatformloader.so", built["original"])
    return p


def test_patch_links_the_library_and_hides_the_loaders_functions(built, tmp_path, monkeypatch):
    monkeypatch.setattr(patch_mod, "artifact", lambda abi, name: b"\x7fELF-langpack")
    patch = base.get("frame.langpacks")
    a = _analysis(libs=["libovrplatformloader.so"], extra={"size": 1, "lang_packs": ["de", "en-us"]})
    assert patch.applies(a)  # the game's data has language packs
    assert patch.detect(a) is None  # opt-in: never part of a suggested recipe
    assert not patch.applies(_analysis(libs=["libovrplatformloader.so"]))  # no language packs: nothing to serve
    assert not patch.applies(_analysis(libs=[], extra={"size": 1, "lang_packs": ["de"]}))  # no platform loader
    with ApkWorkspace(_apk(tmp_path, built)) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {})
        assert patch.apply(ctx)
        loader = ws.read_lib("libovrplatformloader.so")
        assert elf.needed(loader)[0] == "libfp_langpack.so"
        assert "ovr_PopMessage" in elf.dyn_symbols(loader, True)  # still defined, only no longer global
        assert ws.read_lib("libfp_langpack.so") == b"\x7fELF-langpack"
        assert all(ok for _, ok, _ in patch.validate(ctx))
        assert not patch.apply(base.ApkContext(ws, a, {}, Reporter(), {}))  # applied once is enough
        assert any("language packs" in n for n in ctx.notes)


def test_games_already_in_the_library_get_their_language_packs_after_an_app_update(tmp_path):
    """lang_packs is read from the data folder (no APK analysis): filled in once for existing entries."""
    from frameport.core import library

    data = tmp_path / "data"
    (data / "obb").mkdir(parents=True)
    (data / "obb" / "de.lang").write_bytes(b"x")
    g = {"data_dir": str(data), "analysis": {"package": "com.x.y", "extra": {}}}
    assert library._refresh_data_fields(g, g["analysis"])
    assert g["analysis"]["extra"]["lang_packs"] == ["de"]
    assert not library._refresh_data_fields(g, g["analysis"])  # once


DOWNLOADBYID, DOWNLOADUPDATE = 0x2D008992, 0x2FDD0CCD


def _list(api):
    api.f("ovr_AssetFile_GetList", C.c_uint64)()
    m = api.pop()
    arr = api.f("ovr_Message_GetAssetDetailsArray", VOIDP, VOIDP)(m)
    get = api.f("ovr_AssetDetailsArray_GetElement", VOIDP, VOIDP, C.c_size_t)
    items = []
    for i in range(api.f("ovr_AssetDetailsArray_GetSize", C.c_size_t, VOIDP)(arr)):
        d = get(arr, i)
        items.append({**api.details(d), "iap": api.f("ovr_AssetDetails_GetIapStatus", C.c_char_p, VOIDP)(d)})
    api.free(m)
    return items


def test_content_files_are_listed_only_when_switched_on(api, packs, monkeypatch):
    (packs / "Season1Taco-Android_ASTC.pak").write_bytes(b"x" * 10)
    monkeypatch.delenv("FRAMEPORT_ASSET_FILES", raising=False)
    assert not [i for i in _list(api) if i["type"] == b"default"]  # language-pack builds don't change
    monkeypatch.setenv("FRAMEPORT_ASSET_FILES", "1")
    content = [i for i in _list(api) if i["type"] == b"default"]
    assert content == [{"id": content[0]["id"], "type": b"default", "status": b"installed", "iap": b"free",
                        "path": str(packs / "Season1Taco-Android_ASTC.pak").encode()}]
    assert content[0]["id"] >> 48 == 0x4650 and "tag" not in content[0]
    m = api.ask("ovr_LanguagePack_GetCurrent")  # content files are never a language pack
    assert api.is_error(m)
    api.free(m)


def test_download_of_a_content_file_completes_at_once(api, packs, monkeypatch):
    (packs / "WwiseBanksCoreTaco-Android_ASTC.pak").write_bytes(b"y" * 12)
    monkeypatch.setenv("FRAMEPORT_ASSET_FILES", "1")
    asset = next(i["id"] for i in _list(api) if i["type"] == b"default")
    req = api.f("ovr_AssetFile_DownloadById", C.c_uint64, C.c_uint64)(asset)
    update = api.pop()  # Star Wars Tales waits for the update's "completed", then for the result
    assert api.msg_type(update) == DOWNLOADUPDATE and api.req_id(update) == 0
    u = api.f("ovr_Message_GetAssetFileDownloadUpdate", VOIDP, VOIDP)(update)
    assert api.f("ovr_AssetFileDownloadUpdate_GetAssetFileId", C.c_uint64, VOIDP)(u) == asset
    assert api.f("ovr_AssetFileDownloadUpdate_GetBytesTotal", C.c_uint64, VOIDP)(u) == 12
    assert api.f("ovr_AssetFileDownloadUpdate_GetBytesTransferred", C.c_longlong, VOIDP)(u) == 12
    assert api.f("ovr_AssetFileDownloadUpdate_GetCompleted", C.c_bool, VOIDP)(u)
    api.free(update)
    m = api.pop()
    assert api.msg_type(m) == DOWNLOADBYID and api.req_id(m) == req and not api.is_error(m)
    res = api.f("ovr_Message_GetAssetFileDownloadResult", VOIDP, VOIDP)(m)
    assert api.f("ovr_AssetFileDownloadResult_GetFilepath", C.c_char_p, VOIDP)(res) == \
        str(packs / "WwiseBanksCoreTaco-Android_ASTC.pak").encode()
    api.free(m)
    m = api.ask("ovr_AssetFile_DownloadById", asset + 1)  # ours by prefix, but unknown
    assert api.is_error(m)
    api.free(m)


def test_asset_files_patch_switches_the_library_on(built, tmp_path):
    lib = built["lib"].read_bytes()
    on = patch_mod.with_assets(lib)
    assert len(on) == len(lib) and patch_mod.ASSETS_MARK + b"1" in on and patch_mod.with_assets(on) == on
    data = tmp_path / "obb"
    data.mkdir()
    (data / "main.1.com.x.obb").write_bytes(b"o")
    (data / "Season1Taco-Android_ASTC.pak").write_bytes(b"p")
    assert langpacks.find_content_files(data) == ["Season1Taco-Android_ASTC.pak"]
    p = base.get("frame.asset_files")
    a = _analysis(libs=["libovrplatformloader.so", "libUE4.so"],
                  extra={"size": 1, "asset_files": ["Season1Taco-Android_ASTC.pak"], "asset_file_api": True})
    assert p.applies(a) and p.detect(a)
    assert not p.detect(_analysis(libs=["libovrplatformloader.so"], extra={"size": 1, "asset_files": ["a.pak"]}))


def test_asset_stub_empty_list_and_installed_status(api, packs, monkeypatch):
    monkeypatch.setenv("FRAMEPORT_ASSET_FILES_STUB", "1")
    monkeypatch.setenv("FAKE_SILENT_LIST", "1")
    m = api.ask("ovr_AssetFile_GetList")  # immediate, even with packs and a silent dispatcher
    arr = api.f("ovr_Message_GetAssetDetailsArray", VOIDP, VOIDP)(m)
    assert arr and api.f("ovr_AssetDetailsArray_GetSize", C.c_size_t, VOIDP)(arr) == 0
    get = api.f("ovr_AssetDetailsArray_GetElement", VOIDP, VOIDP, C.c_size_t)
    assert get(arr, 0) is None and get(arr, 2**32) is None
    api.free(m)
    for asset in (7, 0x4650000000001234):
        m = api.ask("ovr_AssetFile_StatusById", asset)
        assert api.msg_type(m) == STATUSBYID and not api.is_error(m)
        det = api.f("ovr_Message_GetAssetDetails", VOIDP, VOIDP)(m)
        assert api.details(det) == {"id": asset, "type": b"default", "status": b"installed", "path": b""}
        assert api.f("ovr_AssetDetails_GetIapStatus", C.c_char_p, VOIDP)(det) == b"free"
        path = api.f("ovr_AssetDetails_GetFilepath", VOIDP, VOIDP)(det)
        assert path and C.string_at(path) == b""  # allocated empty string, not a NULL pointer
        assert api.f("ovr_AssetDetails_GetMetadata", C.c_char_p, VOIDP)(det) == b""
        api.free(m)
    assert api.pop() is None
    assert api.loader.fake_freed() == 0  # no dispatcher requests were sent
    monkeypatch.setenv("FRAMEPORT_ASSET_FILES_STUB", "0")
    m = api.ask("ovr_AssetFile_StatusById", 7)
    assert api.details(api.f("ovr_Message_GetAssetDetails", VOIDP, VOIDP)(m))["path"] == b"/store/a"
    api.free(m)


def test_asset_stub_patch_without_language_packs(built, tmp_path, monkeypatch):
    lib = built["lib"].read_bytes()
    assert patch_mod.STUB_MARK + b"0" in lib
    assert patch_mod.STUB_MARK + b"0" in patch_mod.with_assets(lib)
    assert patch_mod.STUB_MARK + b"0" in patch_mod.with_metadata(lib, "1.0")
    on = patch_mod.with_stub(lib)
    assert len(on) == len(lib) and patch_mod.STUB_MARK + b"1" in on
    assert patch_mod.with_stub(on) == on
    monkeypatch.setattr(patch_mod, "artifact", lambda abi, name: lib)
    patch = base.get("frame.asset_files_stub")
    a = _analysis(libs=["libovrplatformloader.so"])
    assert patch.applies(a) and patch.detect(a) is None
    assert not patch.applies(_analysis(libs=[]))
    assert not patch.applies(_analysis(libs=["libovrplatformloader.so"], abis=["armeabi-v7a"]))
    with ApkWorkspace(_apk(tmp_path, built)) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {})
        assert patch.apply(ctx)
        loader = ws.read_lib(patch_mod.LOADER)
        assert elf.needed(loader)[0] == patch_mod.LIB
        assert not elf.hide_exports(loader, patch_mod.EXPORTS)[1]
        assert patch_mod.STUB_MARK + b"1" in ws.read_lib(patch_mod.LIB)
        assert all(ok for _, ok, _ in patch.validate(ctx))
        assert not patch.apply(ctx)


def test_asset_stub_marker_and_environment_override(built, tmp_path, monkeypatch):
    marked = tmp_path / "marked.so"
    marked.write_bytes(patch_mod.with_stub(built["lib"].read_bytes()))
    monkeypatch.delenv("FRAMEPORT_ASSET_FILES_STUB", raising=False)
    for api in _api({**built, "marked": marked}, tmp_path, monkeypatch, "marked"):
        assert _list(api) == []
        m = api.ask("ovr_AssetFile_StatusById", 7)
        assert not api.is_error(m)
        assert api.details(api.f("ovr_Message_GetAssetDetails", VOIDP, VOIDP)(m))["status"] == b"installed"
        api.free(m)
        monkeypatch.setenv("FRAMEPORT_ASSET_FILES_STUB", "0")
        assert _list(api)[0]["path"] == b"/store/a"


def test_asset_stub_via_platform_loader_handle(built, tmp_path, monkeypatch):
    """Unity's P/Invoke resolves against the dispatcher's handle, including its DT_NEEDED library."""
    lib = tmp_path / "libfp_langpack.so"
    lib.write_bytes(patch_mod.with_stub(built["lib"].read_bytes()))
    loader_path = tmp_path / "libovrplatformloader.so"
    loader_path.write_bytes(elf.add_needed(built["patched"], str(lib)))
    monkeypatch.delenv("FRAMEPORT_ASSET_FILES_STUB", raising=False)
    loader = C.CDLL(str(loader_path))
    api = Api(loader, loader)
    try:
        assert _list(api) == []
        m = api.ask("ovr_AssetFile_StatusById", 7)
        assert api.msg_type(m) == STATUSBYID and not api.is_error(m)
        assert api.details(api.f("ovr_Message_GetAssetDetails", VOIDP, VOIDP)(m))["status"] == b"installed"
        api.free(m)
        assert api.pop() is None
    finally:
        _dlclose(loader)


ORGSCOPEDID, USERPROOF = 0x18F0B01B, 0x22810483  # ovrMessage_User_GetOrgScopedID / _GetUserProof


def _org_id(api, user):
    """Ask for the org-scoped id of `user`: (request id, the popped message)."""
    req = api.f("ovr_User_GetOrgScopedID", C.c_uint64, C.c_uint64)(user)
    return req, api.pop()


def _org_value(api, m):
    return api.f("ovr_OrgScopedID_GetID", C.c_uint64, VOIDP)(api.f("ovr_Message_GetOrgScopedID", VOIDP, VOIDP)(m))


def test_org_scoped_id_is_answered_when_switched_on(api, monkeypatch):
    monkeypatch.setenv("FRAMEPORT_ORG_SCOPED_ID", "1")
    user = 0x53444835EC5A1B2C  # a frame.ovr_stable_user style id
    req, m = _org_id(api, user)
    assert req != 0 and m
    assert api.msg_type(m) == ORGSCOPEDID and api.req_id(m) == req and not api.is_error(m)
    assert not api.f("ovr_Message_GetError", VOIDP, VOIDP)(m)
    assert api.f("ovr_Message_GetOrgScopedID", VOIDP, VOIDP)(m)
    oid = _org_value(api, m)
    assert oid and oid < 2**63 and oid != user
    api.free(m)
    assert api.pop() is None  # one answer
    req2, m2 = _org_id(api, user)  # same user: same id, new request
    assert req2 != req and _org_value(api, m2) == oid
    api.free(m2)
    _, m3 = _org_id(api, user + 1)  # another user: another id
    assert _org_value(api, m3) != oid
    api.free(m3)
    assert api.loader.fake_freed() == 0  # nothing went to the loader


def test_org_scoped_id_off_reaches_the_loader(api, monkeypatch):
    monkeypatch.delenv("FRAMEPORT_ORG_SCOPED_ID", raising=False)
    req, m = _org_id(api, 5)  # the library's default (frame.langpacks builds): OVRPort's own answer, request id 0
    assert req == 0 and m is None


def test_other_requests_still_reach_the_loader_with_org_id_on(api, monkeypatch):
    monkeypatch.setenv("FRAMEPORT_ORG_SCOPED_ID", "1")
    req = api.f("ovr_User_GetUserProof", C.c_uint64, lib=api.loader)()  # not ours: the loader's own queue
    assert req == 100
    m = api.pop()
    assert api.msg_type(m) == USERPROOF and api.req_id(m) == req and not api.is_error(m)
    assert api.f("ovr_Message_GetOrgScopedID", VOIDP, VOIDP)(m) is None  # the loader's accessor (NULL)
    api.free(m)
    assert api.loader.fake_freed() == 1
    assert api.f("ovr_OrgScopedID_GetID", C.c_uint64, VOIDP)(16) == 0  # a foreign handle: the loader's function


def test_language_packs_can_be_passed_through(api, packs, monkeypatch):
    monkeypatch.setenv("FRAMEPORT_LANGPACK_SERVE", "0")  # what frame.ovr_org_scoped_id alone builds
    assert api.f("ovr_LanguagePack_GetCurrent", C.c_uint64)() == 0  # the loader's own `return 0`
    assert api.f("ovr_LanguagePack_SetCurrent", C.c_uint64, C.c_char_p)(b"de") == 0
    assert api.pop() is None
    assert _list(api) == [{"id": 7, "type": b"store", "status": b"available", "path": b"/store/a", "iap": b"free"}]
    monkeypatch.setenv("FRAMEPORT_LANGPACK_SERVE", "1")
    assert {i.get("tag") for i in _list(api)} >= {b"de", b"fr"}


def test_org_scoped_id_markers(built, tmp_path, monkeypatch):
    lib = built["lib"].read_bytes()
    assert patch_mod.ORG_MARK + b"0" in lib and patch_mod.LANGS_MARK + b"1" in lib
    on = patch_mod.with_org_id(patch_mod.without_langs(lib))
    assert len(on) == len(lib) and patch_mod.ORG_MARK + b"1" in on and patch_mod.LANGS_MARK + b"0" in on
    assert patch_mod.with_org_id(on) == on and patch_mod.without_langs(on) == on
    monkeypatch.delenv("FRAMEPORT_ORG_SCOPED_ID", raising=False)
    monkeypatch.delenv("FRAMEPORT_LANGPACK_SERVE", raising=False)
    root = tmp_path / "obb"
    root.mkdir()
    (root / "de.lang").write_bytes(b"1")
    monkeypatch.setenv("FRAMEPORT_LANGPACK_DIRS", str(root))
    marked = tmp_path / "marked.so"
    marked.write_bytes(on)
    for api in _api({**built, "marked": marked}, tmp_path, monkeypatch, "marked"):
        req, m = _org_id(api, 42)
        assert req and api.msg_type(m) == ORGSCOPEDID
        api.free(m)
        assert api.f("ovr_LanguagePack_GetCurrent", C.c_uint64)() == 0  # packs off by the marker
        assert not [i for i in _list(api) if i["type"] == b"language_pack"]


def test_org_scoped_id_patch(built, tmp_path, monkeypatch):
    from frameport.patches.frame import ovr_org_scoped_id

    lib = built["lib"].read_bytes()
    monkeypatch.setattr(ovr_org_scoped_id, "artifact", lambda abi, name: lib)
    patch = base.get("frame.ovr_org_scoped_id")
    plain = _analysis(libs=["libovrplatformloader.so"])
    assert patch.applies(plain) and patch.detect(plain) is None  # no sign that the game waits for it
    assert patch.detect(_analysis(package="quest.eleven.forfunlabs", libs=["libovrplatformloader.so"]))
    assert patch.detect(_analysis(libs=["libovrplatformloader.so"], extra={"size": 1, "cortopia_org_id": True}))
    assert not patch.applies(_analysis(libs=[]))
    assert not patch.applies(_analysis(libs=["libovrplatformloader.so"], abis=["armeabi-v7a"]))
    with ApkWorkspace(_apk(tmp_path, built)) as ws:
        ctx = base.ApkContext(ws, plain, {}, Reporter(), {})
        assert patch.apply(ctx)
        loader = ws.read_lib(patch_mod.LOADER)
        assert elf.needed(loader)[0] == patch_mod.LIB
        assert not elf.hide_exports(loader, patch_mod.EXPORTS)[1]  # every library function hidden in the loader
        out = ws.read_lib(patch_mod.LIB)
        assert patch_mod.ORG_MARK + b"1" in out and patch_mod.LANGS_MARK + b"0" in out  # language packs stay off
        assert all(ok for _, ok, _ in patch.validate(ctx))
        assert not patch.apply(ctx)  # once is enough


def test_org_scoped_id_patch_after_language_packs_keeps_them(built, tmp_path, monkeypatch):
    lib = built["lib"].read_bytes()
    monkeypatch.setattr(patch_mod, "artifact", lambda abi, name: lib)
    a = _analysis(libs=["libovrplatformloader.so"], extra={"size": 1, "lang_packs": ["de"]})
    with ApkWorkspace(_apk(tmp_path, built)) as ws:
        assert base.get("frame.langpacks").apply(base.ApkContext(ws, a, {}, Reporter(), {}))
        assert base.get("frame.ovr_org_scoped_id").apply(base.ApkContext(ws, a, {}, Reporter(), {}))
        out = ws.read_lib(patch_mod.LIB)
        assert patch_mod.ORG_MARK + b"1" in out and patch_mod.LANGS_MARK + b"1" in out
        assert elf.needed(ws.read_lib(patch_mod.LOADER)).count(patch_mod.LIB) == 1


GETVERSION = 0x68670A0E  # ovrMessage_Application_GetVersion


def test_app_version_check_is_answered_with_an_error_when_switched_on(api, monkeypatch):
    monkeypatch.setenv("FRAMEPORT_APP_VERSION", "1")
    req = api.f("ovr_Application_GetVersion", C.c_uint64)()
    m = api.pop()
    assert req != 0 and m and api.msg_type(m) == GETVERSION and api.req_id(m) == req
    assert api.is_error(m)  # the game's callback runs and takes its "couldn't check" path: no version is made up
    err = api.f("ovr_Message_GetError", VOIDP, VOIDP)(m)
    assert err and api.f("ovr_Error_GetCode", C.c_int, VOIDP)(err) == 1
    assert api.f("ovr_Error_GetHttpCode", C.c_int, VOIDP)(err) == 503
    assert b"store" in api.f("ovr_Error_GetMessage", C.c_char_p, VOIDP)(err)
    assert api.f("ovr_Message_GetApplicationVersion", VOIDP, VOIDP)(m) is None
    api.free(m)
    assert api.pop() is None  # one answer
    assert api.loader.fake_freed() == 0  # nothing went to the loader


def test_app_version_check_off_reaches_the_loader(api, monkeypatch):
    monkeypatch.delenv("FRAMEPORT_APP_VERSION", raising=False)
    assert api.f("ovr_Application_GetVersion", C.c_uint64)() == 0  # OVRPort's own `return 0`
    assert api.pop() is None


def test_app_version_marker(built, tmp_path, monkeypatch):
    lib = built["lib"].read_bytes()
    assert patch_mod.APPVER_MARK + b"0" in lib
    on = patch_mod.with_app_version(lib)
    assert len(on) == len(lib) and patch_mod.APPVER_MARK + b"1" in on and patch_mod.with_app_version(on) == on
    monkeypatch.delenv("FRAMEPORT_APP_VERSION", raising=False)
    marked = tmp_path / "marked.so"
    marked.write_bytes(on)
    for api in _api({**built, "marked": marked}, tmp_path, monkeypatch, "marked"):
        req = api.f("ovr_Application_GetVersion", C.c_uint64)()
        m = api.pop()
        assert req and api.msg_type(m) == GETVERSION and api.is_error(m)
        api.free(m)
        _, m = _org_id(api, 42)  # the other answers stay off
        assert m is None


def test_app_version_patch(built, tmp_path, monkeypatch):
    from frameport.patches.frame import ovr_app_version

    lib = built["lib"].read_bytes()
    monkeypatch.setattr(ovr_app_version, "artifact", lambda abi, name: lib)
    patch = base.get("frame.ovr_app_version")
    plain = _analysis(libs=["libovrplatformloader.so"])
    assert patch.applies(plain) and patch.detect(plain) is None  # no sign that the game waits for it
    assert patch.detect(_analysis(package="com.wolfandwood.csmashvs", libs=["libovrplatformloader.so"]))
    assert not patch.applies(_analysis(libs=[]))
    assert not patch.applies(_analysis(libs=["libovrplatformloader.so"], abis=["armeabi-v7a"]))
    with ApkWorkspace(_apk(tmp_path, built)) as ws:
        ctx = base.ApkContext(ws, plain, {}, Reporter(), {})
        assert patch.apply(ctx)
        loader = ws.read_lib(patch_mod.LOADER)
        assert elf.needed(loader)[0] == patch_mod.LIB
        assert not elf.hide_exports(loader, patch_mod.EXPORTS)[1]  # every library function hidden in the loader
        out = ws.read_lib(patch_mod.LIB)
        assert patch_mod.APPVER_MARK + b"1" in out and patch_mod.LANGS_MARK + b"0" in out
        assert patch_mod.ORG_MARK + b"0" in out  # only this answer
        assert all(ok for _, ok, _ in patch.validate(ctx))
        assert not patch.apply(ctx)  # once is enough


def test_app_version_patch_with_org_scoped_id_links_the_library_once(built, tmp_path, monkeypatch):
    from frameport.patches.frame import ovr_app_version, ovr_org_scoped_id

    lib = built["lib"].read_bytes()
    monkeypatch.setattr(ovr_org_scoped_id, "artifact", lambda abi, name: lib)
    monkeypatch.setattr(ovr_app_version, "artifact", lambda abi, name: lib)
    a = _analysis(libs=["libovrplatformloader.so"])
    with ApkWorkspace(_apk(tmp_path, built)) as ws:
        assert base.get("frame.ovr_org_scoped_id").apply(base.ApkContext(ws, a, {}, Reporter(), {}))
        assert base.get("frame.ovr_app_version").apply(base.ApkContext(ws, a, {}, Reporter(), {}))
        out = ws.read_lib(patch_mod.LIB)
        assert patch_mod.ORG_MARK + b"1" in out and patch_mod.APPVER_MARK + b"1" in out
        assert elf.needed(ws.read_lib(patch_mod.LOADER)).count(patch_mod.LIB) == 1
