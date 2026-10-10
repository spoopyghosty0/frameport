"""Language packs (`<tag>.lang` files in the game's data) for the Meta Platform SDK (native/langpack)."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact

LOADER = "libovrplatformloader.so"
LIB = "libfp_langpack.so"
META_MARK = b"@FPMETA@\0"  # native/langpack: g_meta_slot = marker + META_LEN bytes the Metadata is written into
META_LEN = 96


def with_metadata(lib: bytes, text: str) -> bytes:
    """The library with `text` (the game's versionName) as the Metadata of the packs it serves.

    Unreal games such as Deadpool VR only treat a pack as installed when its Metadata equals their own version string,
    which Meta's store sets when the pack is uploaded."""
    raw = text.encode("utf-8")
    at = lib.find(META_MARK)
    if not raw or len(raw) >= META_LEN or at < 0:
        return lib
    at += len(META_MARK)
    return lib[:at] + raw + b"\0" * (META_LEN - len(raw)) + lib[at + META_LEN:]

ASSETS_MARK = b"@FPASSETS@"  # native/langpack: g_assets_slot = marker + '0'/'1' (content files listed or not)


def with_assets(lib: bytes) -> bytes:
    """The library with content files (*.pak next to the OBB) listed as installed asset files."""
    at = lib.find(ASSETS_MARK + b"0")
    if at < 0:
        return lib
    at += len(ASSETS_MARK)
    return lib[:at] + b"1" + lib[at + 1:]

# Every function native/langpack/langpack.c defines. The loader's own exports of these are hidden (see
# elf.hide_exports) so lookups reach LIB first; tests/test_langpack.py keeps this list equal to the C source.
EXPORTS = (
    "ovr_AssetDetailsArray_GetElement", "ovr_AssetDetailsArray_GetSize", "ovr_AssetDetails_GetAssetId",
    "ovr_AssetDetails_GetAssetType", "ovr_AssetDetails_GetDownloadStatus", "ovr_AssetDetails_GetFilepath",
    "ovr_AssetDetails_GetIapStatus", "ovr_AssetDetails_GetLanguage", "ovr_AssetDetails_GetMetadata",
    "ovr_AssetFileDownloadResult_GetAssetId", "ovr_AssetFileDownloadResult_GetFilepath",
    "ovr_AssetFileDownloadUpdate_GetAssetFileId", "ovr_AssetFileDownloadUpdate_GetBytesTotal",
    "ovr_AssetFileDownloadUpdate_GetBytesTransferred", "ovr_AssetFileDownloadUpdate_GetCompleted",
    "ovr_AssetFile_DownloadById", "ovr_AssetFile_GetList", "ovr_AssetFile_StatusById", "ovr_Error_GetCode",
    "ovr_Error_GetDisplayableMessage", "ovr_Error_GetHttpCode",
    "ovr_Error_GetMessage", "ovr_FreeMessage", "ovr_LanguagePackInfo_GetEnglishName",
    "ovr_LanguagePackInfo_GetNativeName", "ovr_LanguagePackInfo_GetTag", "ovr_LanguagePack_GetCurrent",
    "ovr_LanguagePack_SetCurrent", "ovr_Message_GetAssetDetails", "ovr_Message_GetAssetDetailsArray",
    "ovr_Message_GetAssetFileDownloadResult", "ovr_Message_GetAssetFileDownloadUpdate", "ovr_Message_GetError",
    "ovr_Message_GetRequestID",
    "ovr_Message_GetType", "ovr_Message_IsError", "ovr_PopMessage",
)


class LanguagePacks(Patch):
    id = "frame.langpacks"
    title = "Language packs from the game's files"
    description = ("OVRPort's platform loader answers the Meta Platform SDK's language-pack calls "
                   "(ovr_LanguagePack_GetCurrent/SetCurrent) with 'no request', so a game that asks for its language "
                   "pack never gets an answer. Adds a small library that serves language packs (<tag>.lang, for "
                   "example de.lang) found in the game's own data folders (OBB and app files) through the same calls, "
                   "the asset-file list and the status call, without any download. The game then finds the file path "
                   "itself. The pack it applies with SetCurrent wins; otherwise the environment variable "
                   "FRAMEPORT_LANGPACK (a tag), otherwise the only pack there is. Everything else goes to the "
                   "loader unchanged. Off by default: turn it on for a game whose data has such files (the game "
                   "page lists the patch only then).")
    order = 32
    experimental = True  # opt-in: checked in a headset with Deadpool VR, AC Nexus and Asgard's Wrath 2 only

    # never part of a default recipe (few games tested): shown under Customize for games whose data has
    # language packs, off until the user turns it on

    def applies(self, a):
        return LOADER in a.libs and "arm64-v8a" in a.abis and bool((a.extra or {}).get("lang_packs"))

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not ws.has(ws.lib(LOADER)):
            return False
        loader = ws.read_lib(LOADER)
        patched, hidden = elf.hide_exports(loader, EXPORTS)
        if not hidden and LIB in elf.needed(loader):
            return False  # applied by an earlier build
        if not hidden:
            ctx.notes.append("the platform loader has none of the language-pack functions: nothing to replace")
            return False
        patched = elf.add_needed(patched, LIB)
        ws.put(ws.lib(LOADER), patched)
        version = (ctx.analysis.version or "").strip()
        ws.put(ws.lib(LIB), with_metadata(artifact(ws.abi, LIB), version))
        ctx.notes.append(f"language packs served by {LIB} ({len(hidden)} loader functions replaced, "
                         f"Metadata \"{version}\")")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if not ws.has(ws.lib(LOADER)):
            return []
        linked = LIB in elf.needed(ws.read_lib(LOADER))
        return [("Language-pack library linked to the platform loader", linked and ws.has(ws.lib(LIB)), LIB)]


class AssetFiles(Patch):
    id = "frame.asset_files"
    title = "Content files from the game's data"
    description = ("Some games keep content in separate files next to their OBB (for example Star Wars: Tales from the "
                   "Galaxy's Edge: its seasons and sound banks) and ask Meta's platform for them as asset files. "
                   "OVRPort's platform loader doesn't know them, so the game waits for content that never arrives "
                   "(black screen after loading). Lists the content files shipped with the game's data (*.pak) as "
                   "installed and answers their download request at once with the file's path; nothing is copied or "
                   "downloaded. Shares the language-pack library.")
    order = 33

    def applies(self, a):
        extra = a.extra or {}
        return LOADER in a.libs and "arm64-v8a" in a.abis and bool(extra.get("asset_files"))

    def detect(self, a):
        if self.applies(a) and (a.extra or {}).get("asset_file_api"):
            n = len(a.extra["asset_files"])
            return Suggestion(True, f"The game asks Meta's platform for asset files and its data has {n} content "
                                    "file(s) (*.pak) that OVRPort's loader doesn't report.")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not ws.has(ws.lib(LOADER)):
            return False
        loader = ws.read_lib(LOADER)
        if LIB not in elf.needed(loader):  # frame.langpacks didn't link the library: do it here
            patched, hidden = elf.hide_exports(loader, EXPORTS)
            if not hidden:
                ctx.notes.append("the platform loader has none of the asset-file functions: nothing to replace")
                return False
            ws.put(ws.lib(LOADER), elf.add_needed(patched, LIB))
            ws.put(ws.lib(LIB), with_metadata(artifact(ws.abi, LIB), (ctx.analysis.version or "").strip()))
        lib = ws.read_lib(LIB)
        flagged = with_assets(lib)
        if flagged == lib:
            return False
        ws.put(ws.lib(LIB), flagged)
        ctx.notes.append(f"content files listed as installed asset files by {LIB}")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if not ws.has(ws.lib(LIB)):
            return []
        return [("Content files listed (asset-file library)", ASSETS_MARK + b"1" in ws.read_lib(LIB), LIB)]


register(LanguagePacks)
register(AssetFiles)
