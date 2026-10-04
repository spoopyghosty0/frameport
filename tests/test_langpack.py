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
    return {"lib": d / "langpack.so", "fast": d / "langpack_fast.so", "original": original, "patched": patched, "hidden": hidden}


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
    assert (d["tag"], d["en"], d["native"]) == (b"de", b"German", "Deutsch".encode())
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
