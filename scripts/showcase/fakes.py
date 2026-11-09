"""Pretend Frame for GUI renders (ui_smoke, docs screenshots, the demo tour): its files, screenshots, games, drives,
monitor stream and Type on Frame, with no network and no device. Shared by scripts/ui_smoke.py and scripts/showcase/.

The demo library (scripts/showcase/demo_home.py) writes `showcase-frame.json` into the data dir: which games the
pretend Frame has installed (and on which drive), the running game and its battery; without it the library's working
Quest builds are "installed" (the 8 largest).
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time

from frameport.core import library
from frameport.core.paths import user_data_dir
from frameport.ui.app import FramePortApp

FRAME_STATE = "showcase-frame.json"
ROOM_DIR = None  # a folder with room.txt (+ its mesh) to serve as the pretend Frame's room instead of the made-up one
SHOTS_AT = time.mktime((2026, 10, 4, 21, 40, 0, 0, 0, -1))  # the newest sample screenshot (local time)


def frame_state() -> dict:
    """The demo library's pretend Frame ({installed: [{package, drive, sha256?}], running, battery}), else {}."""
    try:
        return json.loads((user_data_dir() / FRAME_STATE).read_text())
    except (OSError, ValueError):
        return {}


def fake_room_mesh(room, cell: float = 0.2, seed: int = 7):
    """A made-up scan of `room`: its floor, walls and furniture as noisy triangle grids, wound like a real scan's
    (counter-clockwise seen from inside the room). Like a real scan it has holes where the headset sees nothing:
    the floor under furniture that stands on it, and window glass."""
    from frameport.mr.room import Mesh, plane_corners
    from frameport.mr.view3d import box_corners

    rnd = random.Random(seed)
    verts, idx = [], []

    def surface(o, u, v, noise=0.012, keep=None):  # o + s*u + t*v, normal = u x v; keep(point) False = a hole
        lu = max(1, round((sum(c * c for c in u) ** 0.5) / cell))
        lv = max(1, round((sum(c * c for c in v) ** 0.5) / cell))
        base = len(verts)
        for i in range(lv + 1):
            for j in range(lu + 1):
                n = noise if 0 < i < lv and 0 < j < lu else 0.0
                verts.append(tuple(o[k] + u[k] * j / lu + v[k] * i / lv + rnd.uniform(-n, n) for k in range(3)))
        for i in range(lv):
            for j in range(lu):
                mid = tuple(o[k] + u[k] * (j + 0.5) / lu + v[k] * (i + 0.5) / lv for k in range(3))
                if keep is not None and not keep(mid):
                    continue
                a = base + i * (lu + 1) + j
                b, c, d = a + 1, a + lu + 1, a + lu + 2
                idx.extend((a, b, d, a, d, c))

    def inside(vol, x, z):  # (x, z) on the box's footprint
        a = math.radians(vol.yaw_deg)
        dx, dz = x - vol.cx, z - vol.cz
        lx, lz = math.cos(a) * dx - math.sin(a) * dz, math.sin(a) * dx + math.cos(a) * dz
        return abs(lx) < vol.w / 2 and abs(lz) < vol.d / 2

    y0 = room.floor
    standing = [v for v in room.volumes if v.top_y - v.h - y0 < 0.05 and v.h > 0.3]  # on the floor, no gap below
    windows = [plane_corners(p) for p in room.planes if p.label == "WINDOW_FRAME"]

    def floor_seen(p):
        return not any(inside(v, p[0], p[2]) for v in standing)

    def wall_seen(p):  # not through a window's glass
        for c in windows:
            xs, ys, zs = [q[0] for q in c], [q[1] for q in c], [q[2] for q in c]
            if (min(ys) + 0.05 < p[1] < max(ys) - 0.05 and min(xs) - 0.05 <= p[0] <= max(xs) + 0.05
                    and min(zs) - 0.05 <= p[2] <= max(zs) + 0.05):
                return False
        return True

    xs, zs = [x for x, _ in room.outline], [z for _, z in room.outline]
    surface((min(xs), y0, max(zs)), (max(xs) - min(xs), 0, 0), (0, 0, min(zs) - max(zs)), 0.008,
            floor_seen)  # floor, facing up
    for w in room.walls:
        if w.label == "INVISIBLE_WALL_FACE":
            continue
        surface((w.x0, w.y0, w.z0), (w.x1 - w.x0, 0, w.z1 - w.z0), (0, w.y1 - w.y0, 0), 0.012,
                wall_seen)  # facing into the room
    for vol in room.volumes:
        c = box_corners(vol.cx, vol.cz, vol.yaw_deg, vol.w, vol.d)
        top, bottom = vol.top_y, vol.top_y - vol.h
        soft = 0.03 if vol.label in ("BED", "COUCH") else 0.015  # (a duvet isn't flat)
        surface((c[0][0], top, c[0][1]), (c[3][0] - c[0][0], 0, c[3][1] - c[0][1]),
                (c[1][0] - c[0][0], 0, c[1][1] - c[0][1]), soft)  # top, facing up
        for i in range(4):
            a, b = c[i], c[(i + 1) % 4]
            surface((a[0], bottom, a[1]), (0, top - bottom, 0), (b[0] - a[0], 0, b[1] - a[1]), 0.012)  # facing out
    return Mesh(vertices=verts, indices=idx)


def fake_room():
    """The pretend Frame's scanned room, made up from scratch (no real scan): a 3.6 x 3.2 m bedroom with a bed and a
    nightstand under a wall-mounted TV, a closet, a desk with two monitors under the window, a door, a lamp and a
    plant. Walls: 0 = the bed's (-Z, forward), 1 = right (+X), 2 = behind (+Z), 3 = the desk's and the door's (-X);
    the 3D view looks at walls 0 and 3."""
    from frameport.mr.room import Volume, box_room, plane_on_wall

    room = box_room(3.6, 3.2, 2.5)
    room.source, room.universe, room.time = "scan", 7, int(SHOTS_AT)
    room.walls = [type(w)(f"{0xa0 + i:032x}", w.x0, w.z0, w.x1, w.z1, w.y0, w.y1, w.label)
                  for i, w in enumerate(room.walls)]  # (fixed ids: the same room in every render)
    room.uuid, room.floor_uuid, room.ceiling_uuid = (f"{0xf0 + i:032x}" for i in range(3))
    vid = iter(f"{i:032x}" for i in range(1, 99))
    room.volumes = [Volume(next(vid), "BED", 0.6, -0.58, 0.55, 0, 1.45, 2.05, 0.55),
                    Volume(next(vid), "SCREEN", 0.6, -1.55, 1.78, 0, 1.12, 0.07, 0.66),      # TV above the bed
                    Volume(next(vid), "TABLE", -1.44, -0.25, 0.75, 90, 1.4, 0.7, 0.75),      # desk
                    Volume(next(vid), "SCREEN", -1.6, -0.56, 1.2, 82, 0.6, 0.06, 0.36),      # monitors on it
                    Volume(next(vid), "SCREEN", -1.6, 0.06, 1.2, 98, 0.6, 0.06, 0.36),
                    Volume(next(vid), "STORAGE", -1.25, -1.3, 2.1, 0, 1.0, 0.6, 2.1),        # closet
                    Volume(next(vid), "TABLE", -0.42, -1.38, 0.52, 0, 0.45, 0.4, 0.52),      # nightstand
                    Volume(next(vid), "LAMP", -0.42, -1.42, 0.9, 0, 0.2, 0.2, 0.38),
                    Volume(next(vid), "PLANT", 1.56, -1.37, 0.95, 0, 0.36, 0.36, 0.95)]      # in the corner
    room.planes = [plane_on_wall(room, 3, 0.17, 0.0, 0.85, 2.05, "DOOR_FRAME", f"{0xd0:032x}"),
                   plane_on_wall(room, 3, 0.58, 1.22, 1.05, 1.05, "WINDOW_FRAME", f"{0xd1:032x}")]
    return room


class FakeRoom:
    """The pretend Frame's room storage + scanner (agent room_* commands), files under its fake home like the real
    agent's (rooms.export reads the mesh over the fake SFTP). A scan steps forward on every status poll and then
    waits, so a screenshot finds it mid-scan; room_capture_stop cancels it."""

    def __init__(self, home: str):
        from frameport.mr.room import dumps, write_mesh

        self.home = home
        self.scan: dict = {"state": "none"}
        self.polls = 0
        self.mesh_triangles = 0
        if ROOM_DIR:
            from pathlib import Path

            from frameport.mr.room import parse
            src = Path(ROOM_DIR)
            self.room = parse((src / "room.txt").read_text())
            self.mesh = (src / self.room.mesh.filename).read_bytes() if self.room.mesh else None
            if self.room.mesh:
                self.mesh_triangles = self.room.mesh.index_count // 3
        else:
            from frameport.mr.room import Mesh

            self.room = fake_room()
            m = fake_room_mesh(self.room)
            self.room.mesh = Mesh(uuid=f"{0xe0:032x}", filename="room-mesh.bin", vertex_count=len(m.vertices),
                                  index_count=len(m.indices))
            self.mesh = write_mesh(m)
            self.mesh_triangles = len(m.indices) // 3
        self.text = dumps(self.room)
        self.refine: dict = {}  # the saved clean-up settings (non-defaults)
        self.edited = False
        self.last_report = self.report()
        self._write()

    def _folder(self):
        return os.path.join(self.home, ".local/share/frameport/rooms", str(self.room.universe))

    def _write(self) -> None:
        os.makedirs(self._folder(), exist_ok=True)
        with open(os.path.join(self._folder(), "room.txt"), "w") as f:
            f.write(self.text)
        if self.mesh is not None and self.room.mesh:
            with open(os.path.join(self._folder(), self.room.mesh.filename), "wb") as f:
                f.write(self.mesh)

    # the scan clean-up (scan.json, docs/mr/ROOM_FORMAT.md): what the label rules decided for each of the scanner's
    # boxes (in its order; to = None: removed), and the door / closet suggestions (never applied by the scanner)
    LABELS = [
        {"from": "BED", "to": "BED", "reason": "flat top at 0.55 m, 2.05 x 1.45 m against a wall: a bed",
         "box": {"w": 1.45, "d": 2.05, "h": 0.55}},
        {"from": "TABLE", "to": "SCREEN", "reason": "7 cm thin, flat on the wall, 1.1 m above the floor: a screen",
         "box": {"w": 1.12, "d": 0.07, "h": 0.66}},
        {"from": "TABLE", "to": "TABLE", "reason": "top at 0.75 m by a wall: a desk",
         "box": {"w": 1.4, "d": 0.7, "h": 0.75}},
        {"from": "OTHER", "to": "SCREEN", "reason": "6 cm thin and upright, standing on a table: a screen",
         "box": {"w": 0.6, "d": 0.06, "h": 0.36}},
        {"from": "SCREEN", "to": "SCREEN", "reason": "upright on a table, facing the room: a screen",
         "box": {"w": 0.6, "d": 0.06, "h": 0.36}},
        {"from": "STORAGE", "to": "STORAGE", "reason": "2.1 m high and 0.6 m deep in a corner: storage",
         "box": {"w": 1.0, "d": 0.6, "h": 2.1}},
        {"from": "STORAGE", "to": "TABLE", "reason": "top at 0.52 m, 0.45 m wide by the bed: a nightstand",
         "box": {"w": 0.45, "d": 0.4, "h": 0.52}},
        {"from": "LAMP", "to": "LAMP", "reason": "0.38 m high, standing on a table: a lamp",
         "box": {"w": 0.2, "d": 0.2, "h": 0.38}},
        {"from": "PLANT", "to": "PLANT", "reason": "0.95 m high and round from above: a plant",
         "box": {"w": 0.36, "d": 0.36, "h": 0.95}},
        {"from": "OTHER", "to": None, "reason": "0.5 m behind the wall: outside the room",
         "box": {"w": 0.6, "d": 0.4, "h": 0.8}},
        {"from": "TABLE", "to": None, "reason": "only 3 cm above the floor: a rug, not furniture",
         "box": {"w": 1.6, "d": 1.1, "h": 0.03}},
        {"from": "STORAGE", "to": None, "suggest": "door", "reason": "2.0 m high, 10 cm thin, flat on the wall",
         "box": {"w": 0.8, "d": 0.1, "h": 2.0}}]
    SUGGESTIONS = [{"kind": "door", "box": {"cx": 1.75, "cz": 1.0, "top_y": 2.0, "yaw_deg": 90, "w": 0.8, "d": 0.1,
                                            "h": 2.0}, "wall": 1, "along": 0.81,
                    "reason": "A tall, thin slab on the wall, 0.8 m wide: a door, or a closet's front"}]

    def report(self, settings: dict | None = None) -> dict:
        """scan.json's report (its keys: docs/mr/ROOM_FORMAT.md), consistent with the room's own mesh: `kept` is
        the mesh's triangle count with the recommended settings, other settings remove more or less."""
        s = {**{"walked": 0.4, "smooth": 8, "island": 0.2, "walls": "panels"}, **(settings or {})}

        def removed(s):
            return {"walked": int(1900 * s["walked"]), "small_pieces": int(3600 * s["island"]),
                    "near_walls": 140 if s["walls"] == "panels" else 40}

        now, default = removed(s), removed({"walked": 0.4, "island": 0.2, "walls": "panels"})
        fixed = {"outside": 1260, "thin_pieces": 184, "erased": 0}
        kept = self.mesh_triangles - (sum(now.values()) - sum(default.values()))
        return {"triangles": self.mesh_triangles + sum(default.values()) + sum(fixed.values()), "kept": kept,
                "structural": int(self.mesh_triangles * 0.62) if s["walls"] != "off" else 0, **now, **fixed,
                "tops": 410, "panel_triangles": 64 if s["walls"] == "panels" else 0, "truncated": 0,
                "walked_skipped": False, "floor_y": 0.0, "ceiling_y": 2.5,
                "boxes": sum(1 for lb in self.LABELS if lb["to"]),
                "relabelled": sum(1 for lb in self.LABELS if lb["to"] and lb["to"] != lb["from"]),
                "removed_boxes": sum(1 for lb in self.LABELS if not lb["to"]),
                "suggestions": len(self.SUGGESTIONS), "plane_shifts": [{"kind": "wall", "wall": 3, "shift": 0.02}]}

    def status(self) -> dict:
        from frameport.mr.room import summary

        if self.text is None:
            return {"rooms": [], "universe": self.room.universe, "room": None, "room_text": None, "aligned": False,
                    "default_circle": False, "scanner_installed": True, "refine": self.refine, "labels": [],
                    "suggestions": [], "report": None}
        scanned = self.room.source == "scan"
        return {"rooms": [], "universe": self.room.universe, "aligned": True, "default_circle": False,
                "scanner_installed": True, "room_text": self.text,
                "room": {"universe": self.room.universe, "source": self.room.source, "summary": summary(self.room)},
                "refine": dict(self.refine), "report": self.last_report if scanned else None,
                "labels": self.LABELS if scanned else [], "suggestions": self.SUGGESTIONS if scanned else [],
                "raw": scanned, "edited": self.edited}

    def agent(self, command, **args):
        if command == "room_status":
            return self.status()
        if command == "room_delete":
            self.text = None
            return {"deleted": True}
        if command == "room_from_boundary":
            from frameport.mr.room import box_room, dumps
            room = box_room(5, 4, 2.5)
            room.source, room.universe = "boundary", self.room.universe
            self.room, self.mesh, self.text = room, None, dumps(room)
            self._write()
            return {"room": {"source": "boundary"}}
        if command == "room_capture":
            self.scan, self.polls = {"state": "waiting", "started": time.time()}, 0
            return {"started": True}
        if command == "room_capture_status":
            self.polls += 1
            if self.scan.get("state") in ("waiting", "recording"):
                steps = [{"state": "waiting"}, {"state": "recording", "directions": 3, "looked_up": 0},
                         {"state": "recording", "directions": 7, "looked_up": 6, "looked_down": 2}]
                self.scan.update(steps[min(self.polls - 1, len(steps) - 1)])
            return dict(self.scan)
        if command == "room_refine":  # a clean-up takes a moment; the room's mesh stays the made-up one
            from frameport.mr.room import dumps

            time.sleep(5)
            if args.get("settings") is not None:
                self.refine = dict(args["settings"])
            if not args.get("keep_edits", True):
                self.edited = False
            self.last_report = self.report(self.refine)
            self.room.time += 1
            self.text = dumps(self.room)
            self._write()
            return {"summary": {"refine": self.last_report}, **self.status()}
        if command == "room_put":  # the box editor's Save
            from pathlib import Path

            from frameport.mr.room import dumps, parse
            path = Path(args["path"])
            self.room = parse(path.read_text())
            if self.room.mesh:
                self.mesh = (path.parent / self.room.mesh.filename).read_bytes()
            self.text = dumps(self.room)
            self.edited = bool(args.get("edited"))
            self._write()
            return {"room": {"universe": self.room.universe}}
        if command == "room_capture_stop":
            if self.scan.get("state") in ("waiting", "recording", "building"):
                self.scan.update(state="failed", error="cancelled", cancelled=True)
            return {"stopped": [], "state": self.scan.get("state")}
        raise RuntimeError(f"fake Frame: {command} not available")


class FakeFS:
    """--fake-frame's file system for the Files tab: a temp folder with sample files, served like SFTP."""

    def __init__(self):
        import tempfile

        self.home = tempfile.mkdtemp(prefix="fp-fake-frame-")
        samples = {"Videos": ["Earth from orbit (360).mp4", "Mars landing (VR180).mkv", "Concert 8K 3D.mp4"],
                   "Downloads": ["Mods/", "readme.txt"], "Documents": ["Saves/", "Notes.pdf"]}
        sizes = {".mp4": 2_400_000_000, ".mkv": 1_100_000_000}
        for folder, names in samples.items():
            base = os.path.join(self.home, folder)
            os.makedirs(os.path.join(base, "Trips"), exist_ok=True)
            for n in names:
                if n.endswith("/"):
                    os.makedirs(os.path.join(base, n), exist_ok=True)
                else:
                    with open(os.path.join(base, n), "wb") as f:
                        f.truncate(sizes.get(os.path.splitext(n)[1], 48_000))  # sparse: no real disk use
        # fixed dates (the Files tab shows them): a few days before the newest screenshot, a different one each
        for i, (root, dirs, files) in enumerate(sorted(os.walk(self.home), reverse=True)):
            for j, name in enumerate(sorted(dirs + files)):
                t = SHOTS_AT - 86400 * (2 + (i + j) % 5) - 3600 * ((i * 7 + j * 3) % 11)
                os.utime(os.path.join(root, name), (t, t))
        self.shots = self._screenshots()
        self.room = FakeRoom(self.home)
        # GitHub #532: the Frame's /tmp (served from a folder here: never this computer's own /tmp) and games' storage
        self.tmp = os.path.join(self.home, ".fake-tmp")
        for name, size in (("frameport-install-1a2b/", 0), ("Mods.zip", 312_000_000),
                           ("game-install.apk", 1_400_000_000), ("steam-logs.txt", 48_000)):
            path = os.path.join(self.tmp, name)
            os.makedirs(path if name.endswith("/") else self.tmp, exist_ok=True)
            if not name.endswith("/"):
                with open(path, "wb") as f:
                    f.truncate(size)

    def real(self, path: str) -> str:
        """Where a Frame path is here (only /tmp moves)."""
        path = str(path)
        if (path == "/tmp" or path.startswith("/tmp/")) and not path.startswith(self.home):
            return self.tmp + path[4:]
        return path

    def _game_storage(self, pkg: str, internal: bool = False) -> str:
        """A pretend game's /sdcard (Lepton's links to the shared folders, Android/data) or its internal storage."""
        base = os.path.join(self.home, "Applications/quest-frame", pkg, "lepton-data")
        if internal:
            root = os.path.join(base, "internal", pkg)
            if not os.path.isdir(root):
                for rel, size in (("files/UnrealGame/Saved/SaveGames/Slot1.sav", 184_000),
                                  ("files/UnrealGame/Saved/SaveGames/Slot2.sav", 176_000),
                                  ("files/UnrealGame/Saved/Config/Android/GameUserSettings.ini", 2_300),
                                  ("shared_prefs/" + pkg + ".v2.playerprefs.xml", 4_100),
                                  ("databases/progress.db", 64_000)):
                    os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                    with open(os.path.join(root, rel), "wb") as f:
                        f.truncate(size)
            return root
        root = os.path.join(base, "external")
        if not os.path.isdir(root):
            os.makedirs(os.path.join(root, "Android/data", pkg, "files"), exist_ok=True)
            for home_name, android in (("Videos", "Movies"), ("Downloads", "Download"), ("Documents", "Documents")):
                os.symlink(os.path.join(self.home, home_name), os.path.join(root, android))
        return root

    def run(self, cmd: str):
        """Only the room uploads' folder (rooms._upload_dir: mkdir -p / rm -rf below the room store)."""
        import shlex
        import shutil

        rooms_dir = os.path.join(self.home, ".local/share/frameport/rooms")
        words = shlex.split(cmd)
        path = words[-1] if words else ""
        if words[:2] == ["mkdir", "-p"] and path.startswith(rooms_dir):
            os.makedirs(path, exist_ok=True)
            return 0, "", ""
        if words[:3] == ["rm", "-rf", "--"] and path.startswith(rooms_dir + "/.incoming-"):
            shutil.rmtree(path, ignore_errors=True)
            return 0, "", ""
        raise AttributeError(f"fake Frame: no shell for {cmd!r}")

    def put(self, local, remote, **kwargs):
        import shutil

        if not str(remote).startswith(os.path.join(self.home, ".local/share/frameport/rooms/.incoming-")):
            raise AttributeError("fake Frame: uploads only for rooms")
        shutil.copyfile(local, remote)

    def _screenshots(self) -> list[dict]:
        """Sample Steam screenshots for the Screenshots tab, two days, a few games + SteamVR. They are the games' own
        in-game store screenshots (shot_*.jpg from the store details), never cover art: games installed on the
        pretend Frame first, the running one leading (Take screenshot adds one of it); SteamVR's own shots (taken in
        a game FramePort didn't install) come from another game's. Only a library without any store screenshots falls
        back to hero/wide art, else colour gradients."""
        from frameport.artwork import fetch

        folder = os.path.join(self.home, "shots")
        os.makedirs(folder, exist_ok=True)
        state = frame_state()
        on_frame = {i["package"] for i in state.get("installed") or []}
        running = state.get("running")
        real, covers = [], []
        for g in library.games():
            if g.get("kind") == "rift":
                continue
            shots_art = sorted(fetch.artwork_dir(g["package"]).glob("shot_*.jpg"))
            if shots_art:
                real.append((g, shots_art))
            else:
                covers.append((g, [p for p in fetch.files(g["package"]) if p.stem in ("hero", "landscape")]))
        games = real or covers
        games.sort(key=lambda ga: (ga[0]["package"] != running, ga[0]["package"] not in on_frame, -len(ga[1]),
                                   ga[0]["package"]))
        self.owners = [(g["package"], g.get("title") or g["package"], arts) for g, arts in games[:3]]
        rest = [arts for g, arts in games[3:] if g["package"] not in on_frame] or [arts for _g, arts in games[3:4]]
        self.owners.append((None, "SteamVR", rest[0] if rest else []))
        self._used = [0] * len(self.owners)  # the next screenshot of each owner (no picture shown twice)
        # fixed dates (renders don't change from one day to the next): 6 shots on the first day, 4 on the one before
        # (--gestures drags across the first row)
        shots = []
        for i in range(10):
            t = SHOTS_AT - i * 2400 - (86400 if i >= 6 else 0)
            shots.append(self._shot(t, i, i % len(self.owners)))
        return shots

    def _shot(self, t: float, i: int, owner: int) -> dict:
        """One Steam screenshot: the owner's next store screenshot, whole (scaled to 1280x720)."""
        from PIL import Image, ImageOps

        pkg, title, arts = self.owners[owner]
        name = time.strftime("%Y%m%d%H%M%S", time.localtime(t)) + "_1.jpg"
        path = os.path.join(self.home, "shots", name)
        if arts:
            n = self._used[owner]
            self._used[owner] += 1
            with Image.open(arts[n % len(arts)]) as src:
                im = ImageOps.fit(src.convert("RGB"), (1280, 720))
        else:
            im = Image.linear_gradient("L").resize((640, 360)).convert("RGB")
            im = Image.merge("RGB", (im.getchannel(0).point(lambda v, i=i: (v + 40 * i) % 256),
                                     im.getchannel(1).point(lambda v: 255 - v), im.getchannel(2)))
        im.save(path, "JPEG", quality=85)
        return {"path": path, "thumb": None, "time": int(t), "width": 1920, "height": 1080,
                "size": os.path.getsize(path), "account": "1", "appid": "250820", "package": pkg, "title": title}

    def take_screenshot(self) -> dict:
        """Take screenshot: a new shot of the running game (its next store screenshot), newest of all."""
        time.sleep(1.2)  # SteamVR captures, Steam saves
        t = max(s["time"] for s in self.shots) + 600
        shot = self._shot(t, len(self.shots) + 1, 0)
        self.shots.insert(0, shot)
        return {"taken": True, "path": shot["path"], "reason": None, "hmd": "Normal"}

    def agent(self, command, **args):
        if command == "storage_targets":
            shared = [{"id": n.lower(), "path": os.path.join(self.home, n), "android": a, "shared": True}
                      for n, a in (("Videos", "/sdcard/Movies"), ("Downloads", "/sdcard/Download"),
                                   ("Documents", "/sdcard/Documents"))]
            if args.get("package"):
                shared.append({"id": "app", "path": self._game_storage(args["package"]), "android": "/sdcard",
                               "shared": False})
            return {"targets": shared}
        if command == "saves_files":  # GitHub #532: a game's internal storage (saves) in the Files tab
            root = self._game_storage(args["package"], internal=True)
            if args.get("op") == "info":
                return {"root": root, "android": f"/data/data/{args['package']}", "exists": True, "running": False}
            if args.get("op") == "list":
                out = []
                for name in sorted(os.listdir(args["path"])):
                    full = os.path.join(args["path"], name)
                    st = os.stat(full)
                    out.append({"name": name, "path": full, "is_dir": os.path.isdir(full), "size": st.st_size,
                                "mtime": st.st_mtime, "link": False})
                return {"entries": out}
        if command == "list_screenshots":
            want = args.get("package")
            shots = [s for s in self.shots if want is None or (s["package"] or "") == want]
            games = {}
            for s in self.shots:
                games.setdefault(s["package"] or "", {"package": s["package"], "title": s["title"], "count": 0})
                games[s["package"] or ""]["count"] += 1
            return {"shots": shots, "total": len(shots), "games": list(games.values())}
        if command == "take_screenshot":  # the Screenshots tab's button: a new shot of the running game
            return self.take_screenshot()
        if command.startswith("room_"):
            return self.room.agent(command, **args)
        if command == "saves_info":  # Restore saves…: what the Frame has now, and the copy from the last restore
            return {"package": args.get("package"), "files": 14, "bytes": 2_310_000, "skipped": [],
                    "running": False, "version": "1.0", "rollback": {"size": 812_000, "time": time.time() - 86400}}
        raise RuntimeError(f"fake Frame: {command} not available")

    @property
    def sftp(self):
        import paramiko

        real = self.real

        class SFTP:
            def listdir_attr(self, path):
                out = []
                for name in sorted(os.listdir(real(path))):
                    a = paramiko.SFTPAttributes.from_stat(os.lstat(os.path.join(real(path), name)))
                    a.filename = name
                    out.append(a)
                return out

            def stat(self, path):
                return paramiko.SFTPAttributes.from_stat(os.stat(real(path)))

            def get(self, remote, local, callback=None):
                import shutil

                shutil.copyfile(remote, local)

            def open(self, path, mode="r"):  # (rooms.export reads the room mesh)
                return open(path, mode)  # noqa: SIM115
        return SFTP()


class FakeTarget:
    """--fake-frame: a pretend Steam Frame with the library's working Quest builds installed (no network, no device),
    for documentation screenshots."""

    def __init__(self, count: int = 8):
        from frameport.frame.connection import parse_target

        self.label = "steamframe"
        self.frame = FakeFS()
        self.target = parse_target("steamos@steamframe.local")
        state = frame_state()
        if state.get("installed"):  # the demo library says what's on the Frame
            sd_games = {i["package"] for i in state["installed"] if i.get("drive") == "sd"}
            self.games = [self._record(g, i.get("sha256")) for i in state["installed"]
                          if (g := library.game(i["package"]))]
        else:
            works = [g for g in library.games() if g.get("kind") != "rift" and (g.get("build") or {}).get("sha256")
                     and (g.get("recipe") or {}).get("status") in ("works", "issues")]
            works.sort(key=lambda g: -(g.get("data_bytes") or 0))
            self.games = [self._record(g) for g in works[:count]]
            sd_games = {self.games[1]["package"]} if len(self.games) > 1 else set()
        # --linux: the AppImage is on the Frame, but SteamOS lacks one of its libraries
        self.games += [{"package": g["package"], "kind": "linux", "title": g.get("title"), "apk_size": 2**26,
                        "apk_present": True, "recipe": None, "exe": g.get("exe"),
                        "missing_libraries": ["libwebkit2gtk-4.1.so.0"]}
                       for g in library.games() if g.get("kind") == "linux" and g["package"] == LINUX_APPIMAGE]
        # GitHub #90: most games on internal storage, some on a microSD card
        sd = {"internal": False, "path": "/run/media/steamos/SD Card", "label": "SD Card"}
        for g in self.games:
            g["drive"] = sd if g["package"] in sd_games else \
                {"internal": True, "path": "/home/steamos", "label": "Internal storage"}
            g["drive_missing"] = False

    @staticmethod
    def _record(g: dict, sha256: str | None = None) -> dict:
        """The agent's list_installed record of a library game (sha256: a different build = "Update on Frame")."""
        rift = g.get("kind") == "rift"
        return {"package": g["package"], "kind": "rift" if rift else "quest", "title": g.get("title"),
                "apk_size": (g.get("data_bytes") or 0) + (0 if rift else 2**28),
                "sha256": sha256 or (g.get("build") or {}).get("sha256"),
                "recipe": {"patches": list((g.get("recipe") or {}).get("patches", []))}}

    def launch(self, pkg: str) -> dict:
        """Play: Steam on the pretend Frame starts the game (the "Starting … put the headset on" toast)."""
        return {"via": "shortcut", "steam": {"result": "started", "lines": []}}

    def drives(self) -> list[dict]:
        sd_games = sum(1 for g in self.games if not g["drive"]["internal"])
        return [{"id": "internal", "path": "/home/steamos", "install_dir": "/home/steamos/Applications/quest-frame",
                 "label": "Internal storage", "fstype": "ext4", "internal": True, "removable": False,
                 "free_bytes": 312 * 2**30, "total_bytes": 460 * 2**30, "usable": True, "reason": "",
                 "games": len(self.games) - sd_games},
                {"id": "/run/media/steamos/SD Card", "path": "/run/media/steamos/SD Card",
                 "install_dir": "/run/media/steamos/SD Card/FramePort", "label": "SD Card", "fstype": "ext4",
                 "internal": False, "removable": True, "free_bytes": 187 * 2**30, "total_bytes": 238 * 2**30,
                 "usable": True, "reason": "", "games": sd_games},
                {"id": "/run/media/steamos/USB", "path": "/run/media/steamos/USB", "install_dir":
                 "/run/media/steamos/USB/FramePort", "label": "USB", "fstype": "exfat", "internal": False,
                 "removable": True, "free_bytes": 50 * 2**30, "total_bytes": 64 * 2**30, "usable": False,
                 "reason": "exfat can't hold game data (no Unix permissions or symlinks); format the drive in "
                           "SteamOS to install games on it", "games": 0}]

    def move(self, package, dest, reporter) -> dict:
        reporter.stage("Move game files")
        for i in range(5):
            reporter.progress(i / 4, f"Copying {i / 2:.1f}/2.0 GiB")
            time.sleep(0.5)
        return {"state": "done", "package": package}

    def set_desktop_entry(self, package, enabled) -> dict:
        for g in self.games:
            if g["package"] == package:
                g["desktop_entry"] = enabled
        return {"enabled": enabled}

    def describe(self) -> dict:
        return {"hostname": "steamframe", "os": "SteamOS", "os_version": "3.8", "build_id": "20260922",
                "free_bytes": 312 * 2**30, "installed": self.games, "lepton": True,
                "proton": {"ready": {"display_name": "Proton 11 (ARM64)"}, "openxr": {"name": "SteamVR"}},
                "kernel_keys": {"keys": 31, "max_keys": 200},
                "battery": {"percent": frame_state().get("battery", 95), "status": "Discharging"}}

    def installed(self) -> list[dict]:
        return self.games

    def close(self) -> None:
        pass


class FakeKeyboardSession:
    """--fake-frame: Type on Frame connects at once (nothing is sent anywhere)."""

    def __init__(self, frame):
        self.closed = False

    def key(self, name, action="down"):
        return True

    def text(self, text):
        return ""

    def close(self):
        self.closed = True


class FakeMonitorSession:
    """--fake-frame: the Monitor tab's stream as synthetic samples, one per second, shaped like a real Quest game on
    the dev Frame (2026-10-07): 72 fps, the game's Android container, SteamVR/Steam as context. Two minutes are sent
    at once first, so the charts are full in screenshots. GAME = --game (else the library's first game)."""

    GAME: str | None = None
    DIP: tuple[float, float] | None = None  # (until, fps): steps hook fps_dip
    PERFECT = 0.0  # until (time.time()): right on 72 fps (steps hook perfect_pacing)
    BATTERY: dict | None = None  # the battery from now on (steps hook battery)
    BACKFILL = 120
    # (name, group, CPU %, GPU %, memory); "GAME" = the game's package
    PROCS = [("GAME", "game", 9.8, 44.0, 1_350_000_000), ("vrcompositor", "steamvr", 1.4, 9.0, 80_000_000),
             ("system_server", "game", 1.1, 0.0, 296_000_000), ("XRServiceLoopTh", "steamvr", 0.5, 0.0, 433_000_000),
             ("steamwebhelper", "steam", 0.4, 0.0, 350_000_000), ("com.android.systemui", "game", 0.1, 0.0,
                                                                   229_000_000),
             ("surfaceflinger", "game", 0.1, 0.4, 96_000_000), ("audioserver", "game", 0.1, 0.0, 31_000_000),
             ("com.android.providers.media.module", "game", 0.1, 0.0, 130_000_000),
             ("com.android.launcher3", "game", 0.0, 0.0, 194_000_000), ("main", "game", 0.0, 0.0, 178_000_000),
             ("com.android.settings", "game", 0.0, 0.0, 172_000_000),
             ("com.android.networkstack.process", "game", 0.0, 0.0, 130_000_000),
             ("android.ext.services", "game", 0.0, 0.0, 111_000_000)] + \
            [("vrwebhelper", "steamvr", 0.3, 0.0, 160_000_000 + i * 9_000_000) for i in range(4)]

    def __init__(self, frame, on_sample, on_end=None):
        import math

        self.math, self.closed, self.on_sample = math, False, on_sample
        self.static = {"agent": 62, "cores": 8, "gpu_max_mhz": 903, "mem_total": 16 * 1024 ** 3, "fan": True,
                       "clusters": [{"cpus": [0, 1], "max_mhz": 2265}, {"cpus": [2, 3, 4], "max_mhz": 3148},
                                    {"cpus": [5, 6], "max_mhz": 2956}, {"cpus": [7], "max_mhz": 3052}],
                       "temp_groups": ["CPU", "GPU", "Memory", "Battery", "NPU", "Power ICs", "Modem", "Camera"]}
        self.t, self.t0 = 0, None
        threading.Thread(target=self._run, daemon=True).start()

    def _sample(self) -> dict:
        m, t = self.math, self.t
        wave = lambda a, p, o=0: a * (1 + m.sin(t / p + o)) / 2  # noqa: E731
        state = frame_state()
        idle = not self.GAME and "running" in state and state["running"] is None  # (a fresh Frame: no game)
        running = self.GAME or state.get("running")
        g = library.game(running) if running else (library.games() or [None])[0]
        game = g or {"package": "com.example.game", "title": "Example Game"}
        pkg = game["package"]
        procs = [{"pid": 570799 + i * 37, "ppid": 1, "name": pkg if n == "GAME" else n,
                  "group": f"game:{pkg}" if grp == "game" else grp, "game": pkg if grp == "game" else None,
                  "cpu": round(c * (0.85 + wave(0.3, 3, i)), 1), "gpu": round(gp * (0.9 + wave(0.2, 5, i)), 1),
                  "rss": r, "age": 1500 + t, "uid": 1000, "critical": grp in ("steamvr", "steam"), "locked": False,
                  "context": grp != "game"}
                 for i, (n, grp, c, gp, r) in enumerate(self.PROCS) if not (idle and grp == "game")]
        if t == 0 or self.t0 is None:
            self.t0 = time.time()
        # the two minutes sent first get exact 1 s steps (the Monitor's numbers are averaged by time: the real clock's
        # jitter between samples made every render of a page differ on slow machines), later samples the real time
        when = self.t0 - (self.BACKFILL - t) if t <= self.BACKFILL else time.time()
        return {
            "t": when, "dt": 1.0, "self_ms": 11.5,
            "cpu": {"total": round(15 + wave(4, 4), 1), "cores": [round(4 + wave(30, 3 + i, i), 1) for i in range(8)],
                    "mhz": [2265, 1996, 1920, 518]},
            "gpu": {"busy": round(52 + wave(8, 5), 1), "mhz": 903},
            "mem": {"total": 15.3 * 1024 ** 3, "avail": int((8.3 - wave(0.15, 9)) * 1024 ** 3),
                    "swap_total": 7.6 * 1024 ** 3, "swap_free": 7.4 * 1024 ** 3},
            "psi": {"cpu": 1.0, "memory": 0.0, "io": 0.1},
            "temps": {"CPU": round(48 + wave(1.5, 11), 1), "GPU": round(46.5 + wave(1, 13), 1), "Memory": 47.1,
                      "Battery": 27.7, "NPU": 47.3, "Power ICs": 37.0, "Modem": 44.2, "Camera": 46.3},
            "zones": {"CPU": {"cpu7-top": 48.4, "cpu0": 44.0}, "GPU": {"gpuss-0": 46.5}},
            "fan": 8500 + int(wave(300, 7)),
            "power": {"system": round(7.3 + wave(0.6, 4), 2), "cpu": 0.85, "gpu": round(1.0 + wave(0.3, 5), 2),
                      "npu": 0.1},
            "battery": {"percent": frame_state().get("battery", 95), "status": "Discharging", "plugged": False,
                        "draining": True, "watts": -1.96, "empty_s": 22440},
            "net": {"wlan0": [int(2000 + wave(4000, 3)), 1000], "wlanap": [0, 0], "usb0": [0, 0]},
            "games": [{"package": pkg, "title": game.get("title") or pkg, "kind": "quest", "appid": 1,
                       "elapsed": 1500 + t, "cpu": 9.4, "gpu": round(44 + wave(6, 5), 1), "mem": 3_390_000_000,
                       "processes": 87, "fps": round(71.6 + wave(0.6, 2) - (9 if t % 41 == 30 else 0), 1),
                       "frame_ms": 0.0}] if not idle else [],
            "procs": procs, "filter": "game"}

    def _run(self) -> None:
        self.t = 0
        while not self.closed and self.t < 100000:
            self.on_sample(self._sample())
            if self.t >= self.BACKFILL:  # the first two minutes at once (full charts), then one per second
                time.sleep(1)
            self.t += 1

    def set_modules(self, m):
        # the hub asks for everything when the Monitor tab subscribes (the Frame card started the stream): send the
        # two minutes again so its charts are full in screenshots too
        if m == "all":
            self.t = 0
        return True

    def set_interval(self, s): pass  # noqa: E704
    def set_filter(self, w): pass  # noqa: E704
    def pause(self, p): pass  # noqa: E704

    def kill(self, pid, sig="TERM", force=False):
        return {"ended": True, "pid": pid}

    def end_game(self, package):
        return {"ended": True, "via": "steam"}

    def close(self):
        self.closed = True


FAKE_INSTALL_STAGES = ["Patching the game", "OVRPort (primary)", "Frame fixes (primary)", "sign (primary)",
                       "validate (primary)", "Prepare Frame", "Upload APK"]


def start_fake_install(app: FramePortApp, package: str) -> None:
    """A running install job of `package` that reports the real stage names up to the upload (62 %) and then waits
    (it never ends: the smoke run exits with it)."""
    from frameport.ui.jobs import Job

    hold, reached = threading.Event(), threading.Event()

    def run(job):
        for stage in FAKE_INSTALL_STAGES:
            job.reporter.stage(stage)
        time.sleep(0.3)  # past the job queue's notification throttle
        job.reporter.progress(0.62, "com.example.apk", speed="36.4 MB/s · ~1 min left")
        reached.set()
        hold.wait()

    app.jobs.submit(Job(f"Install {app._title(package)} on Frame", run, package, "install"))
    reached.wait(10)


def open_type_tab(app: FramePortApp) -> None:
    """The Type on Frame tab, with one key 'pressed' so the screenshot shows it working."""
    app.type_on_frame()
    time.sleep(1.5)
    app.keyboard_view.press("Enter")


def fake_live_stream(app: FramePortApp) -> None:
    """The Live view while streaming: a local ffmpeg test picture through the real relay (no Frame needed). Web mode
    has no in-window player, so it's shown as playing in the browser (the bar's streaming layout)."""
    import shutil
    import subprocess

    from frameport.install.livestream import LiveStream

    view = app.live_view
    if view is None or shutil.which("ffmpeg") is None:
        return
    src = ["ffmpeg", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-c:v", "libx264", "-preset", "ultrafast",
           "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mp4",
           "-movflags", "empty_moov+default_base_moof+frag_keyframe", "-frag_duration", "100000", "-"]
    proc = subprocess.Popen(src, stdout=subprocess.PIPE)
    view.live = LiveStream(lambda: proc.stdout, proc.terminate).start()
    view.mode = "browser"
    view._refresh()


def stop_fake_live(app: FramePortApp) -> None:
    if app.live_view is not None and app.live_view.live is not None:
        app.live_view.stop()


def open_monitor_details(app: FramePortApp) -> None:
    """The Monitor with its details row open."""
    app.go("monitor")
    time.sleep(2)
    if not app.monitor_view.details.visible:
        app.monitor_view.toggle_details()


def open_room(app: FramePortApp) -> None:
    """The Room tab, once its room has loaded and the canvas has drawn it."""
    app.go("room")
    for _ in range(40):
        st = getattr(app, "_room_state", None) or {}
        if st.get("model") is not None or st.get("model_error"):
            break
        time.sleep(0.25)
    time.sleep(1.5)


def start_fake_scan(app: FramePortApp) -> None:
    """Scan room in the headset on the pretend Frame: it reaches 7 of 12 directions and waits there."""
    from frameport.ui import room_state

    app.go("room")
    if app.room_view is not None and app.room_view.root is not None:  # (back to the top after the editing cards)
        app.page.run_task(app.room_view.root.scroll_to, offset=0, duration=0)
    room_state.operate(app, "capture")
    time.sleep(5)  # rooms.scan polls every 2 s


def cancel_fake_scan(app: FramePortApp) -> None:
    from frameport.ui import room_state

    room_state.operate(app, "cancel")
    time.sleep(3)


def show_room_editing(app: FramePortApp, which: str) -> None:
    """The Room tab's editing cards (scrolled into view): cleanup (Clean-up with the key settings), more (+ More
    settings), box (a box selected in the editor), doors (a door added on a wall), suggestion (the Labels card's door
    suggestion taken), applied (other settings applied: the job runs, then the new counts)."""
    view = app.room_view
    if view is None or view.root is None:
        open_room(app)
        view = app.room_view
    if which == "cleanup":
        if view.cleanup.more.visible:
            view.cleanup.toggle_more()
    elif which == "more":
        if not view.cleanup.more.visible:
            view.cleanup.toggle_more()
    elif which == "box":
        if view.cleanup.more.visible:
            view.cleanup.toggle_more()
        view.editor.select(("box", 1))
    elif which == "doors":
        view.editor._add_plane("DOOR_FRAME")
        view.editor._plane_value("along", 0.3)
    elif which == "suggestion":
        view.editor.accept(app.target.frame.room.SUGGESTIONS[0], "door")
    elif which == "applied":
        view.editor.discard()
        view.cleanup.set("walls", "snap")
        view.cleanup._slid("smooth", 4)
        view.cleanup.apply()
        time.sleep(0.6)
        app.page.run_task(view.show_editing)
        time.sleep(0.6)
        return  # (the running clean-up: the next step shows its result)
    elif which == "result":
        time.sleep(6)
    app.page.run_task(view.show_editing)
    time.sleep(1.5)


def show_room_state(app: FramePortApp, which: str) -> None:
    """none: the Room tab without a saved room; disconnected: its not-connected page; frame: the Frame page's room card
    (scrolled into view) with the room back."""
    from frameport.ui import room_state

    fs = app.target.frame if app.target else None
    if which == "none":
        fs.room.saved_text = fs.room.text
        fs.room.text = None
        room_state.state(app, force=True)
        app.go("room")
        time.sleep(2)
    elif which == "disconnected":
        app.frame_state = "none"
        app.go("room")
    elif which == "frame":
        app.frame_state = "connected"
        fs.room.text = getattr(fs.room, "saved_text", None) or fs.room.text
        room_state.state(app, force=True)
        app.go("frame")
        time.sleep(2)
        view = app.switcher.content.content
        try:
            app.page.run_task(view.scroll_to, offset=T_SCROLL_TO_ROOM, duration=0)
        except Exception:  # noqa: BLE001 - only for the picture
            pass


T_SCROLL_TO_ROOM = 520  # the Frame page's room card, below the device card and the readiness checklist


LINUX_APPIMAGE = "linux.venera"  # --linux's packages
LINUX_FOLDER = "linux.matineevr"


def _elf(machine: int = 183, appimage: bool = False, extra: bytes = b"") -> bytes:
    """A minimal ELF header (183 = aarch64): enough for FramePort's Linux app detection."""
    head = bytearray(64)
    head[:4], head[4], head[5] = b"\x7fELF", 2, 1
    if appimage:
        head[8:11] = b"AI\x02"
    head[18:20] = machine.to_bytes(2, "little")
    return bytes(head) + extra


def add_fake_linux_apps() -> None:
    """--linux: an AppImage and a VR app folder (with a helper program, so "Change program…" shows) in the library."""
    from frameport import pipeline

    base = user_data_dir() / "smoke-linux"
    app = base / "MatineeVR"
    (app / "lib").mkdir(parents=True, exist_ok=True)
    (base / "Venera-2.4.2-aarch64.AppImage").write_bytes(_elf(appimage=True, extra=b"\0" * 4096))
    (app / "MatineeVR").write_bytes(_elf(extra=b"\0" * 8192))
    (app / "crashpad_handler").write_bytes(_elf())
    (app / "lib" / "libopenxr_loader.so.1").write_bytes(_elf(extra=b"xrCreateInstance"))
    pipeline.add_linux_app(base / "Venera-2.4.2-aarch64.AppImage")
    pipeline.add_linux_app(app)


def install_fakes(game: str | None, monitor_session=None) -> None:
    """--fake-frame: never reach a real Frame (start-up auto-connect, discovery, the 30 s poll); Type on Frame and
    the Monitor stream are fakes. `game` = the Monitor's game card (with its artwork)."""
    from frameport.frame import keyboard, monitor
    from frameport.install import rooms

    rooms.ensure_scanner = rooms.ensure_prompts = lambda frame: None  # (the pretend Frame has its own scanner)
    keyboard.KeyboardSession = FakeKeyboardSession
    monitor.MonitorSession = monitor_session or FakeMonitorSession
    FakeMonitorSession.GAME = game
    FramePortApp.connect = lambda self, *a, **k: None
    FramePortApp.refresh_frame = lambda self, *a, **k: None
    install_first_run_fakes()


# ------------------------------------------------------------------ first run (the install tutorial)
# The setup command shows placeholders for the address and code (each PC has its own; never this PC's real address).
PAIR_HOST, PAIR_PORT, PAIR_CODE = "<your-PC-address>", 8765, "<one-time-code>"
TOOLS = {"ready": True}  # False: the managed tools look missing until the (pretend) download finishes


class FakePairingServer:
    """The Frame page's setup command without a server: nothing listens and no firewall rule is touched. The
    `pairing_done` step hook plays the Frame running the command (on_paired → the pretend Frame connects)."""

    def __init__(self, on_paired=None, host: str = "", on_ask=None, **_kw):
        # host stays as given ("" = over the network; the Frame page words a set host as the USB-cable setup)
        self.on_paired, self.host, self.on_ask = on_paired, host, on_ask
        self.port, self.code = PAIR_PORT, PAIR_CODE
        self.requests, self.failures, self.paired, self.hint = 1, 0, [], ""  # (requests: no firewall hint)
        self.asks: list = []  # the setup URL: the `frame_asks` step hook plays a Frame asking (Allow / Deny)
        self.running = False

    def open_asks(self) -> list:
        """Like PairingServer.open_asks (requests never expire on film)."""
        return [a for a in self.asks if a.state == "open"]

    def prune(self, now: float | None = None) -> list:
        return []

    def decide(self, ask_id: str, allow: bool) -> None:
        for a in self.asks:
            if a.id == ask_id and a.state == "open":
                a.state = "allowed" if allow else "denied"
        # (the Frame then runs the setup: the storyboard's `pairing_done` hook connects the pretend Frame)

    @property
    def url(self) -> str:
        return f"http://{self.host or PAIR_HOST}:{self.port}"

    @property
    def one_liner(self) -> str:
        return f"curl -fsS {self.host or PAIR_HOST}:{self.port}/{self.code} | bash"

    def start(self):
        self.running = True
        return self

    def stop_soon(self) -> None:
        self.running = False

    def stop(self) -> None:
        self.running = False


def install_first_run_fakes() -> None:
    """Fakes for a first start: no Frame discovery on the real network (a recording must not show a real device),
    the pretend setup command, and the managed tools' state (TOOLS)."""
    from frameport.frame import discovery, pairing
    from frameport.tools import toolchain

    discovery.browse = lambda *a, **k: []
    pairing.PairingServer = FakePairingServer
    pairing.ensure_reachable = lambda server: "ok"
    if not getattr(toolchain.status, "_showcase", False):
        real_status = toolchain.status

        def status(*a, **k):
            out = real_status(*a, **k)
            if not TOOLS["ready"]:
                for s in out:
                    s.installed, s.version = False, None
            return out
        status._showcase = True
        toolchain.status = status


def tools_job(app, pace: float = 1.0, delay: float = 0.0):
    """The welcome screen's "Getting FramePort ready": a pretend download of Java, OVRPort and apksigner through the
    real job queue (app.update_tools while TOOLS["ready"] is False). `delay`: seconds at 0 % first."""
    from frameport.ui.jobs import Job

    def run(job):
        rep = job.reporter
        rep.stage("Downloading tools")
        time.sleep(delay)
        parts = [("Java runtime (Temurin 21)", 48.0), ("OVRPort CLI", 21.0), ("apksigner", 1.6)]
        total = sum(mb for _n, mb in parts)
        done = 0.0
        for name, mb in parts:
            rep.stage(f"Downloading {name}")
            steps = max(3, int(mb / 4))
            for i in range(1, steps + 1):
                got = done + mb * i / steps
                rep.progress(got / total, f"{name}: {mb * i / steps:.1f} of {mb:.1f} MB")
                time.sleep(0.12 * pace)
            done += mb
        TOOLS["ready"] = True
        return "FramePort is ready"
    job = app.jobs.submit(Job("Download tools", run, None, "tools"))
    return job


def scan_job(app, folder: str, entries: list[dict], pace: float = 1.0):
    """Scan a folder: the demo games appear one by one through the real job queue (the folder picker is a native
    dialog a script can't drive; `entries` = the demo library saved aside by demo_home's "fresh" profile)."""
    from frameport.artwork import thumbs

    def run(job):
        rep = job.reporter
        rep.stage("Looking for games")
        for i, e in enumerate(entries, 1):
            rep.progress(i / len(entries), e.get("title") or e["package"])
            library.upsert_game(e["package"], **{k: v for k, v in e.items() if k != "package"})
            try:
                thumbs.prewarm(e["package"])
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.12 * pace)
        library.set_setting("scan.roots", [folder])
        if app.route[0] == "welcome":
            library.set_setting("ui.welcome_done", True)
        return f"{len(entries)} games found in {folder}"
    return app.submit(f"Scan {folder.rstrip('/').rsplit('/', 1)[-1]}", run, None, "scan")


def fake_save_backups(pkg: str) -> None:
    """Two pretend save backups of `pkg` on this PC (yesterday's and last week's), for the Restore saves… dialog."""
    import io
    import json
    import tarfile

    from frameport.install import saves

    d = saves.backups_dir(pkg)
    d.mkdir(parents=True, exist_ok=True)
    for days, n, size in ((1, 14, 2_300_000), (7, 11, 1_900_000)):
        created = time.time() - days * 86400
        path = d / time.strftime("%Y%m%d-%H%M%S.tar.gz", time.localtime(created))
        manifest = {"format": 1, "package": pkg, "created": created, "version": "1.0",
                    "files": [{"path": f"external/files/save{i}.dat", "size": size // n} for i in range(n)]}
        raw = json.dumps(manifest).encode()
        with tarfile.open(path, "w:gz") as tar:
            ti = tarfile.TarInfo(saves.MANIFEST)
            ti.size = len(raw)
            tar.addfile(ti, io.BytesIO(raw))


def attach_fake_frame(app: FramePortApp, monitor_session=None) -> None:
    """Connect `app` to a FakeTarget (the shared monitor stream, used by the Monitor tab and the Frame card, too)."""
    app.monitor_hub.session_factory = monitor_session or FakeMonitorSession
    app.target = FakeTarget()
    app.frame_info, app.frame_state = app.target.describe(), "connected"


class FrozenMonitorSession(FakeMonitorSession):
    """The fake stream for still pictures: the two minutes of history, then nothing new (every render of a page shows
    the same numbers, whenever the screenshot is taken)."""

    # the newest session (the Monitor tab's faster interval starts a new one after the Frame card's) and whether it
    # has replayed its history; the monitor_settled hook waits for that, else a slow machine shot the page halfway
    # through the replay (set_modules("all") asks for the replay again)
    LATEST: FrozenMonitorSession | None = None

    def __init__(self, *args, **kwargs):
        self.settled = threading.Event()
        FrozenMonitorSession.LATEST = self
        super().__init__(*args, **kwargs)

    def _run(self) -> None:
        self.t = 0
        while not self.closed:
            if self.t <= self.BACKFILL:
                self.on_sample(self._sample())
                self.t += 1
            else:
                self.settled.set()
                time.sleep(0.2)

    def set_modules(self, m):
        if m == "all":
            self.settled.clear()
        return super().set_modules(m)


class LiveMonitorSession(FakeMonitorSession):
    """The fake stream, livelier for videos: a sample every 0.6 s and a steady frame rate with a short, shallow dip
    now and then (also in the two minutes sent first), so the card's sparkline (5 s points) has a shape and visibly
    moves without the game looking like it struggles. Seeded: every recording gets the same curve."""

    def _sample(self) -> dict:
        s = super()._sample()
        t = self.t
        rnd = random.Random(t)
        fps = 72 - abs(rnd.gauss(0, 0.6))
        if t % 45 in (20, 21, 22):  # a short, shallow dip now and then
            fps = 66 + 2 * abs(t % 45 - 21) + rnd.uniform(-0.8, 0.8)
        if self.DIP and time.time() < self.DIP[0]:  # (the step hook fps_dip: a stutter on cue)
            fps = self.DIP[1] + rnd.uniform(-0.8, 0.8)
        if time.time() < self.PERFECT:  # (the step hook perfect_pacing: right on target)
            fps = 72 - rnd.uniform(0, 0.2)
        for g in s["games"]:
            g["fps"] = round(fps, 1)
        if self.BATTERY:
            s["battery"] = dict(self.BATTERY)
        return s

    def _run(self) -> None:
        self.t = 0
        while not self.closed and self.t < 100000:
            self.on_sample(self._sample())
            if self.t >= self.BACKFILL:
                time.sleep(0.6)
            self.t += 1

    def set_modules(self, m):
        """Opening the Monitor tab adds modules: on film the stream just goes on, as the real one does (the tab's
        charts start there and grow live). Replaying the two minutes, as the stills' stream does, showed the charts
        racing to catch up."""
        return True


def install_job(package: str, pace: float = 1.0):
    """A pretend install of `package` for the real JobManager (app.submit): every stage the real one reports, an
    upload ticking at a plausible speed, the launch test passing. pace < 1 = faster."""
    def run(job):
        rep = job.reporter
        g = library.game(package) or {}
        title = g.get("title") or package
        rnd = random.Random(7)

        def stage(name, secs, logs=()):
            rep.stage(name)
            for line in logs:
                rep.log(line)
            time.sleep(secs * pace)
        if g.get("kind") == "rift":
            stage("Prepare the game", 1.0)
        else:
            stage("Patching the game", 0.9)
            stage("OVRPort (primary)", 1.2, ["overport patch --version=latest"])
            stage("Frame fixes (primary)", 1.0, list((g.get("recipe") or {}).get("patches") or [])[-3:])
            stage("sign (primary)", 0.7)
            rep.stage("validate (primary)")
            for name in ("APK signature", "64-bit libraries", "OpenXR loader"):
                rep.check(name, True)
                time.sleep(0.25 * pace)
        stage("Prepare Frame", 0.7)
        total = max(g.get("data_bytes") or 0, 1.4e9)
        first, steps = 0.42, 14
        for name, file, lo, hi in (("Upload APK", f"{package}.apk", 0.0, first),
                                   ("Upload data (3 files)", f"main.1.{package}.obb", first, 1.0)):
            rep.stage(name)
            for i in range(1, steps + 1):
                f = lo + (hi - lo) * i / steps
                speed = 34.0 + rnd.uniform(0, 4)
                left = (1 - f) * total / (speed * 1e6)
                rep.progress(f, file, speed=f"{speed:.1f} MB/s · ~{left:.0f} s left" if f < 1 else f"{speed:.1f} MB/s")
                time.sleep(0.22 * pace)
        stage("Artwork for the Steam library", 0.8)
        stage("Finalize install", 0.9)
        stage("Add to Steam library", 0.8)
        stage("Launch test (headless)", 1.8, ["process alive", "OpenXR session created", "frames paced: 72 fps"])
        job.summary = {"verdict": "pass", "findings": []}
        rep.stage("Launch test: passed")
        time.sleep(0.5 * pace)
        return f"{title} is ready on your Frame — put the headset on and start it from your Steam library"
    return run

