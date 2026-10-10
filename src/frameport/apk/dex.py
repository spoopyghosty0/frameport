"""Minimal in-place dex edits: find one method's bytecode and turn a call in it into no-ops, or make the method return
at once (same length), then fix the header's SHA-1 signature and Adler-32 checksum. Nothing moves, so the rest of
classes.dex stays byte-identical (no smali round trip)."""
from __future__ import annotations

import hashlib
import struct
import zlib

INVOKE_VIRTUAL, INVOKE_VIRTUAL_RANGE = 0x6E, 0x74  # 35c / 3rc: 3 code units, method index in the second
INVOKES = frozenset(range(0x6E, 0x73)) | frozenset(range(0x74, 0x79))  # invoke-virtual..interface(/range)
RETURN_VOID = 0x0E
CONST4_V0_0, RETURN_V0 = 0x0012, 0x000F  # const/4 v0, #0 ; return v0


def _widths() -> list[int]:
    """Code units of each Dalvik opcode (0 = unused/unknown)."""
    w = [0] * 256
    spans = [((0x00, 0x00), 1), ((0x01, 0x01), 1), ((0x02, 0x02), 2), ((0x03, 0x03), 3), ((0x04, 0x04), 1),
             ((0x05, 0x05), 2), ((0x06, 0x06), 3), ((0x07, 0x07), 1), ((0x08, 0x08), 2), ((0x09, 0x09), 3),
             ((0x0A, 0x12), 1), ((0x13, 0x13), 2), ((0x14, 0x14), 3), ((0x15, 0x16), 2), ((0x17, 0x17), 3),
             ((0x18, 0x18), 5), ((0x19, 0x1A), 2), ((0x1B, 0x1B), 3), ((0x1C, 0x1C), 2), ((0x1D, 0x1E), 1),
             ((0x1F, 0x20), 2), ((0x21, 0x21), 1), ((0x22, 0x23), 2), ((0x24, 0x26), 3), ((0x27, 0x28), 1),
             ((0x29, 0x29), 2), ((0x2A, 0x2C), 3), ((0x2D, 0x3D), 2), ((0x44, 0x6D), 2), ((0x6E, 0x72), 3),
             ((0x74, 0x78), 3), ((0x7B, 0x8F), 1), ((0x90, 0xAF), 2), ((0xB0, 0xCF), 1), ((0xD0, 0xE2), 2),
             ((0xFA, 0xFB), 4), ((0xFC, 0xFD), 3), ((0xFE, 0xFF), 2)]
    for (lo, hi), n in spans:
        for op in range(lo, hi + 1):
            w[op] = n
    return w


WIDTHS = _widths()


def _uleb(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7


class Dex:
    def __init__(self, data: bytes):
        if data[:4] != b"dex\n":
            raise ValueError("not a dex file")
        self.data = bytearray(data)
        h = struct.unpack_from("<8I", data, 56)  # string_ids size/off, type_ids, proto_ids, field_ids
        self.strings_n, self.strings_off, self.types_n, self.types_off = h[0], h[1], h[2], h[3]
        self.methods_n, self.methods_off, self.classes_n, self.classes_off = struct.unpack_from("<4I", data, 88)

    def string(self, idx: int) -> str:
        off = struct.unpack_from("<I", self.data, self.strings_off + 4 * idx)[0]
        _n, pos = _uleb(self.data, off)
        end = self.data.index(0, pos)
        return bytes(self.data[pos:end]).decode("utf-8", "replace")

    def type_name(self, idx: int) -> str:
        return self.string(struct.unpack_from("<I", self.data, self.types_off + 4 * idx)[0])

    def method(self, idx: int) -> tuple[str, str]:
        cls, _proto, name = struct.unpack_from("<HHI", self.data, self.methods_off + 8 * idx)
        return self.type_name(cls), self.string(name)

    def return_type(self, idx: int) -> str:
        proto = struct.unpack_from("<HHI", self.data, self.methods_off + 8 * idx)[1]
        protos_off = struct.unpack_from("<I", self.data, 76)[0]
        return self.type_name(struct.unpack_from("<I", self.data, protos_off + 12 * proto + 4)[0])

    def method_index(self, cls: str, name: str) -> list[int]:
        return [i for i in range(self.methods_n) if self.method(i) == (cls, name)]

    def code_items(self, cls: str, name: str) -> list[int]:
        """Offsets of the code items of the methods `name` in class `cls` (direct and virtual)."""
        return [code for _idx, code in self.class_methods(cls) if self.method(_idx)[1] == name]

    def class_methods(self, cls: str) -> list[tuple[int, int]]:
        """(method index, code item offset) of every method of class `cls` that has code."""
        out = []
        for c in range(self.classes_n):
            base = self.classes_off + 32 * c
            if self.type_name(struct.unpack_from("<I", self.data, base)[0]) != cls:
                continue
            class_data = struct.unpack_from("<I", self.data, base + 24)[0]
            if not class_data:
                continue
            pos = class_data
            sf, pos = _uleb(self.data, pos)
            inf, pos = _uleb(self.data, pos)
            dm, pos = _uleb(self.data, pos)
            vm, pos = _uleb(self.data, pos)
            for _ in range(sf + inf):
                _, pos = _uleb(self.data, pos)
                _, pos = _uleb(self.data, pos)
            for count in (dm, vm):
                idx = 0
                for _ in range(count):
                    diff, pos = _uleb(self.data, pos)
                    _, pos = _uleb(self.data, pos)
                    code, pos = _uleb(self.data, pos)
                    idx += diff
                    if code:
                        out.append((idx, code))
        return out

    def nop_calls(self, code_off: int, targets: set[int]) -> int:
        """Replace invoke-virtual(/range) calls of any method index in `targets` inside one code item with nops."""
        insns_n = struct.unpack_from("<I", self.data, code_off + 12)[0]
        start = code_off + 16
        units = struct.unpack_from(f"<{insns_n}H", self.data, start)
        done = 0
        for i in range(insns_n - 2):
            if units[i] & 0xFF in (INVOKE_VIRTUAL, INVOKE_VIRTUAL_RANGE) and units[i + 1] in targets:
                struct.pack_into("<3H", self.data, start + 2 * i, 0, 0, 0)
                done += 1
        return done

    def invoked(self, code_off: int) -> set[int]:
        """Method indices invoked (virtual/super/direct/static/interface, also /range) in one code item."""
        out: set[int] = set()
        for _pos, unit in self._insns(code_off):
            if unit[0] & 0xFF in INVOKES:
                out.add(unit[1])
        return out

    def _insns(self, code_off: int):
        """(position, code units) of each instruction, decoded in order; stops at payloads or unknown opcodes."""
        insns_n = struct.unpack_from("<I", self.data, code_off + 12)[0]
        units = struct.unpack_from(f"<{insns_n}H", self.data, code_off + 16)
        i = 0
        while i < insns_n:
            op = units[i] & 0xFF
            if op == 0 and units[i] >> 8:  # packed-switch / sparse-switch / fill-array-data payload
                return
            n = WIDTHS[op]
            if not n or i + n > insns_n:
                return
            yield i, units[i:i + n]
            i += n

    def return_void(self, code_off: int) -> bool:
        """Make a void method return at once (see return_early)."""
        return self.return_early(code_off, "V")

    def return_early(self, code_off: int, ret: str) -> bool:
        """Make a method return at once: its first instruction(s) become `return-void` (ret "V") or `const/4 v0, 0;
        return v0` (ret Z/B/S/C/I: false / 0), padded with nops to the next instruction boundary. The rest of the code
        stays as it was (unreachable). False if the method already starts that way, the return type isn't supported
        (wide, objects) or its first instructions can't be decoded."""
        if ret == "V":
            new = [RETURN_VOID]
        elif ret in ("Z", "B", "S", "C", "I"):
            if not struct.unpack_from("<H", self.data, code_off)[0]:  # registers_size: v0 must exist
                return False
            new = [CONST4_V0_0, RETURN_V0]
        else:
            return False
        start = code_off + 16
        insns_n = struct.unpack_from("<I", self.data, code_off + 12)[0]
        if list(struct.unpack_from(f"<{min(len(new), insns_n)}H", self.data, start)) == new:
            return False
        width = 0
        for pos, units in self._insns(code_off):
            width = pos + len(units)
            if width >= len(new):
                break
        if width < len(new):
            return False
        struct.pack_into(f"<{width}H", self.data, start, *new, *([0] * (width - len(new))))
        return True

    def finish(self) -> bytes:
        self.data[12:32] = hashlib.sha1(bytes(self.data[32:])).digest()
        struct.pack_into("<I", self.data, 8, zlib.adler32(bytes(self.data[12:])) & 0xFFFFFFFF)
        return bytes(self.data)
