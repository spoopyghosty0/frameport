"""Language packs (`<tag>.lang` files in the game's data) for the Meta Platform SDK (native/langpack)."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, register
from . import artifact

LOADER = "libovrplatformloader.so"
LIB = "libfp_langpack.so"

# Every function native/langpack/langpack.c defines. The loader's own exports of these are hidden (see
# elf.hide_exports) so lookups reach LIB first; tests/test_langpack.py keeps this list equal to the C source.
EXPORTS = (
    "ovr_AssetDetailsArray_GetElement", "ovr_AssetDetailsArray_GetSize", "ovr_AssetDetails_GetAssetId",
    "ovr_AssetDetails_GetAssetType", "ovr_AssetDetails_GetDownloadStatus", "ovr_AssetDetails_GetFilepath",
    "ovr_AssetDetails_GetIapStatus", "ovr_AssetDetails_GetLanguage", "ovr_AssetDetails_GetMetadata",
    "ovr_AssetFileDownloadResult_GetAssetId", "ovr_AssetFileDownloadResult_GetFilepath", "ovr_AssetFile_GetList",
    "ovr_AssetFile_StatusById", "ovr_Error_GetCode", "ovr_Error_GetDisplayableMessage", "ovr_Error_GetHttpCode",
    "ovr_Error_GetMessage", "ovr_FreeMessage", "ovr_LanguagePackInfo_GetEnglishName",
    "ovr_LanguagePackInfo_GetNativeName", "ovr_LanguagePackInfo_GetTag", "ovr_LanguagePack_GetCurrent",
    "ovr_LanguagePack_SetCurrent", "ovr_Message_GetAssetDetails", "ovr_Message_GetAssetDetailsArray",
    "ovr_Message_GetAssetFileDownloadResult", "ovr_Message_GetError", "ovr_Message_GetRequestID",
    "ovr_Message_GetType", "ovr_Message_IsError", "ovr_PopMessage",
)


class LanguagePacks(Patch):
    id = "frame.langpacks"
    title = "Language packs from the game's files"
    description = ("OVRPort's platform loader answers the Meta Platform SDK's language-pack calls "
                   "(ovr_LanguagePack_GetCurrent/SetCurrent) with 'no request', so a game that asks for its language "
                   "pack never gets an answer. Adds a small library that serves language packs (<tag>.lang, e.g. "
                   "de.lang) found in the game's own data folders (OBB and app files) through the same calls, the "
                   "asset-file list and the status call, without any download. The game then finds the file path "
                   "itself. The pack it applies with SetCurrent wins; otherwise the environment variable "
                   "FRAMEPORT_LANGPACK (a tag), otherwise the only pack there is. Everything else goes to the "
                   "loader unchanged. Off by default: turn it on for a game whose data has such files (the game "
                   "page lists the patch only then).")
    order = 32
    experimental = True  # written from the SDK documentation and the loader's code; not yet run in a headset

    # never part of a default recipe (not yet run in a headset): shown under Customize for games whose data has
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
        ws.put(ws.lib(LIB), artifact(ws.abi, LIB))
        ctx.notes.append(f"language packs served by {LIB} ({len(hidden)} loader functions replaced)")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if not ws.has(ws.lib(LOADER)):
            return []
        linked = LIB in elf.needed(ws.read_lib(LOADER))
        return [("Language-pack library linked to the platform loader", linked and ws.has(ws.lib(LIB)), LIB)]


register(LanguagePacks)
