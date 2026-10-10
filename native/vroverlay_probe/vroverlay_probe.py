#!/usr/bin/env python3
"""OpenVR overlay probe (Linux, python3 stdlib only): can an overlay application show something on the Steam Frame?

Runs on the Frame against SteamVR's own libopenvr_api.so through ctypes (the same pattern as the agent's
take_screenshot). It connects as VRApplication_Overlay, creates
  - a head-locked overlay 1.5 m in front of the headset (magenta/green checker, 0.6 m wide),
  - a world overlay 1.5 m in front of the standing origin (yellow/blue),
  - a dashboard overlay "FramePort probe" (thumbnail + main image),
logs every EVROverlayError and the visible flags, then stays alive (default 60 s), polling IsOverlayVisible /
IsDashboardVisible every 5 s.

    python3 vroverlay_probe.py [seconds] [--lib /path/libopenvr_api.so] [--show-dashboard]

Output: one line per call, "<call> -> <result>"; exit code 0 when every overlay call succeeded.
"""
import ctypes as C
import os
import struct
import sys
import time
import zlib

LIBS = ("/opt/steamvr/bin/linuxarm64/libopenvr_api.so", "/opt/steamvr/bin/linux64/libopenvr_api.so")
VRApplication_Overlay = 2
OVERLAY_TABLE = b"FnTable:IVROverlay_027"   # openvr_capi.h of OpenVR 2.5.1; the Frame's vrclient serves 010-028
# indexes into VR_IVROverlay_FnTable (IVROverlay_027)
IDX = dict(FindOverlay=0, CreateOverlay=1, DestroyOverlay=2, GetOverlayErrorNameFromEnum=7, SetOverlayFlag=10,
           SetOverlayAlpha=15, SetOverlaySortOrder=19, SetOverlayWidthInMeters=21, SetOverlayTransformAbsolute=32,
           SetOverlayTransformTrackedDeviceRelative=34, ShowOverlay=41, HideOverlay=42, IsOverlayVisible=43,
           SetOverlayRaw=60, SetOverlayFromFile=61, GetOverlayTextureSize=64, CreateDashboardOverlay=65,
           IsDashboardVisible=66, IsActiveDashboardOverlay=67, ShowDashboard=70)
H = C.c_uint64
ERR = C.c_int


class Mat34(C.Structure):
    _fields_ = [("m", (C.c_float * 4) * 3)]


def translation(x, y, z):
    m = Mat34()
    for r, row in enumerate(((1, 0, 0, x), (0, 1, 0, y), (0, 0, 1, z))):
        for c, v in enumerate(row):
            m.m[r][c] = v
    return m


def checker(w, h, a, b, cell=32):
    buf = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            col = a if ((x // cell) + (y // cell)) % 2 == 0 else b
            if x < 4 or y < 4 or x >= w - 4 or y >= h - 4:
                col = (255, 255, 255, 255)
            i = (y * w + x) * 4
            buf[i:i + 4] = bytes(col)
    return bytes(buf)


def write_png(path, w, h, rgba):
    raw = b"".join(b"\0" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def main():
    args = sys.argv[1:]
    seconds = float(next((a for a in args if a.replace(".", "").isdigit()), 60))
    lib = args[args.index("--lib") + 1] if "--lib" in args else next((p for p in LIBS if os.path.isfile(p)), None)
    if not lib:
        log("no libopenvr_api.so")
        return 2
    vr = C.CDLL(lib)
    vr.VR_InitInternal2.restype = C.c_uint32
    vr.VR_InitInternal2.argtypes = [C.POINTER(C.c_int), C.c_int, C.c_char_p]
    vr.VR_GetGenericInterface.restype = C.c_void_p
    vr.VR_GetGenericInterface.argtypes = [C.c_char_p, C.POINTER(C.c_int)]
    vr.VR_IsHmdPresent.restype = C.c_bool
    vr.VR_IsRuntimeInstalled.restype = C.c_bool
    log(f"lib {lib} hmd_present={vr.VR_IsHmdPresent()} runtime_installed={vr.VR_IsRuntimeInstalled()}")
    err = C.c_int(0)
    token = vr.VR_InitInternal2(C.byref(err), VRApplication_Overlay, None)
    log(f"VR_InitInternal2(Overlay) -> err={err.value} token={token}")
    if err.value:
        return 2
    ptr = vr.VR_GetGenericInterface(OVERLAY_TABLE, C.byref(err))
    log(f"VR_GetGenericInterface({OVERLAY_TABLE.decode()}) -> {hex(ptr or 0)} err={err.value}")
    if not ptr:
        vr.VR_ShutdownInternal()
        return 2
    table = (C.c_void_p * 83).from_address(ptr)

    def fn(name, restype, *argtypes):
        return C.CFUNCTYPE(restype, *argtypes)(table[IDX[name]])

    errname = fn("GetOverlayErrorNameFromEnum", C.c_char_p, ERR)
    failed = []

    def call(label, f, *a):
        r = f(*a)
        log(f"{label} -> {r} {errname(r).decode() if r else 'None'}")
        if r:
            failed.append(label)
        return r

    create = fn("CreateOverlay", ERR, C.c_char_p, C.c_char_p, C.POINTER(H))
    create_dash = fn("CreateDashboardOverlay", ERR, C.c_char_p, C.c_char_p, C.POINTER(H), C.POINTER(H))
    raw = fn("SetOverlayRaw", ERR, H, C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32)
    from_file = fn("SetOverlayFromFile", ERR, H, C.c_char_p)
    width = fn("SetOverlayWidthInMeters", ERR, H, C.c_float)
    rel = fn("SetOverlayTransformTrackedDeviceRelative", ERR, H, C.c_uint32, C.POINTER(Mat34))
    absolute = fn("SetOverlayTransformAbsolute", ERR, H, C.c_int, C.POINTER(Mat34))
    show = fn("ShowOverlay", ERR, H)
    visible = fn("IsOverlayVisible", C.c_bool, H)
    dash_visible = fn("IsDashboardVisible", C.c_bool)
    active_dash = fn("IsActiveDashboardOverlay", C.c_bool, H)
    tex_size = fn("GetOverlayTextureSize", ERR, H, C.POINTER(C.c_uint32), C.POINTER(C.c_uint32))
    sort = fn("SetOverlaySortOrder", ERR, H, C.c_uint32)
    show_dash = fn("ShowDashboard", None, C.c_char_p)

    tag = f"frameport.probe.{os.getpid()}"
    head, world, dmain, dthumb = H(), H(), H(), H()
    w = h = 256
    img1 = C.create_string_buffer(checker(w, h, (255, 0, 255, 255), (0, 200, 0, 255)), w * h * 4)
    img2 = C.create_string_buffer(checker(w, h, (255, 220, 0, 255), (0, 60, 255, 255)), w * h * 4)
    png = f"/tmp/{tag}.png"
    write_png(png, w, h, checker(w, h, (0, 220, 220, 255), (40, 40, 40, 255)))

    if not call(f"CreateOverlay({tag}.head)", create, f"{tag}.head".encode(), b"FramePort probe (head)", C.byref(head)):
        call("SetOverlayRaw(head)", raw, head, C.cast(img1, C.c_void_p), w, h, 4)
        call("SetOverlayWidthInMeters(head, 0.6)", width, head, 0.6)
        call("SetOverlayTransformTrackedDeviceRelative(head, hmd, z=-1.5)", rel, head, 0, C.byref(translation(0, 0, -1.5)))
        call("SetOverlaySortOrder(head, 10)", sort, head, 10)
        call("ShowOverlay(head)", show, head)
    if not call(f"CreateOverlay({tag}.world)", create, f"{tag}.world".encode(), b"FramePort probe (world)", C.byref(world)):
        call("SetOverlayRaw(world)", raw, world, C.cast(img2, C.c_void_p), w, h, 4)
        call("SetOverlayWidthInMeters(world, 1.0)", width, world, 1.0)
        call("SetOverlayTransformAbsolute(world, standing, y=1.4 z=-1.5)", absolute, world, 1,
             C.byref(translation(0, 1.4, -1.5)))
        call("ShowOverlay(world)", show, world)
    if not call(f"CreateDashboardOverlay({tag}.dash)", create_dash, f"{tag}.dash".encode(), b"FramePort probe",
                C.byref(dmain), C.byref(dthumb)):
        call("SetOverlayFromFile(dash main)", from_file, dmain, png.encode())
        call("SetOverlayFromFile(dash thumb)", from_file, dthumb, png.encode())
        call("SetOverlayWidthInMeters(dash, 1.5)", width, dmain, 1.5)
    if "--show-dashboard" in args:
        show_dash(f"{tag}.dash".encode())
        log("ShowDashboard(probe) called")

    end = time.time() + seconds
    while True:
        tw, th = C.c_uint32(0), C.c_uint32(0)
        tex_size(head, C.byref(tw), C.byref(th))
        log(f"state: head visible={visible(head)} tex={tw.value}x{th.value} world visible={visible(world)} "
            f"dash main visible={visible(dmain)} active={active_dash(dmain)} dashboard visible={dash_visible()}")
        if time.time() >= end:
            break
        time.sleep(min(5, max(0.1, end - time.time())))
    vr.VR_ShutdownInternal()
    try:
        os.remove(png)
    except OSError:
        pass
    log(f"done, failed calls: {failed or 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
