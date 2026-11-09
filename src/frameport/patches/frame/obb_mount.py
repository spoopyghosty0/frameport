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

Encrypted OBBs (jobb -k: PBKDF2 + Twofish, footer flag OBB_SALTED) are not supported; GRID's are plain FAT32 and the
game passes no key. FAT is case-insensitive, the Frame's file systems are not: unpacked names keep the image's case
(GRID's own index.txt uses the same spelling as its files).
"""
from __future__ import annotations

from ...apk.dex import Dex
from ..base import ApkContext, Patch, Suggestion, register

MOUNTED = 1  # android.os.storage.OnObbStateChangeListener.MOUNTED

# class -> (mount method, mounted-path method, state callback (I,String)V, image path helper (String)String)
HANDLERS = {
    "Landroidutils/OBBHandler;": ("mountOBB", "getMountedOBBFullPath", "onMountOBBStateChanged", "getOBBFullPath"),
}


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

    def applies(self, a):
        return bool((a.extra or {}).get("obb_mount"))

    def detect(self, a):
        found = (a.extra or {}).get("obb_mount") or []
        if any(c in HANDLERS for c in found):
            return Suggestion(True, "The game mounts its OBB files as disk images, which Lepton can't do: unpack "
                                    "them on the Frame and let the game read the folders.")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        changed = False
        for name in sorted(n for n in ctx.ws.names() if n.startswith("classes") and n.endswith(".dex")):
            data = ctx.ws.read(name)
            if not handler_classes(data):
                continue
            dex = Dex(data)
            done = patch_dex(dex)
            if done:
                ctx.ws.put(name, dex.finish())
                ctx.notes.append(f"{name}: OBB mounting replaced by unpacked folders in {', '.join(sorted(done))}")
                changed = True
        return changed


register(ObbMountEmulation)
