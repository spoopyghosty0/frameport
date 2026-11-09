"""A small dex writer for helper classes FramePort generates at build time (e.g. frame.stable_android_id's settings
forwarder, which carries a per-game value in its code). It writes static fields and static methods only, with
hand-assembled Dalvik bytecode (`Asm`), and produces a complete, verifiable dex: sorted id tables, type lists, code
items with try/catch-all ranges, class data and the map list. No Java toolchain is needed at build time.

Bytecode refers to strings, types, fields and methods through the placeholders `S`, `T`, `F` and `M` (one 16-bit
code unit each); `build` collects them, sorts every table the way the dex format requires and fills the indices in.
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass, field

ACC_PUBLIC, ACC_PRIVATE, ACC_STATIC, ACC_FINAL = 0x1, 0x2, 0x8, 0x10
ACC_CONSTRUCTOR = 0x10000
NO_INDEX = 0xFFFFFFFF


@dataclass(frozen=True)
class S:  # string index
    value: str


@dataclass(frozen=True)
class T:  # type index (descriptor)
    descriptor: str


@dataclass(frozen=True)
class F:  # field index
    cls: str
    name: str
    type: str


@dataclass(frozen=True)
class M:  # method index
    cls: str
    name: str
    descriptor: str  # "(Ljava/lang/String;I)V"


def parse_descriptor(desc: str) -> tuple[list[str], str]:
    """(parameter types, return type) of a method descriptor."""
    if not desc.startswith("(") or ")" not in desc:
        raise ValueError(f"bad method descriptor {desc!r}")
    params, i = [], 1
    while desc[i] != ")":
        j = i
        while desc[j] == "[":
            j += 1
        j = desc.index(";", j) + 1 if desc[j] == "L" else j + 1
        params.append(desc[i:j])
        i = j
    return params, desc[i + 1:]


def words(types: list[str]) -> int:
    """Registers the values of these types take (long/double take two)."""
    return sum(2 if t in ("J", "D") else 1 for t in types)


def _shorty(params: list[str], ret: str) -> str:
    return "".join("L" if t[0] in "L[" else t for t in [ret, *params])


class Asm:
    """Dalvik bytecode with labels: emit units (ints or S/T/F/M placeholders), jump to labels, mark try ranges."""

    def __init__(self):
        self.units: list = []
        self.labels: dict[str, int] = {}
        self._fix: list[tuple[int, int, str, str]] = []  # (unit pos, instruction pos, label, kind)
        self.tries: list[tuple[str, str, str]] = []  # (start label, end label, catch-all label)

    def pos(self) -> int:
        return len(self.units)

    def label(self, name: str) -> None:
        self.labels[name] = self.pos()

    def op(self, *units) -> None:
        self.units.extend(units)

    # instructions used by FramePort's helpers
    def const4(self, a: int, value: int) -> None:
        self.op(((value & 0xF) << 12) | (a << 8) | 0x12)

    def const16(self, a: int, value: int) -> None:
        self.op((a << 8) | 0x13, value & 0xFFFF)

    def const_string(self, a: int, s: str) -> None:
        self.op((a << 8) | 0x1A, S(s))

    def const_class(self, a: int, t: str) -> None:
        self.op((a << 8) | 0x1C, T(t))

    def array_length(self, a: int, b: int) -> None:
        self.op((b << 12) | (a << 8) | 0x21)

    def new_array(self, a: int, size: int, t: str) -> None:
        self.op((size << 12) | (a << 8) | 0x23, T(t))

    def aget_object(self, a: int, array: int, index: int) -> None:
        self.op((a << 8) | 0x46, (index << 8) | array)

    def aput_object(self, a: int, array: int, index: int) -> None:
        self.op((a << 8) | 0x4D, (index << 8) | array)

    def sget(self, kind: str, a: int, f: F) -> None:
        ops = {"": 0x60, "wide": 0x61, "object": 0x62, "boolean": 0x63}
        self.op((a << 8) | ops[kind], f)

    def sput(self, kind: str, a: int, f: F) -> None:
        ops = {"": 0x67, "wide": 0x68, "object": 0x69, "boolean": 0x6A}
        self.op((a << 8) | ops[kind], f)

    def add_int_lit8(self, a: int, b: int, lit: int) -> None:
        self.op((a << 8) | 0xD8, ((lit & 0xFF) << 8) | b)

    def invoke(self, kind: str, m: M, regs: list[int]) -> None:
        """invoke-virtual / invoke-static with up to 5 argument registers (each < 16)."""
        op = {"virtual": 0x6E, "static": 0x71}[kind]
        if len(regs) > 5 or any(r > 15 for r in regs):
            raise ValueError("use invoke_range")
        r = regs + [0] * (5 - len(regs))
        self.op((len(regs) << 12) | (r[4] << 8) | op, m, (r[3] << 12) | (r[2] << 8) | (r[1] << 4) | r[0])

    def invoke_static_range(self, m: M, first: int, count: int) -> None:
        self.op((count << 8) | 0x77, m, first)

    def move_result(self, kind: str, a: int) -> None:
        self.op((a << 8) | {"": 0x0A, "wide": 0x0B, "object": 0x0C}[kind])

    def ret(self, kind: str, a: int = 0) -> None:
        if kind == "void":
            self.op(0x0E)
        else:
            self.op((a << 8) | {"": 0x0F, "wide": 0x10, "object": 0x11}[kind])

    def if_z(self, test: str, a: int, target: str) -> None:
        here = self.pos()
        self.op((a << 8) | {"eqz": 0x38, "nez": 0x39}[test], 0)
        self._fix.append((here + 1, here, target, "16"))

    def if_ge(self, a: int, b: int, target: str) -> None:
        here = self.pos()
        self.op((b << 12) | (a << 8) | 0x35, 0)
        self._fix.append((here + 1, here, target, "16"))

    def goto(self, target: str) -> None:
        here = self.pos()
        self.op(0x29, 0)  # goto/16: no 8-bit range limit
        self._fix.append((here + 1, here, target, "16"))

    def try_range(self, start: str, end: str, catch_all: str) -> None:
        self.tries.append((start, end, catch_all))

    def finish(self) -> tuple[list, list[tuple[int, int, int]]]:
        """(units, tries as (start, count, catch-all address)) with every label resolved."""
        units = list(self.units)
        for at, ins, target, _kind in self._fix:
            units[at] = (self.labels[target] - ins) & 0xFFFF
        tries = [(self.labels[a], self.labels[b] - self.labels[a], self.labels[c]) for a, b, c in self.tries]
        return units, tries


@dataclass
class Method:
    name: str
    descriptor: str
    access: int
    registers: int
    outs: int
    code: Asm

    @property
    def ins(self) -> int:
        params, _ret = parse_descriptor(self.descriptor)
        return words(params) + (0 if self.access & ACC_STATIC else 1)


@dataclass
class ClassDef:
    name: str  # descriptor, "Lcom/example/Foo;"
    access: int = ACC_PUBLIC | ACC_FINAL
    superclass: str = "Ljava/lang/Object;"
    static_fields: list[tuple[str, str, int]] = field(default_factory=list)  # (name, type, access)
    methods: list[Method] = field(default_factory=list)  # direct methods (static, <clinit>)


def _uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _mutf8(s: str) -> bytes:
    """Modified UTF-8: each UTF-16 code unit on its own, NUL as two bytes."""
    out = bytearray()
    for unit in _utf16_key(s):
        if 0 < unit < 0x80:
            out.append(unit)
        elif unit < 0x800:
            out += bytes((0xC0 | unit >> 6, 0x80 | unit & 0x3F))
        else:
            out += bytes((0xE0 | unit >> 12, 0x80 | (unit >> 6) & 0x3F, 0x80 | unit & 0x3F))
    return bytes(out)


def _utf16_key(s: str) -> list[int]:
    data = s.encode("utf-16-be")
    return list(struct.unpack(f">{len(data) // 2}H", data))


def build(classes: list[ClassDef]) -> bytes:
    """A dex file with these classes (each one's superclass must be outside the file or come before it)."""
    strings: set[str] = set()
    types: set[str] = set()
    protos: set[tuple[str, tuple[str, ...]]] = set()
    fields: set[F] = set()
    methods: set[M] = set()

    def add_type(t: str) -> None:
        types.add(t)
        strings.add(t)

    def add_proto(desc: str) -> tuple[str, tuple[str, ...]]:
        params, ret = parse_descriptor(desc)
        for t in [ret, *params]:
            add_type(t)
        strings.add(_shorty(params, ret))
        key = (ret, tuple(params))
        protos.add(key)
        return key

    def add_ref(ref) -> None:
        if isinstance(ref, S):
            strings.add(ref.value)
        elif isinstance(ref, T):
            add_type(ref.descriptor)
        elif isinstance(ref, F):
            fields.add(ref)
            add_type(ref.cls)
            add_type(ref.type)
            strings.add(ref.name)
        elif isinstance(ref, M):
            methods.add(ref)
            add_type(ref.cls)
            strings.add(ref.name)
            add_proto(ref.descriptor)

    assembled = {}
    for c in classes:
        add_type(c.name)
        add_type(c.superclass)
        for name, typ, _acc in c.static_fields:
            add_ref(F(c.name, name, typ))
        for m in c.methods:
            add_ref(M(c.name, m.name, m.descriptor))
            units, tries = m.code.finish()
            for u in units:
                if not isinstance(u, int):
                    add_ref(u)
            assembled[(c.name, m.name, m.descriptor)] = (units, tries)

    string_list = sorted(strings, key=_utf16_key)
    sidx = {s: i for i, s in enumerate(string_list)}
    type_list = sorted(types, key=lambda t: sidx[t])
    tidx = {t: i for i, t in enumerate(type_list)}
    proto_list = sorted(protos, key=lambda p: (tidx[p[0]], [tidx[t] for t in p[1]]))
    pidx = {p: i for i, p in enumerate(proto_list)}
    field_list = sorted(fields, key=lambda f: (tidx[f.cls], sidx[f.name], tidx[f.type]))
    fidx = {f: i for i, f in enumerate(field_list)}

    def proto_of(desc: str) -> int:
        params, ret = parse_descriptor(desc)
        return pidx[(ret, tuple(params))]

    method_list = sorted(methods, key=lambda m: (tidx[m.cls], sidx[m.name], proto_of(m.descriptor)))
    midx = {m: i for i, m in enumerate(method_list)}

    def resolve(u) -> int:
        if isinstance(u, int):
            return u
        if isinstance(u, S):
            return sidx[u.value]
        if isinstance(u, T):
            return tidx[u.descriptor]
        if isinstance(u, F):
            return fidx[u]
        return midx[u]

    header = 0x70
    off = header
    sid_off = off
    off += 4 * len(string_list)
    tid_off = off
    off += 4 * len(type_list)
    pid_off = off
    off += 12 * len(proto_list)
    fid_off = off
    off += 8 * len(field_list)
    mid_off = off
    off += 8 * len(method_list)
    cdef_off = off
    off += 32 * len(classes)
    data_off = off
    data = bytearray()
    sections: list[tuple[int, int, int]] = []  # (map type, count, offset)

    def here() -> int:
        return data_off + len(data)

    def align(n: int) -> None:
        while here() % n:
            data.append(0)

    # type lists (proto parameters)
    align(4)
    tl_start, tl_offs = here(), {}
    for _ret, params in proto_list:
        if params and params not in tl_offs:
            align(4)
            tl_offs[params] = here()
            data.extend(struct.pack("<I", len(params)) + b"".join(struct.pack("<H", tidx[t]) for t in params))
    if tl_offs:
        sections.append((0x1001, len(tl_offs), tl_start))

    # code items
    align(4)
    code_start, code_offs = here(), {}
    for c in classes:
        for m in c.methods:
            units, tries = assembled[(c.name, m.name, m.descriptor)]
            align(4)
            code_offs[(c.name, m.name, m.descriptor)] = here()
            insns = [resolve(u) for u in units]
            data.extend(struct.pack("<4HII", m.registers, m.ins, m.outs, len(tries), 0, len(insns)))
            data.extend(struct.pack(f"<{len(insns)}H", *insns))
            if tries:
                if len(insns) % 2:
                    data.extend(b"\0\0")
                handlers = bytearray(_uleb(len(tries)))
                handler_offs = []
                for _s, _n, catch_all in tries:
                    handler_offs.append(len(handlers))
                    handlers += b"\0" + _uleb(catch_all)  # size 0 (sleb128): only a catch-all
                for (start, count, _c), h in zip(tries, handler_offs, strict=True):
                    data.extend(struct.pack("<IHH", start, count, h))
                data.extend(handlers)
    if code_offs:
        sections.append((0x2001, len(code_offs), code_start))

    # string data
    str_start, str_offs = here(), []
    for s in string_list:
        str_offs.append(here())
        data.extend(_uleb(len(_utf16_key(s))) + _mutf8(s) + b"\0")
    sections.append((0x2002, len(string_list), str_start))

    # class data
    cd_start, cd_offs = here(), {}
    for c in classes:
        cd_offs[c.name] = here()
        sfields = sorted((fidx[F(c.name, n, t)], acc) for n, t, acc in c.static_fields)
        dmethods = sorted((midx[M(c.name, m.name, m.descriptor)], m.access,
                           code_offs[(c.name, m.name, m.descriptor)]) for m in c.methods)
        blob = bytearray(_uleb(len(sfields)) + _uleb(0) + _uleb(len(dmethods)) + _uleb(0))
        prev = 0
        for i, acc in sfields:
            blob += _uleb(i - prev) + _uleb(acc)
            prev = i
        prev = 0
        for i, acc, code in dmethods:
            blob += _uleb(i - prev) + _uleb(acc) + _uleb(code)
            prev = i
        data.extend(blob)
    sections.append((0x2000, len(classes), cd_start))

    # map list
    align(4)
    map_off = here()
    entries = [(0x0000, 1, 0), (0x0001, len(string_list), sid_off), (0x0002, len(type_list), tid_off),
               (0x0003, len(proto_list), pid_off), (0x0004, len(field_list), fid_off),
               (0x0005, len(method_list), mid_off), (0x0006, len(classes), cdef_off)]
    entries = [e for e in entries if e[1]] + sections + [(0x1000, 1, map_off)]
    entries.sort(key=lambda e: e[2])
    data.extend(struct.pack("<I", len(entries)))
    for typ, count, at in entries:
        data.extend(struct.pack("<HHII", typ, 0, count, at))

    out = bytearray(header)
    out[:8] = b"dex\n035\0"
    file_size = data_off + len(data)

    def sized(n: int, at: int) -> tuple[int, int]:
        return n, at if n else 0

    struct.pack_into("<IIIIII", out, 32, file_size, header, 0x12345678, 0, 0, map_off)
    struct.pack_into("<12I", out, 56, *sized(len(string_list), sid_off), *sized(len(type_list), tid_off),
                     *sized(len(proto_list), pid_off), *sized(len(field_list), fid_off),
                     *sized(len(method_list), mid_off), *sized(len(classes), cdef_off))
    struct.pack_into("<II", out, 104, len(data), data_off)
    out += b"".join(struct.pack("<I", o) for o in str_offs)
    out += b"".join(struct.pack("<I", sidx[t]) for t in type_list)
    for ret, params in proto_list:
        out += struct.pack("<III", sidx[_shorty(list(params), ret)], tidx[ret], tl_offs.get(params, 0))
    out += b"".join(struct.pack("<HHI", tidx[f.cls], tidx[f.type], sidx[f.name]) for f in field_list)
    out += b"".join(struct.pack("<HHI", tidx[m.cls], proto_of(m.descriptor), sidx[m.name]) for m in method_list)
    for c in classes:
        out += struct.pack("<8I", tidx[c.name], c.access, tidx[c.superclass], 0, NO_INDEX, 0, cd_offs[c.name], 0)
    out += data
    out[12:32] = hashlib.sha1(bytes(out[32:])).digest()
    struct.pack_into("<I", out, 8, zlib.adler32(bytes(out[12:])) & 0xFFFFFFFF)
    return bytes(out)
