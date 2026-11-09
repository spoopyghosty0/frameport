"""Minimal Android binary XML (AndroidManifest.xml) reader/editor.

Edits are surgical (string pool append, attribute retarget, element insert, boolean flip) so that the rest of the
file stays byte-identical, which keeps diffs against known-good builds meaningful.
"""
from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

RES_XML = 0x0003
RES_STRING_POOL = 0x0001
RES_XML_START_ELEMENT = 0x0102
RES_XML_END_ELEMENT = 0x0103
TYPE_STRING = 0x03
TYPE_INT_BOOLEAN = 0x12
TYPE_REFERENCE = 0x01
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
ANDROID_NS = "http://schemas.android.com/apk/res/android"
LAUNCHER = "android.intent.category.LAUNCHER"
INFO = "android.intent.category.INFO"
LEANBACK_LAUNCHER = "android.intent.category.LEANBACK_LAUNCHER"  # Android TV's launcher (apps made for a gamepad)
MAIN = "android.intent.action.MAIN"
DEFAULT = "android.intent.category.DEFAULT"
VR_CATEGORY = "com.oculus.intent.category.VR"
COMPONENTS = ("activity", "activity-alias", "service", "receiver", "provider")


@dataclass
class Attr:
    offset: int  # absolute offset of the attribute record
    ns: int
    name: int
    raw: int
    dtype: int
    value: int


@dataclass
class Element:
    offset: int  # chunk offset
    size: int
    name: str
    attrs: list[Attr]


class Axml:
    def __init__(self, data: bytes):
        self.data = bytearray(data)
        typ, hsz, _ = struct.unpack_from("<HHI", self.data, 0)
        if typ != RES_XML:
            raise ValueError("not a binary XML file")
        self.pool_off = hsz
        ptype, phsz, psize, self.count, self.styles, self.flags, self.sstart, self.ystart = struct.unpack_from(
            "<HHIIIIII", self.data, self.pool_off
        )
        if ptype != RES_STRING_POOL:
            raise ValueError("string pool not found")
        self.pool_hsz, self.pool_size = phsz, psize
        self.utf8 = bool(self.flags & 0x100)
        base = self.pool_off + phsz
        self.offsets = list(struct.unpack_from(f"<{self.count}I", self.data, base))
        self.style_offsets = list(struct.unpack_from(f"<{self.styles}I", self.data, base + 4 * self.count))
        self._strings: list[str] | None = None

    # ------------------------------------------------------------------ strings
    def string(self, i: int) -> str:
        p = self.pool_off + self.sstart + self.offsets[i]
        d = self.data
        if self.utf8:
            n = d[p]
            p += 2 if n & 0x80 else 1  # char count
            n = d[p]
            nb = ((n & 0x7F) << 8 | d[p + 1]) if n & 0x80 else n
            p += 2 if n & 0x80 else 1
            return bytes(d[p : p + nb]).decode("utf-8", "replace")
        n = struct.unpack_from("<H", d, p)[0]
        p += 2
        if n & 0x8000:
            n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", d, p)[0]
            p += 2
        return bytes(d[p : p + 2 * n]).decode("utf-16-le", "replace")

    def strings(self) -> list[str]:
        if self._strings is None:
            self._strings = [self.string(i) for i in range(self.count)]
        return self._strings

    def append_string(self, s: str) -> int:
        """Append s to the pool; returns its index. Rebuilds the pool chunk and the file size."""
        if self.utf8:
            b = s.encode("utf-8")
            if len(s) >= 0x80 or len(b) >= 0x80:
                raise ValueError("long UTF-8 strings are not supported")
            enc = bytes([len(s), len(b)]) + b + b"\0"
        else:
            enc = struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0"
        d, po = self.data, self.pool_off
        str_end = po + (self.ystart if self.styles else self.pool_size)
        str_data = bytes(d[po + self.sstart : str_end])
        new_off = len(str_data)
        str_data += enc
        str_data += b"\0" * (-len(str_data) % 4)
        style_data = bytes(d[po + self.ystart : po + self.pool_size]) if self.styles else b""
        offsets = self.offsets + [new_off]
        sstart = self.pool_hsz + 4 * len(offsets) + 4 * self.styles
        ystart = sstart + len(str_data) if self.styles else 0
        body = (
            struct.pack(f"<{len(offsets)}I", *offsets)
            + struct.pack(f"<{self.styles}I", *self.style_offsets)
            + str_data
            + style_data
        )
        header = struct.pack(
            "<HHIIIIII", RES_STRING_POOL, self.pool_hsz, self.pool_hsz + len(body), len(offsets), self.styles,
            self.flags, sstart, ystart,
        )
        header += bytes(d[po + 28 : po + self.pool_hsz])  # any extra header bytes
        new = bytes(d[:po]) + header + body + bytes(d[po + self.pool_size :])
        self.__init__(new)
        struct.pack_into("<I", self.data, 4, len(self.data))
        return len(offsets) - 1

    def index(self, s: str) -> int | None:
        try:
            return self.strings().index(s)
        except ValueError:
            return None

    # ------------------------------------------------------------------ elements
    def elements(self) -> Iterator[Element]:
        names = self.strings()
        pos = struct.unpack_from("<H", self.data, 2)[0]
        while pos < len(self.data):
            typ, hsz, size = struct.unpack_from("<HHI", self.data, pos)
            if size == 0:
                break
            if typ == RES_XML_START_ELEMENT:
                ext = pos + hsz
                _, name, astart, asize, acount = struct.unpack_from("<IIHHH", self.data, ext)
                attrs = []
                for a in range(acount):
                    at = ext + astart + a * asize
                    ns, aname, raw = struct.unpack_from("<III", self.data, at)
                    dtype = self.data[at + 15]
                    value = struct.unpack_from("<I", self.data, at + 16)[0]
                    attrs.append(Attr(at, ns, aname, raw, dtype, value))
                yield Element(pos, size, names[name] if name < len(names) else "", attrs)
            pos += size

    def attr_str(self, el: Element, attr: str) -> str | None:
        names = self.strings()
        for a in el.attrs:
            if a.name < len(names) and names[a.name] == attr:
                if a.raw < len(names):
                    return names[a.raw]
                if a.dtype == TYPE_STRING and a.value < len(names):
                    return names[a.value]
        return None

    # ------------------------------------------------------------------ edits
    def retarget(self, element: str, old_index: int, new_index: int) -> int:
        """In every <element>, point string-valued attributes that reference old_index at new_index."""
        hits = 0
        for el in self.elements():
            if el.name != element:
                continue
            for a in el.attrs:
                if a.raw == old_index or (a.dtype == TYPE_STRING and a.value == old_index):
                    struct.pack_into("<I", self.data, a.offset + 8, new_index)
                    if a.dtype == TYPE_STRING:
                        struct.pack_into("<I", self.data, a.offset + 16, new_index)
                    hits += 1
        return hits

    def set_bool(self, element: str, attr: str, value: bool) -> int:
        names = self.strings()
        hits = 0
        for el in self.elements():
            if el.name != element:
                continue
            for a in el.attrs:
                if a.name < len(names) and names[a.name] == attr and a.dtype == TYPE_INT_BOOLEAN:
                    struct.pack_into("<I", self.data, a.offset + 16, 0xFFFFFFFF if value else 0)
                    hits += 1
        return hits

    def get_bool(self, element: str, attr: str) -> bool | None:
        names = self.strings()
        for el in self.elements():
            if el.name != element:
                continue
            for a in el.attrs:
                if a.name < len(names) and names[a.name] == attr and a.dtype == TYPE_INT_BOOLEAN:
                    return a.value != 0
        return None

    def bytes(self) -> bytes:
        return bytes(self.data)


# ---------------------------------------------------------------------- high-level queries/fixes
def copy_activity_label_to_application(manifest: bytes) -> bytes | None:
    """Fill a missing application label from an activity, retaining its resource/string value (#269).

    OVRPort's appName() assumes the application has a label; Android permits activity-only labels. Existing
    application labels and the activity itself are untouched. No strings or resource ids are added.
    """
    x = Axml(manifest)
    names = x.strings()

    def label(el):
        return next((a for a in el.attrs if a.ns < len(names) and a.name < len(names) and names[a.ns] == ANDROID_NS
                     and names[a.name] == "label"), None)

    elements = list(x.elements())
    app = next((el for el in elements if el.name == "application"), None)
    if app is None or label(app) is not None:
        return None
    source = next((a for el in elements if el.name in ("activity", "activity-alias")
                   and (a := label(el)) is not None), None)
    if source is None:
        return None
    # Attributes are sorted by resource id in Android's binary XML. Reuse the existing resource map.
    ids = {}
    pos = x.pool_off + x.pool_size
    while pos < app.offset:
        typ, hsz, size = struct.unpack_from("<HHI", x.data, pos)
        if size < hsz or size == 0:
            raise ValueError("invalid binary XML chunk")
        if typ == 0x0180:  # RES_XML_RESOURCE_MAP
            ids = dict(enumerate(struct.unpack_from(f"<{(size - hsz) // 4}I", x.data, pos + hsz)))
            break
        pos += size
    ext = app.offset + struct.unpack_from("<H", x.data, app.offset + 2)[0]
    start, stride, count = struct.unpack_from("<HHH", x.data, ext + 8)
    index = next((i for i, a in enumerate(app.attrs)
                  if ids.get(a.name, 0xFFFFFFFF) > ids.get(source.name, 0xFFFFFFFF)), count)
    at = ext + start + index * stride
    record = x.data[source.offset:source.offset + 20] + bytes(stride - 20)
    data = x.data[:at] + record + x.data[at:]
    struct.pack_into("<I", data, app.offset + 4, app.size + stride)
    struct.pack_into("<H", data, ext + 12, count + 1)
    for offset in (14, 16, 18):  # one-based id/class/style attribute indices
        value = struct.unpack_from("<H", data, ext + offset)[0]
        if value > index:
            struct.pack_into("<H", data, ext + offset, value + 1)
    struct.pack_into("<I", data, 4, len(data))
    return bytes(data)


def used_and_declared_permissions(manifest: bytes) -> tuple[list[str], set[str]]:
    x = Axml(manifest)
    used, declared = [], set()
    for el in x.elements():
        if el.name in ("uses-permission", "permission"):
            name = x.attr_str(el, "name")
            if name:
                (used.append if el.name == "uses-permission" else declared.add)(name)
    return used, declared


def undeclared_meta_permissions(manifest: bytes) -> list[str]:
    used, declared = used_and_declared_permissions(manifest)
    return [p for p in used if p.startswith(("com.oculus.permission.", "horizonos.permission.")) and p not in declared]


def min_sdk(manifest: bytes) -> int | None:
    """<uses-sdk android:minSdkVersion> (None when absent or a preview codename)."""
    x = Axml(manifest)
    names = x.strings()
    for el in x.elements():
        if el.name != "uses-sdk":
            continue
        for a in el.attrs:
            if a.name < len(names) and names[a.name] == "minSdkVersion":
                if a.dtype in (TYPE_INT_DEC, TYPE_INT_HEX):
                    return a.value
                s = x.attr_str(el, "minSdkVersion")
                return int(s) if s and s.isdigit() else None
    return None


def meta_data(manifest: bytes) -> dict[str, str | int | bool | None]:
    """<meta-data android:name android:value|resource> entries: a string, a bool, an int, or a resource id (int)
    for references (@string/…), None when the value isn't one of those."""
    x = Axml(manifest)
    names = x.strings()
    out: dict = {}
    for el in x.elements():
        if el.name != "meta-data":
            continue
        key = x.attr_str(el, "name")
        if not key:
            continue
        value = None
        for a in el.attrs:
            if a.name >= len(names) or names[a.name] not in ("value", "resource"):
                continue
            if a.raw < len(names) or a.dtype == TYPE_STRING:
                value = x.attr_str(el, names[a.name])
            elif a.dtype == TYPE_INT_BOOLEAN:
                value = a.value != 0
            elif a.dtype in (TYPE_INT_DEC, TYPE_INT_HEX, TYPE_REFERENCE):
                value = a.value
        out[key] = value
    return out


def set_meta_data_bool(manifest: bytes, name_suffix: str, value: bool) -> bytes | None:
    """Set the boolean android:value of every <meta-data> whose android:name ends with `name_suffix` (one 4-byte
    value per element, nothing else moves). None when no such boolean entry differs from `value`. A string value
    ("true") is left alone: Android's Bundle.getBoolean reads only real booleans."""
    x = Axml(manifest)
    names = x.strings()
    hits = 0
    for el in x.elements():
        if el.name != "meta-data" or not (x.attr_str(el, "name") or "").endswith(name_suffix):
            continue
        for a in el.attrs:
            if (a.name < len(names) and names[a.name] == "value" and a.dtype == TYPE_INT_BOOLEAN
                    and (a.value != 0) != value):
                struct.pack_into("<I", x.data, a.offset + 16, 0xFFFFFFFF if value else 0)
                hits += 1
    return x.bytes() if hits else None


def categories(manifest: bytes) -> set[str]:
    x = Axml(manifest)
    return {v for el in x.elements() if el.name == "category" for v in [x.attr_str(el, "name")] if v}


def fix_launcher(manifest: bytes) -> bytes | None:
    """Lepton only launches activities with category LAUNCHER: retarget category INFO to LAUNCHER."""
    x = Axml(manifest)
    names = x.strings()
    if INFO not in names or LAUNCHER in names:
        return None
    info = names.index(INFO)
    launcher = x.append_string(LAUNCHER)
    return x.bytes() if x.retarget("category", info, launcher) else None


def component_filters(manifest: bytes) -> list[tuple[str, str, list[tuple[str, int, int]]]]:
    """[(element, component name, [(element, attr offset, string index)])] for each component's intent-filter
    children (action/category), in file order. Children follow their component's start element, so the last
    component seen owns them."""
    x = Axml(manifest)
    names = x.strings()
    out: list = []
    for el in x.elements():
        if el.name in COMPONENTS:
            out.append((el.name, x.attr_str(el, "name") or "", []))
        elif el.name in ("action", "category") and out:
            for a in el.attrs:
                if a.name < len(names) and names[a.name] == "name":
                    idx = a.raw if a.raw < len(names) else a.value
                    out[-1][2].append((el.name, a.offset, idx))
    return out


def vr_activity(manifest: bytes) -> str | None:
    """The activity with Meta's VR category when it isn't the start activity (a 2D launcher that starts the VR
    part, e.g. WiiCompiled's LauncherActivity -> QuestActivity). The start activity is the LAUNCHER one, else the
    INFO one (Quest store builds often give their only activity MAIN + INFO + VR, GitHub #476), else the first MAIN."""
    x = Axml(manifest)
    names = x.strings()
    vr = None
    start = {}  # category -> first activity
    for el, name, items in component_filters(manifest):
        cats = {names[i] for kind, _, i in items if kind == "category" and i < len(names)}
        if el == "activity" and VR_CATEGORY in cats:
            vr = vr or name
        for cat in (LAUNCHER, INFO):
            if cat in cats:
                start.setdefault(cat, name)
        if el == "activity" and any(k == "action" and i < len(names) and names[i] == MAIN for k, _, i in items):
            start.setdefault(MAIN, name)
    launcher = start.get(LAUNCHER) or start.get(INFO) or start.get(MAIN)
    return vr if vr and vr != launcher else None


def _package(x: Axml) -> str | None:
    return next((x.attr_str(el, "package") for el in x.elements() if el.name == "manifest"), None)


def separate_process_activities(manifest: bytes) -> list[str]:
    """Activities with a MAIN intent-filter that run in another process than the app's main one (android:process
    ":name" or another package's name). Lepton only watches the main process: started directly, such an activity
    leaves none, and Lepton closes the app about a minute later ("Target app ... did not start", GitHub #178)."""
    x = Axml(manifest)
    package = _package(x)
    process = {}
    for el in x.elements():
        if el.name == "activity":
            process[x.attr_str(el, "name") or ""] = x.attr_str(el, "process")
    names = x.strings()
    out = []
    for el, name, items in component_filters(manifest):
        proc = process.get(name) if el == "activity" else None
        if proc and proc != package and any(k == "action" and i < len(names) and names[i] == MAIN
                                            for k, _, i in items):
            out.append(name)
    return out


def set_main_process(manifest: bytes, activities: list[str]) -> bytes | None:
    """Run `activities` in the app's main process: their android:process becomes the package name (only those
    elements' attribute values change). None when there is nothing to change."""
    x = Axml(manifest)
    package = _package(x)
    if not package:
        return None
    hits = [(x.attr_str(el, "name"), el) for el in x.elements() if el.name == "activity"]
    if not any(n in activities and x.attr_str(el, "process") not in (None, package) for n, el in hits):
        return None
    index = x.index(package)
    if index is None:
        index = x.append_string(package)
    names = x.strings()
    for el in x.elements():  # again: append_string moved the elements
        if el.name != "activity" or x.attr_str(el, "name") not in activities:
            continue
        for a in el.attrs:
            if a.name < len(names) and names[a.name] == "process":
                _set_string_attr(x, a.offset, index)
    return x.bytes()


def set_start_activity(manifest: bytes, activity: str) -> bytes | None:
    """Make `activity` the one Lepton starts: its MAIN intent-filter gets category LAUNCHER (in place of its VR or
    DEFAULT category), every other component's LAUNCHER category becomes INFO. Only those attribute values change
    (per element, not every use of the string). None when `activity` has no MAIN filter or already is the only
    launcher."""
    filters = component_filters(manifest)
    x = Axml(manifest)
    names = x.strings()
    short = activity.rsplit(".", 1)[-1]
    target = next((f for f in filters if f[0] in ("activity", "activity-alias")
                   and (f[1] == activity or f[1].rsplit(".", 1)[-1] == short)), None)
    if target is None or not any(k == "action" and names[i] == MAIN for k, _, i in target[2] if i < len(names)):
        return None
    cats = [(off, names[i]) for k, off, i in target[2] if k == "category" and i < len(names)]
    others = [off for f in filters if f is not target for k, off, i in f[2]
              if k == "category" and i < len(names) and names[i] == LAUNCHER]
    if any(c == LAUNCHER for _, c in cats) and not others:
        return None
    pick = next((off for off, c in cats if c == VR_CATEGORY), None) or next(
        (off for off, c in cats if c == DEFAULT), None) or next((off for off, c in cats if c != LAUNCHER), None)
    if pick is None and not any(c == LAUNCHER for _, c in cats):
        return None

    def index(s: str) -> int:
        i = x.index(s)
        return i if i is not None else x.append_string(s)
    launcher, info = index(LAUNCHER), index(INFO)
    # append_string rebuilds the pool: offsets of attribute records move by the same amount for all of them
    shift = len(x.data) - len(manifest)
    for off in others:
        _set_string_attr(x, off + shift, info)
    if pick is not None and not any(c == LAUNCHER for _, c in cats):
        _set_string_attr(x, pick + shift, launcher)
    return x.bytes()


def launcher_on_activity(manifest: bytes) -> bool:
    """A real <activity> (not only an <activity-alias>) has category LAUNCHER: Lepton only starts activities."""
    names = Axml(manifest).strings()
    return any(el == "activity" and any(k == "category" and i < len(names) and names[i] == LAUNCHER
                                        for k, _, i in items) for el, _, items in component_filters(manifest))


def launcher_from_alias(manifest: bytes) -> bytes | None:
    """When the only LAUNCHER entry is an <activity-alias> (e.g. Flycast's MainActivity -> NativeGLActivity, GitHub
    #400), Lepton finds no activity to start ("APP_ACTIVITY is empty"). The alias's target activity gets its own
    MAIN intent-filter with the alias's categories (inserted right after the activity's start element), and the
    alias's LAUNCHER becomes INFO so there is one launcher. None when there's nothing to do or the target isn't
    declared in the manifest."""
    if launcher_on_activity(manifest):
        return None
    x = Axml(manifest)
    names = x.strings()
    alias = next((f for f in component_filters(manifest) if f[0] == "activity-alias"
                  and any(k == "category" and i < len(names) and names[i] == LAUNCHER for k, _, i in f[2])
                  and any(k == "action" and i < len(names) and names[i] == MAIN for k, _, i in f[2])), None)
    if alias is None:
        return None
    alias_el = next(el for el in x.elements() if el.name == "activity-alias" and x.attr_str(el, "name") == alias[1])
    target = x.attr_str(alias_el, "targetActivity") or ""
    package = _package(x) or ""
    full = package + target if target.startswith(".") else target
    activity = next((el for el in x.elements() if el.name == "activity"
                     and x.attr_str(el, "name") in (target, full)), None)
    child = next((el for el in x.elements() if el.name in ("action", "category") and el.attrs), None)
    filter_name = x.index("intent-filter")
    if activity is None or child is None or filter_name is None:
        return None
    name_attr = next(a for a in child.attrs if a.name < len(names) and names[a.name] == "name")
    action_el, category_el = x.index("action"), x.index("category")
    cats = [i for k, _, i in alias[2] if k == "category" and i < len(names)]
    # the MAIN filter's categories only (an alias can have a VIEW filter too): those up to the first non-MAIN action
    main_cats, seen_main = [], False
    for k, _, i in alias[2]:
        if k == "action":
            if seen_main and names[i] != MAIN:
                break
            seen_main = seen_main or names[i] == MAIN
        elif seen_main and i in cats:
            main_cats.append(i)

    def element(name: int, value: int | None) -> bytes:
        count = 0 if value is None else 1
        start = struct.pack("<HHIII", RES_XML_START_ELEMENT, 16, 36 + 20 * count, 0, 0xFFFFFFFF)
        start += struct.pack("<IIHHHHHH", 0xFFFFFFFF, name, 20, 20, count, 0, 0, 0)
        if value is not None:
            start += struct.pack("<IIIHBBI", name_attr.ns, name_attr.name, value, 8, 0, TYPE_STRING, value)
        return start

    def end(name: int) -> bytes:
        return struct.pack("<HHIII", RES_XML_END_ELEMENT, 16, 24, 0, 0xFFFFFFFF) + struct.pack("<II", 0xFFFFFFFF,
                                                                                                name)
    chunk = element(filter_name, None)
    chunk += element(action_el, names.index(MAIN)) + end(action_el)
    for c in main_cats:
        chunk += element(category_el, c) + end(category_el)
    chunk += end(filter_name)
    info = x.index(INFO)
    if info is None:
        info = x.append_string(INFO)
    shift = len(x.data) - len(manifest)
    for k, off, i in alias[2]:
        if k == "category" and i < len(names) and names[i] == LAUNCHER:
            _set_string_attr(x, off + shift, info)
    activity = next(el for el in x.elements() if el.name == "activity"
                    and x.attr_str(el, "name") in (target, full))
    at = activity.offset + activity.size
    data = x.data[:at] + chunk + x.data[at:]
    struct.pack_into("<I", data, 4, len(data))
    return bytes(data)


def _set_string_attr(x: Axml, offset: int, index: int) -> None:
    struct.pack_into("<I", x.data, offset + 8, index)
    if x.data[offset + 15] == TYPE_STRING:
        struct.pack_into("<I", x.data, offset + 16, index)


def set_bool_attr(manifest: bytes, element: str, attr: str, value: bool) -> bytes | None:
    x = Axml(manifest)
    return x.bytes() if x.set_bool(element, attr, value) else None


def define_meta_permissions(manifest: bytes) -> tuple[bytes, list[str]] | None:
    """Declare Meta-only permissions (com.oculus.*, horizonos.*) the app uses. A permission the app declares itself
    with the default 'normal' protection level is granted at install, so checks like "allow spatial data" pass."""
    x = Axml(manifest)
    wanted = undeclared_meta_permissions(manifest)
    if not wanted or x.index("name") is None:
        return None
    android_ns = x.index(ANDROID_NS)
    if android_ns is None:
        return None
    perm_el = x.index("permission")
    if perm_el is None:
        perm_el = x.append_string("permission")
    names = x.strings()
    name_attr = names.index("name")
    chunks = b""
    for p in wanted:
        value = names.index(p)
        start = struct.pack("<HHIII", RES_XML_START_ELEMENT, 16, 56, 0, 0xFFFFFFFF)
        start += struct.pack("<IIHHHHHH", 0xFFFFFFFF, perm_el, 20, 20, 1, 0, 0, 0)
        start += struct.pack("<IIIHBBI", android_ns, name_attr, value, 8, 0, TYPE_STRING, value)
        end = (struct.pack("<HHIII", RES_XML_END_ELEMENT, 16, 24, 0, 0xFFFFFFFF)
               + struct.pack("<II", 0xFFFFFFFF, perm_el))
        chunks += start + end
    manifest_el = next(el for el in x.elements() if el.name == "manifest")
    insert_at = manifest_el.offset + manifest_el.size
    data = x.data[:insert_at] + chunks + x.data[insert_at:]
    struct.pack_into("<I", data, 4, len(data))
    return bytes(data), wanted


# ---------------------------------------------------------------------- browser stand-in (frame.browser_handoff)
RES_XML_START_NAMESPACE, RES_XML_END_NAMESPACE, RES_XML_CDATA, RES_XML_RESOURCE_MAP = 0x0100, 0x0101, 0x0104, 0x0180
VIEW = "android.intent.action.VIEW"
BROWSABLE = "android.intent.category.BROWSABLE"
CUSTOM_TABS_SERVICE = "android.support.customtabs.action.CustomTabsService"
BROWSER_SCHEMES = ("http", "https")
THEME_NO_DISPLAY = 0x01030055  # @android:style/Theme.NoDisplay
# android: attributes of the stand-in activity, by framework resource id (the order aapt writes them in)
ATTR_IDS = {"theme": 0x01010000, "name": 0x01010003, "exported": 0x01010010, "excludeFromRecents": 0x01010017,
            "scheme": 0x01010027}


def events(manifest: bytes) -> Iterator[tuple[str, str, Element | None]]:
    """("start" | "end", element name, the Element for starts) in file order."""
    x = Axml(manifest)
    names = x.strings()
    starts = {el.offset: el for el in x.elements()}
    pos = struct.unpack_from("<H", x.data, 2)[0]
    while pos < len(x.data):
        typ, hsz, size = struct.unpack_from("<HHI", x.data, pos)
        if size == 0:
            break
        if typ == RES_XML_START_ELEMENT:
            el = starts[pos]
            yield "start", el.name, el
        elif typ == RES_XML_END_ELEMENT:
            name = struct.unpack_from("<I", x.data, pos + hsz + 4)[0]
            yield "end", names[name] if name < len(names) else "", None
        pos += size


def queries_browser(manifest: bytes) -> bool:
    """The manifest's <queries> ask for a web browser: an intent with action VIEW and an http(s) scheme, or a Custom
    Tabs provider. Apps declare that (Android 11 package visibility) when they look for a browser before opening a
    page, e.g. Plex's sign-in, which then says "no browser installed" in Lepton (its Android has none)."""
    x = Axml(manifest)
    queries = 0
    intent: dict | None = None
    for kind, name, el in events(manifest):
        if kind == "start":
            if name == "queries":
                queries += 1
            elif queries and name == "intent":
                intent = {"actions": set(), "schemes": set()}
            elif intent is not None and name == "action":
                intent["actions"].add(x.attr_str(el, "name"))
            elif intent is not None and name == "data":
                intent["schemes"].add(x.attr_str(el, "scheme"))
        elif name == "queries":
            queries -= 1
        elif name == "intent" and intent is not None:
            if CUSTOM_TABS_SERVICE in intent["actions"] or (
                    VIEW in intent["actions"] and intent["schemes"] & set(BROWSER_SCHEMES)):
                return True
            intent = None
    return False


def browser_activities(manifest: bytes) -> list[str]:
    """The app's own activities that look like a web browser: a VIEW + BROWSABLE filter for http(s) without a host
    (deep links name their hosts)."""
    x = Axml(manifest)
    out: list[str] = []
    component = None
    filt: dict | None = None
    for kind, name, el in events(manifest):
        if kind == "start":
            if name in ("activity", "activity-alias"):
                component = x.attr_str(el, "name") or ""
            elif name == "intent-filter" and component is not None:
                filt = {"actions": set(), "cats": set(), "schemes": set(), "hosts": False}
            elif filt is not None and name == "action":
                filt["actions"].add(x.attr_str(el, "name"))
            elif filt is not None and name == "category":
                filt["cats"].add(x.attr_str(el, "name"))
            elif filt is not None and name == "data":
                filt["schemes"].add(x.attr_str(el, "scheme"))
                filt["hosts"] |= x.attr_str(el, "host") is not None
        elif name == "intent-filter" and filt is not None:
            if (VIEW in filt["actions"] and BROWSABLE in filt["cats"] and filt["schemes"] & set(BROWSER_SCHEMES)
                    and not filt["hosts"] and component not in out):
                out.append(component)
            filt = None
        elif name in ("activity", "activity-alias"):
            component = None
    return out


def _resource_map(x: Axml) -> tuple[int, list[int]] | None:
    """(chunk offset, attribute resource ids) of the resource map that follows the string pool."""
    pos = x.pool_off + x.pool_size
    typ, hsz, size = struct.unpack_from("<HHI", x.data, pos)
    if typ != RES_XML_RESOURCE_MAP:
        return None
    return pos, list(struct.unpack_from(f"<{(size - hsz) // 4}I", x.data, pos + hsz))


def _shift_string_refs(data: bytearray, start: int, at: int) -> None:
    """Every string reference >= at in the XML nodes from `start` on moves up by one (a string was inserted)."""
    def bump(off: int) -> None:
        v = struct.unpack_from("<I", data, off)[0]
        if v != 0xFFFFFFFF and v >= at:
            struct.pack_into("<I", data, off, v + 1)

    pos = start
    while pos < len(data):
        typ, hsz, size = struct.unpack_from("<HHI", data, pos)
        if size == 0:
            break
        ext = pos + hsz
        if typ in (RES_XML_START_NAMESPACE, RES_XML_END_NAMESPACE, RES_XML_START_ELEMENT, RES_XML_END_ELEMENT,
                   RES_XML_CDATA):
            bump(pos + 12)  # comment
            bump(ext)  # namespace prefix / element namespace / cdata text
        if typ in (RES_XML_START_NAMESPACE, RES_XML_END_NAMESPACE, RES_XML_START_ELEMENT, RES_XML_END_ELEMENT):
            bump(ext + 4)  # namespace uri / element name
        if typ == RES_XML_CDATA and data[ext + 7] == TYPE_STRING:
            bump(ext + 8)
        if typ == RES_XML_START_ELEMENT:
            astart, asize, acount = struct.unpack_from("<HHH", data, ext + 8)
            for i in range(acount):
                a = ext + astart + i * asize
                for off in (0, 4, 8):  # namespace, name, raw value
                    bump(a + off)
                if data[a + 15] == TYPE_STRING:
                    bump(a + 16)
        pos += size


def _attr_name(x: Axml, name: str, resource: int) -> Axml:
    """Make sure an android: attribute name exists in the resource-mapped part of the string pool (Android matches
    manifest attributes by resource id). A missing one is inserted right after the mapped strings (its offset; the
    text goes at the end of the pool) and every string reference from there on moves up by one."""
    found = _resource_map(x)
    ids = found[1] if found else []
    names = x.strings()
    if any(i < len(names) and names[i] == name and rid == resource for i, rid in enumerate(ids)):
        return x
    if x.styles:
        raise ValueError("string pools with styles are not supported")
    at = len(ids)
    if x.utf8:
        b = name.encode("utf-8")
        enc = bytes([len(name), len(b)]) + b + b"\0"
    else:
        enc = struct.pack("<H", len(name)) + name.encode("utf-16-le") + b"\0\0"
    d, po = x.data, x.pool_off
    str_data = bytes(d[po + x.sstart: po + x.pool_size])
    offsets = x.offsets[:at] + [len(str_data)] + x.offsets[at:]
    str_data += enc + b"\0" * (-(len(str_data) + len(enc)) % 4)
    sstart = x.pool_hsz + 4 * len(offsets)
    header = struct.pack("<HHIIIIII", RES_STRING_POOL, x.pool_hsz, sstart + len(str_data), len(offsets), 0,
                         x.flags & ~0x1, sstart, 0) + bytes(d[po + 28: po + x.pool_hsz])  # (no longer sorted)
    pool = header + struct.pack(f"<{len(offsets)}I", *offsets) + str_data
    rest = found[0] + struct.unpack_from("<I", d, found[0] + 4)[0] if found else po + x.pool_size
    resmap = struct.pack("<HHI", RES_XML_RESOURCE_MAP, 8, 8 + 4 * (at + 1)) + struct.pack(f"<{at + 1}I", *ids,
                                                                                          resource)
    data = bytearray(bytes(d[:po]) + pool + resmap + bytes(d[rest:]))
    _shift_string_refs(data, po + len(pool) + len(resmap), at)
    struct.pack_into("<I", data, 4, len(data))
    return Axml(bytes(data))


def add_browser_activity(manifest: bytes, activity: str) -> bytes | None:
    """Declare `activity` (fully qualified) as a web browser at the start of the app's <application>: exported,
    Theme.NoDisplay, left out of recents, with a VIEW + DEFAULT + BROWSABLE filter for http and https (no host).
    None when it's already declared or there is no <application>."""
    x = Axml(manifest)
    elements = list(x.elements())
    if any(el.name == "activity" and x.attr_str(el, "name") == activity for el in elements) or \
            not any(el.name == "application" for el in elements):
        return None
    for name, resource in ATTR_IDS.items():
        x = _attr_name(x, name, resource)

    def index(s: str) -> int:
        i = x.index(s)
        return i if i is not None else x.append_string(s)

    ns = index(ANDROID_NS)
    activity_el, filter_el, action_el, category_el, data_el = (index(s) for s in (
        "activity", "intent-filter", "action", "category", "data"))
    values = {s: index(s) for s in (activity, VIEW, DEFAULT, BROWSABLE, *BROWSER_SCHEMES)}
    names, ids = x.strings(), _resource_map(x)[1]
    attr = {a: next(i for i, rid in enumerate(ids) if rid == resource and names[i] == a)
            for a, resource in ATTR_IDS.items()}

    def record(name: str, kind: int, value: int) -> bytes:
        return struct.pack("<IIIHBBI", ns, attr[name], value if kind == TYPE_STRING else 0xFFFFFFFF, 8, 0, kind,
                           value)

    def start(el: int, records: list[bytes]) -> bytes:
        return (struct.pack("<HHIII", RES_XML_START_ELEMENT, 16, 36 + 20 * len(records), 0, 0xFFFFFFFF)
                + struct.pack("<IIHHHHHH", 0xFFFFFFFF, el, 20, 20, len(records), 0, 0, 0) + b"".join(records))

    def end(el: int) -> bytes:
        return struct.pack("<HHIII", RES_XML_END_ELEMENT, 16, 24, 0, 0xFFFFFFFF) + struct.pack("<II", 0xFFFFFFFF, el)

    # attributes sorted by resource id, as aapt writes them (Android's attribute lookup relies on the order)
    chunk = start(activity_el, [record("theme", TYPE_REFERENCE, THEME_NO_DISPLAY),
                                record("name", TYPE_STRING, values[activity]),
                                record("exported", TYPE_INT_BOOLEAN, 0xFFFFFFFF),
                                record("excludeFromRecents", TYPE_INT_BOOLEAN, 0xFFFFFFFF)])
    chunk += start(filter_el, [])
    chunk += start(action_el, [record("name", TYPE_STRING, values[VIEW])]) + end(action_el)
    for cat in (DEFAULT, BROWSABLE):
        chunk += start(category_el, [record("name", TYPE_STRING, values[cat])]) + end(category_el)
    for scheme in BROWSER_SCHEMES:
        chunk += start(data_el, [record("scheme", TYPE_STRING, values[scheme])]) + end(data_el)
    chunk += end(filter_el) + end(activity_el)
    app = next(el for el in x.elements() if el.name == "application")
    at = app.offset + app.size
    data = x.data[:at] + chunk + x.data[at:]
    struct.pack_into("<I", data, 4, len(data))
    return bytes(data)


# ---------------------------------------------------------------------- shared libraries (frame.keep_system_libraries)
USES_LIBRARY_ATTR_IDS = {"name": 0x01010003, "required": 0x0101028E}


def uses_libraries(manifest: bytes) -> list[str]:
    """The shared Java libraries the manifest's <uses-library> entries name (not <uses-native-library>), in file
    order, each once."""
    x = Axml(manifest)
    out: list[str] = []
    for el in x.elements():
        if el.name == "uses-library":
            name = x.attr_str(el, "name")
            if name and name not in out:
                out.append(name)
    return out


def horizonos_supplements(manifest: bytes) -> list[str]:
    """The Horizon OS supplements the manifest's <horizonos:uses-horizonos-supplement> entries name, in file order,
    each once. Meta's Horizon Platform SDK (com.meta.xr.sdk.platform 2xx) declares horizonos-supplement-
    hzplatformclientcore: on Android its Oculus.Platform calls go through that supplement's Java classes
    (horizonos.supplement.hzplatformclientcore.HorizonPlatformCore), not through libovrplatformloader.so, so OVRPort's
    platform library never sees them (GitHub #511, TMNT: Empire City 1.0.9)."""
    x = Axml(manifest)
    out: list[str] = []
    for el in x.elements():
        if el.name == "uses-horizonos-supplement":
            name = x.attr_str(el, "name")
            if name and name not in out:
                out.append(name)
    return out


def add_uses_libraries(manifest: bytes, libraries: list[str]) -> bytes | None:
    """Declare each of `libraries` the manifest doesn't name yet as `<uses-library android:name=… android:required=
    "false"/>` at the start of <application> (never required: a library the device lacks must not block the
    install). None when there's nothing to add or no <application>."""
    have = set(uses_libraries(manifest))
    wanted = [lib for i, lib in enumerate(libraries) if lib not in have and lib not in libraries[:i]]
    x = Axml(manifest)
    if not wanted or not any(el.name == "application" for el in x.elements()):
        return None
    for name, resource in USES_LIBRARY_ATTR_IDS.items():
        x = _attr_name(x, name, resource)

    def index(s: str) -> int:
        i = x.index(s)
        return i if i is not None else x.append_string(s)

    ns, element = index(ANDROID_NS), index("uses-library")
    values = [index(lib) for lib in wanted]
    names, ids = x.strings(), _resource_map(x)[1]
    attr = {a: next(i for i, rid in enumerate(ids) if rid == resource and names[i] == a)
            for a, resource in USES_LIBRARY_ATTR_IDS.items()}
    chunk = b""
    for value in values:
        # name before required: sorted by resource id, as aapt writes them
        records = (struct.pack("<IIIHBBI", ns, attr["name"], value, 8, 0, TYPE_STRING, value)
                   + struct.pack("<IIIHBBI", ns, attr["required"], 0xFFFFFFFF, 8, 0, TYPE_INT_BOOLEAN, 0))
        chunk += (struct.pack("<HHIII", RES_XML_START_ELEMENT, 16, 36 + len(records), 0, 0xFFFFFFFF)
                  + struct.pack("<IIHHHHHH", 0xFFFFFFFF, element, 20, 20, 2, 0, 0, 0) + records
                  + struct.pack("<HHIII", RES_XML_END_ELEMENT, 16, 24, 0, 0xFFFFFFFF)
                  + struct.pack("<II", 0xFFFFFFFF, element))
    app = next(el for el in x.elements() if el.name == "application")
    at = app.offset + app.size
    data = x.data[:at] + chunk + x.data[at:]
    struct.pack_into("<I", data, 4, len(data))
    return bytes(data)


def manifest_attrs(manifest: bytes) -> dict[str, str | int | bool]:
    """The <manifest> element's attributes by name (package, split, versionCode, configForSplit, isFeatureSplit…):
    strings, ints or bools. App bundles (sources/bundles.py) tell the base APK from its splits with them."""
    x = Axml(manifest)
    names = x.strings()
    el = next((e for e in x.elements() if e.name == "manifest"), None)
    out: dict = {}
    for a in el.attrs if el else ():
        if a.name >= len(names):
            continue
        key = names[a.name]
        if a.raw < len(names):
            out[key] = names[a.raw]
        elif a.dtype == TYPE_STRING and a.value < len(names):
            out[key] = names[a.value]
        elif a.dtype == TYPE_INT_BOOLEAN:
            out[key] = a.value != 0
        elif a.dtype in (TYPE_INT_DEC, TYPE_INT_HEX):
            out[key] = a.value
    return out


def has_attr(manifest: bytes, element: str, attr: str) -> bool:
    x = Axml(manifest)
    names = x.strings()
    return any(el.name == element and any(a.name < len(names) and names[a.name] == attr for a in el.attrs)
               for el in x.elements())


def remove_attrs(manifest: bytes, element: str, attrs: set[str]) -> bytes | None:
    """Drop the named attributes from the first <element> (only the attribute records; their strings stay in the
    pool). None when it has none of them. App bundles: a merged base loses isSplitRequired/requiredSplitTypes, else
    Android refuses it without its splits (INSTALL_FAILED_MISSING_SPLIT)."""
    x = Axml(manifest)
    names = x.strings()
    el = next((e for e in x.elements() if e.name == element), None)
    if el is None:
        return None
    drop = [i for i, a in enumerate(el.attrs) if a.name < len(names) and names[a.name] in attrs]
    if not drop:
        return None
    data = bytearray(x.data)
    ext = el.offset + struct.unpack_from("<H", data, el.offset + 2)[0]
    start, stride, count = struct.unpack_from("<HHH", data, ext + 8)
    for i in reversed(drop):  # from the end: the records before it stay where they are
        at = ext + start + i * stride
        del data[at:at + stride]
        count -= 1
        for offset in (14, 16, 18):  # one-based id/class/style attribute indices
            value = struct.unpack_from("<H", data, ext + offset)[0]
            if value == i + 1:
                struct.pack_into("<H", data, ext + offset, 0)
            elif value > i + 1:
                struct.pack_into("<H", data, ext + offset, value - 1)
    struct.pack_into("<I", data, el.offset + 4, el.size - len(drop) * stride)
    struct.pack_into("<H", data, ext + 12, count)
    struct.pack_into("<I", data, 4, len(data))
    return bytes(data)
