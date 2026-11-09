"""Games that mount their OBB as a disk image (GitHub #308, GRID Legends): Android's StorageManager.mountObb() asks
vold to attach the .obb (a FAT image made with Google's jobb tool) to a loop device and mount it; the game then reads
its data below StorageManager.getMountedObbPath(). Lepton's Android 11 has neither loop devices nor device-mapper
(`vold: Failed to open loop-control`), so every mount fails with state 21 (ERROR_COULD_NOT_MOUNT) and the game throws
`RuntimeException: Can't mount OBB …`.

FramePort mounts nothing: the agent unpacks each FAT image on the Frame into a folder *with the image's name*
(`obb/DataSet0.obb/` instead of the file `obb/DataSet0.obb`; agent `unpack_obb`, after the install), and this patch
rewrites the game's OBB helper so that "mounting" reports success at once and the "mount point" is that folder, i.e.
the OBB's own path. The folder keeps the checks that look at the file working (File.exists(), the `.obb` count in the
OBB folder), the storage cost is the data once (the image is removed after unpacking), and Lepton's read-only bind
mount of the OBB folder shows it to the game unchanged.

Handlers: only Codemasters' `androidutils.OBBHandler` (GRID Legends; its native side, libandroid_utils.so, already
accepts a synchronous MOUNTED callback: that is its "already mounted" path) is rewritten so far. Other mountObb users
are detected (analysis `obb_mount`) and reported, but need their own handler entry (or a dex writer that can add
methods). The edit is in place (Dex.rewrite_start): `mountOBB(name)` → `onMountOBBStateChanged(1 /* MOUNTED */,
name); return`, `getMountedOBBFullPath(name)` → `return getOBBFullPath(name)`. unmountOBB stays: in Lepton
isObbMounted() is false, so it already reports "not mounted" (23), which the game accepts.

The folder's path is longer than vold's mount points, which changes who frees it in native code: see NATIVE_IMPORTS
(libfp_obbpath.so hands the string over from the right allocator; no game code is changed).

Encrypted OBBs (jobb -k: PBKDF2 + Twofish, footer flag OBB_SALTED) are not supported; GRID's are plain FAT32 and the
game passes no key. FAT is case-insensitive, the Frame's file systems are not: unpacked names keep the image's case,
and a game that asks for another spelling finds nothing. GRID does: libgrid.so opens `<mount>/Index.txt`, while its
images store `index.txt` (strace on the Frame, 2026-11-09: ENOENT for every DataSetN.obb/Index.txt, the game then
stops loading; with an `Index.txt` link it ran). So the libraries this patch already routes (see NATIVE_IMPORTS) get
their file name strings respelled to the images' spelling (INDEX_NAMES; an exact, whole NUL-terminated string in
.rodata, same length, in place; more than one such string = unclear what each one is for → left alone and noted).
"""
from __future__ import annotations

from ...analysis import elf
from ...apk.dex import Dex
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact

MOUNTED = 1  # android.os.storage.OnObbStateChangeListener.MOUNTED

# class -> (mount method, mounted-path method, state callback (I,String)V, image path helper (String)String)
HANDLERS = {
    "Landroidutils/OBBHandler;": ("mountOBB", "getMountedOBBFullPath", "onMountOBBStateChanged", "getOBBFullPath"),
}

# The mounted path on its way to native code. Codemasters' libandroid_utils.so returns it as a libc++ std::string
# (AndroidUtils::getMountedOBBFullPath(const char*)), and the game library (GRID: libgrid.so, getOBBMountPoints())
# frees it with its *own* exported operator delete, inlined (it reads an allocator header in front of the text).
# libandroid_utils.so is loaded first (androidutils.NativeActivity's static block), so its strings come from libc++'s
# operator new. On a Quest that never shows: vold's mount point "/mnt/obb/obb:N" fits inside the string object
# (libc++ keeps up to 22 characters there), nothing is on the heap. The unpacked folder's path is longer, and the
# game crashed in that delete (SIGSEGV at getOBBMountPoints+420, fault address = the path's text; device run
# 2026-10-16). Fix without touching the game's code: every library that imports the function *and* exports its own
# operator new gets the import renamed to the helper's (elf.rename_imports, in place) + the helper in DT_NEEDED;
# native/obbpath copies a long result into a block from that library's own operator new and frees the original with
# the operator delete of the library that made it. Matched by symbol names only, so other builds work the same.
NATIVE_IMPORTS = {"_ZN12AndroidUtils21getMountedOBBFullPathEPKc": "fp_obb_mounted_path"}
HELPER = "libfp_obbpath.so"
OWN_NEW = "_Znwm"  # operator new(unsigned long)


def needs_mounted_path_helper(data: bytes) -> bool:
    """A library that takes the mounted path from another library and has its own operator new (so its delete of
    that string doesn't match the allocator that made it)."""
    imports = elf.dyn_symbols(data, defined=False)
    return bool(set(NATIVE_IMPORTS) & imports) and OWN_NEW in elf.dyn_symbols(data, defined=True)


def route_mounted_path(data: bytes) -> tuple[bytes, list[str]]:
    """The library with its mounted-path import(s) going to the helper (renamed + DT_NEEDED); (data, renamed)."""
    patched, renamed = elf.rename_imports(data, NATIVE_IMPORTS)
    if renamed or set(NATIVE_IMPORTS.values()) & elf.dyn_symbols(patched, defined=False):
        patched = elf.add_needed(patched, HELPER, first=False)
    return patched, renamed


# File names the game library opens below the mount point, spelt differently from the files inside the images (FAT
# doesn't care, the Frame's file systems do). Key: the library's spelling, value: the images' spelling (same length).
INDEX_NAMES = {"Index.txt": "index.txt"}


def _whole_rodata_strings(data: bytes, text: str) -> int:
    """How many times `text` is a whole NUL-terminated string in .rodata (not the tail of a longer one)."""
    return elf.replace_rodata_string(data, text, text)[1]


def respell_names(data: bytes) -> tuple[bytes, list[str], list[str]]:
    """The library with each INDEX_NAMES string respelt where it occurs exactly once; (data, respelt, unclear) —
    unclear = names found more than once (each could be anything, so none is changed)."""
    done, unclear = [], []
    for old, new in INDEX_NAMES.items():
        count = _whole_rodata_strings(data, old)
        if count > 1:
            unclear.append(old)
        elif count == 1:
            data, _n = elf.replace_rodata_string(data, old, new)
            done.append(old)
    return data, done, unclear


def handler_classes(data: bytes) -> list[str]:
    """Known OBB-mounting handler classes in one dex's bytes (cheap byte check, for analysis)."""
    return [c for c in HANDLERS if c.encode() in data and b"mountObb" in data]


def patch_dex(dex: Dex) -> list[str]:
    """Rewrite every known handler in this dex; returns 'Class.method' for each method changed ([] = nothing)."""
    done = []
    for cls, (mount, mounted_path, callback, image_path) in HANDLERS.items():
        cb = [i for i in dex.method_index(cls, callback) if dex.shorty(i) == "VIL"]
        path = [i for i in dex.method_index(cls, image_path) if dex.shorty(i) == "LL"]
        if not cb or not path:
            continue
        for idx, code in dex.class_methods(cls):
            name = dex.method(idx)[1]
            if name not in (mount, mounted_path) or dex.shorty(idx) != ("VL" if name == mount else "LL"):
                continue
            regs, ins, _n = dex.code_header(code)
            arg = regs - ins  # static (String): the name is the last register
            if ins != 1 or regs < 2 or arg >= 16:
                continue
            if name == mount:  # const/4 v0, #1; invoke-static {v0, vArg}, callback; return-void
                new = [0x0012 | (MOUNTED << 12)] + Dex.invoke_static(cb[0], [0, arg]) + [0x000E]
            else:  # invoke-static {vArg}, image path; move-result-object v0; return-object v0
                new = Dex.invoke_static(path[0], [arg]) + [0x000C, 0x0011]
            if dex.rewrite_start(code, new):
                done.append(f"{cls[1:-1].replace('/', '.')}.{name}")
    return done


class ObbMountEmulation(Patch):
    id = "frame.obb_mount_emul"
    title = "OBB disk images: unpack instead of mounting"
    description = ("Some games keep their data in OBB files that Android mounts as disk images (for example GRID "
                   "Legends). Lepton can't mount disk images, so these games quit at start (\"Can't mount OBB\"). "
                   "FramePort unpacks the images on the Frame after the install (into folders with the same names, "
                   "so the data takes its space only once) and changes the game's OBB code so it uses those "
                   "folders instead of mounting.")
    order = 44
    needs_vr = False
    experimental = True
    revision = 2  # 2: file names in the images' spelling (GRID's Index.txt)

    def applies(self, a):
        return bool((a.extra or {}).get("obb_mount"))

    def detect(self, a):
        found = (a.extra or {}).get("obb_mount") or []
        if any(c in HANDLERS for c in found):
            return Suggestion(True, "The game mounts its OBB files as disk images, which Lepton can't do: unpack "
                                    "them on the Frame and let the game read the folders.")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        changed = found = False
        for name in sorted(n for n in ctx.ws.names() if n.startswith("classes") and n.endswith(".dex")):
            data = ctx.ws.read(name)
            if not handler_classes(data):
                continue
            found = True
            dex = Dex(data)
            done = patch_dex(dex)
            if done:
                ctx.ws.put(name, dex.finish())
                ctx.notes.append(f"{name}: OBB mounting replaced by unpacked folders in {', '.join(sorted(done))}")
                changed = True
        if found:
            changed |= self.apply_native(ctx)
        return changed

    def apply_native(self, ctx: ApkContext) -> bool:
        """Route the mounted path through the helper in every library that frees it with its own allocator."""
        ws = ctx.ws
        if ws.abi != "arm64-v8a":
            return False
        needles = tuple(n.encode() for n in NATIVE_IMPORTS) + tuple(n.encode() for n in NATIVE_IMPORTS.values())
        changed = False
        for lib in ws.libs():
            if lib == HELPER or not ws.contains(ws.lib(lib), needles):
                continue
            data = ws.read_lib(lib)
            if not needs_mounted_path_helper(data) and HELPER not in elf.needed(data):
                continue
            patched, renamed = route_mounted_path(data)
            if patched != data:
                ctx.notes.append(f"{lib}: mounted OBB path through {HELPER} (its own allocator)")
            patched, respelt, unclear = respell_names(patched)
            for old in respelt:
                ctx.notes.append(f"{lib}: opens {INDEX_NAMES[old]} instead of {old} below the mount point (the "
                                 f"images' spelling; the Frame's file names are case-sensitive)")
            for old in unclear:
                ctx.notes.append(f"{lib}: {old} appears more than once, left as is (the game may not find it)")
            if patched != data:
                ws.put(ws.lib(lib), patched)
            changed = True
        if changed:
            helper = artifact(ws.abi, HELPER)
            if not ws.has(ws.lib(HELPER)) or ws.read_lib(HELPER) != helper:
                ws.put(ws.lib(HELPER), helper)
        return changed

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if ws.abi != "arm64-v8a":
            return []
        needles = tuple(n.encode() for n in NATIVE_IMPORTS)
        bad = [lib for lib in ws.libs() if lib != HELPER and ws.contains(ws.lib(lib), needles)
               and needs_mounted_path_helper(ws.read_lib(lib))]
        if not bad and not ws.has(ws.lib(HELPER)):
            return []
        checks = [("Mounted OBB path from the game's allocator", not bad and ws.has(ws.lib(HELPER)),
                   HELPER if not bad else f"still imported directly by {', '.join(bad)}")]
        routed = [lib for lib in ws.libs() if lib != HELPER and HELPER.encode() in ws.read_lib(lib)
                  and HELPER in elf.needed(ws.read_lib(lib))]
        left = {lib: [old for old in INDEX_NAMES if _whole_rodata_strings(ws.read_lib(lib), old)] for lib in routed}
        left = {lib: names for lib, names in left.items() if names}
        if routed:
            # warning only: a name found more than once is left alone on purpose (respell_names)
            checks.append(("OBB file names in the images' spelling", None if left else True,
                           "; ".join(f"{lib} still opens {', '.join(n)}" for lib, n in left.items())
                           or ", ".join(routed)))
        return checks


register(ObbMountEmulation)
