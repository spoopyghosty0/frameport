"""frame.vivox_audio_route (GitHub #101, Green Hell VR): Vivox's AudioChangeListener calls Android 12 AudioManager
methods; the methods that call them return at once (in-place dex edit)."""
from __future__ import annotations

import struct

from frameport.apk.dex import Dex
from frameport.patches.frame.vivox_audio_route import LISTENER, VivoxAudioRoute, patch_dex
from frameport.validate.triage import triage


def _uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def build_dex(methods: list[tuple[str, str, str, list[int] | None]]) -> bytes:
    """A minimal dex: methods = (class, name, return type, code units or None for a referenced-only method). Every
    method with code belongs to one class_def per class (virtual methods)."""
    strings: list[str] = []

    def s(x: str) -> int:
        if x not in strings:
            strings.append(x)
        return strings.index(x)

    types: list[int] = []

    def t(x: str) -> int:
        i = s(x)
        if i not in types:
            types.append(i)
        return types.index(i)

    protos: list[tuple[int, int]] = []

    def p(ret: str) -> int:
        key = (s(ret if len(ret) == 1 else "L"), t(ret))
        if key not in protos:
            protos.append(key)
        return protos.index(key)

    mids = [(t(c), p(r), s(n)) for c, n, r, _ in methods]
    header = 0x70
    off = header
    sid_off = off
    off += 4 * len(strings)
    tid_off = off
    off += 4 * len(types)
    pid_off = off
    off += 12 * len(protos)
    mid_off = off
    off += 8 * len(mids)
    classes = sorted({c for c, _n, _r, code in methods if code is not None})
    cdef_off = off
    off += 32 * len(classes)
    data = bytearray()

    def place(blob: bytes, align: int = 1) -> int:
        while (off + len(data)) % align:
            data.append(0)
        pos = off + len(data)
        data.extend(blob)
        return pos

    str_offs = [place(_uleb(len(x)) + x.encode() + b"\0") for x in strings]
    code_offs = {}
    for i, (_c, _n, _r, code) in enumerate(methods):
        if code is not None:
            item = struct.pack("<4HII", 4, 1, 1, 0, 0, len(code)) + struct.pack(f"<{len(code)}H", *code)
            code_offs[i] = place(item, 4)
    class_data = {}
    for c in classes:
        idxs = [i for i, m in enumerate(methods) if m[0] == c and m[3] is not None]
        blob = _uleb(0) + _uleb(0) + _uleb(0) + _uleb(len(idxs))
        prev = 0
        for i in idxs:
            blob += _uleb(i - prev) + _uleb(1) + _uleb(code_offs[i])
            prev = i
        class_data[c] = place(blob)
    out = bytearray(header)
    out[:8] = b"dex\n035\0"
    struct.pack_into("<10I", out, 56, len(strings), sid_off, len(types), tid_off, len(protos), pid_off, 0, 0,
                     len(mids), mid_off)
    struct.pack_into("<2I", out, 96, len(classes), cdef_off)
    out += b"".join(struct.pack("<I", o) for o in str_offs)
    out += b"".join(struct.pack("<I", i) for i in types)
    out += b"".join(struct.pack("<3I", a, b, 0) for a, b in protos)
    out += b"".join(struct.pack("<HHI", *m) for m in mids)
    out += b"".join(struct.pack("<8I", t(c), 1, 0, 0, 0, 0, class_data[c], 0) for c in classes)
    out += data
    return bytes(out)


AM = "Landroid/media/AudioManager;"
# method indices: 0 getAvailableCommunicationDevices, 1 getCommunicationDevice (both referenced only)
CHECK = [0x011D, 0x1054, 0x0000, 0x106E, 0, 0x0000, 0x000C, 0x000E]  # monitor-enter; iget-object; invoke; move-result
SET = [0x1054, 0x0000, 0x106E, 0, 0x0000, 0x000A, 0x000F]           # iget-object; invoke; move-result v0; return v0
GET = [0x106E, 1, 0x0000, 0x000C, 0x0011]                          # invoke; move-result-object; return-object
OTHER = [0x000E]


def _dex() -> bytes:
    return build_dex([(AM, "getAvailableCommunicationDevices", "Ljava/util/List;", None),
                      (AM, "getCommunicationDevice", "Landroid/media/AudioDeviceInfo;", None),
                      (LISTENER, "checkAudioRouteAndApplyChanges", "V", CHECK),
                      (LISTENER, "setCommunicationDevice", "Z", SET),
                      (LISTENER, "getCommunicationDevice", "Lcom/vivox/sdk/jni/VxaRenderRoute;", GET),
                      (LISTENER, "setContext", "V", OTHER)])


def _code(dex: Dex, name: str) -> list[int]:
    off = dex.code_items(LISTENER, name)[0]
    n = struct.unpack_from("<I", dex.data, off + 12)[0]
    return list(struct.unpack_from(f"<{n}H", dex.data, off + 16))


def test_vivox_methods_return_early_in_place():
    raw = _dex()
    dex = Dex(raw)
    assert dex.return_type(2) == "V" and dex.return_type(3) == "Z"
    assert sorted(patch_dex(dex)) == ["checkAudioRouteAndApplyChanges", "setCommunicationDevice"]
    assert _code(dex, "checkAudioRouteAndApplyChanges") == [0x000E] + CHECK[1:]  # monitor-enter -> return-void
    assert _code(dex, "setCommunicationDevice") == [0x0012, 0x000F] + SET[2:]  # const/4 v0, 0; return v0
    assert _code(dex, "getCommunicationDevice") == GET  # object return: left alone
    assert _code(dex, "setContext") == OTHER
    out = dex.finish()
    assert len(out) == len(raw) and patch_dex(Dex(out)) == []  # same size; a second run changes nothing


def test_return_early_pads_to_an_instruction_boundary():
    dex = Dex(build_dex([(LISTENER, "f", "I", [0x0013, 0x0005, 0x000F])]))  # const/16 v0, 5; return v0
    assert dex.return_early(dex.code_items(LISTENER, "f")[0], "I") is True
    assert _code(dex, "f") == [0x0012, 0x000F, 0x000F]
    dex = Dex(build_dex([(LISTENER, "g", "V", [0x0018, 1, 2, 3, 4, 0x000E])]))  # const-wide (5 units)
    assert dex.return_early(dex.code_items(LISTENER, "g")[0], "V") is True
    assert _code(dex, "g") == [0x000E, 0, 0, 0, 0, 0x000E]
    assert dex.return_early(dex.code_items(LISTENER, "g")[0], "J") is False


def test_detect_and_triage():
    class A:
        extra = {"vivox_api31": True}
    assert VivoxAudioRoute().detect(A()).recommended
    A.extra = {}
    assert VivoxAudioRoute().detect(A()) is None
    log = ("10-08 21:00:01.000  900  950 E AndroidRuntime: FATAL EXCEPTION: main\n"
           "10-08 21:00:01.000  900  950 E AndroidRuntime: java.lang.NoSuchMethodError: No virtual method "
           "getAvailableCommunicationDevices()Ljava/util/List; in class Landroid/media/AudioManager; or its super "
           "classes (declaration of 'android.media.AudioManager' appears in /system/framework/framework.jar)\n")
    r = triage(log, "EXITED", "com.Incuvo.GreenHellVR")
    assert [f.id for f in r.findings] == ["vivox-api31"]
    assert r.findings[0].suggest == ["frame.vivox_audio_route"]
