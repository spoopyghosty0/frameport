"""'Files on the Frame' — a browsable tree of what an installed game actually has on the Frame (agent `list_files`):
folders with their total size, expandable; a filter box; and files that are missing or incomplete compared to what
was uploaded. The tree is built once from the flat file list; only visible rows are turned into controls."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import flet as ft

from ...errors import explain
from ...i18n import tr, tr_n
from .. import components as C
from .. import theme as T

if TYPE_CHECKING:
    from ..app import FramePortApp

MAX_CHILDREN = 300  # rows shown per folder before "… N more"
MAX_MATCHES = 500


@dataclass
class Node:
    name: str
    path: str = ""
    size: int = 0
    files: int = 0  # files below this folder (1 for a file)
    children: dict[str, Node] = field(default_factory=dict)
    is_dir: bool = True


def folder_note(path: str) -> str:
    """What a well-known entry of an install is (shown next to it, in plain words)."""
    notes = {
        "lepton-app": tr("the installed game (APK and its data)"),
        "lepton-data": tr("saves and settings (the app's Android storage)"),
        "lepton-shaders": tr("shader cache: makes later starts load faster; rebuilt if deleted"),
        "artwork": tr("pictures for the Steam library"),
        "previous-game.apk": tr("the previous version, kept for one rollback"),
        "launch.log": tr("the log of the last start"),
    }
    return notes.get(path, "")


def build_tree(name: str, files: list[list]) -> Node:
    """[[rel, size], ...] → a folder tree with aggregated sizes, children sorted folders-first by name."""
    root = Node(name)
    for rel, size in files:
        parts = rel.split("/")
        node = root
        node.size += size
        node.files += 1
        for i, part in enumerate(parts[:-1]):
            child = node.children.get(part)
            if child is None:
                child = node.children[part] = Node(part, "/".join(parts[:i + 1]))
            child.size += size
            child.files += 1
            node = child
        node.children[parts[-1]] = Node(parts[-1], rel, size, 1, is_dir=False)
    return root


def sorted_children(node: Node) -> list[Node]:
    return sorted(node.children.values(), key=lambda n: (not n.is_dir, n.name.lower()))


def visible_rows(root: Node, expanded: set[str]) -> list[tuple[int, Node | int]]:
    """(depth, node) for everything shown with `expanded` folder paths open; (depth, n) = "… n more"."""
    out: list[tuple[int, Node | int]] = []

    def walk(node: Node, depth: int):
        kids = sorted_children(node)
        for child in kids[:MAX_CHILDREN]:
            out.append((depth, child))
            if child.is_dir and child.path in expanded:
                walk(child, depth + 1)
        if len(kids) > MAX_CHILDREN:
            out.append((depth, len(kids) - MAX_CHILDREN))
    walk(root, 0)
    return out


def matches(files: list[list], text: str) -> list[list]:
    t = text.lower()
    return [f for f in files if t in f[0].lower()][:MAX_MATCHES]


def human(n: int) -> str:
    for unit, div in ((tr("GiB"), 2**30), (tr("MiB"), 2**20), (tr("KiB"), 2**10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def show_files_dialog(app: FramePortApp, package: str, title: str) -> None:
    status = C.meta(tr("Reading the file list from the Frame…"))
    ring = C.spinner()
    body = ft.Column([ft.Row([ring, status],
                             spacing=T.S2)], spacing=T.S3, expand=True)
    dialog = C.dialog(tr("Files on the Frame — {title}").format(title=title), body,
                      [C.ghost(tr("Close"), on_click=lambda e: app.page.pop_dialog())], size="l", height=T.px(560))
    app.page.show_dialog(dialog)

    def load():
        if not app.target:
            raise RuntimeError(tr("The Frame isn't connected"))
        result = app.target.frame.agent("list_files", package=package, timeout=180)
        trees = [(r, build_tree(r["name"], r["files"])) for r in result["roots"]]
        expanded: set[str] = set()
        rows = ft.ListView(spacing=0, expand=True)
        search = C.search(hint_text=tr("Filter files (for example .pak, Binaries)"), expand=True)

        def row(depth: int, node: Node | int, root_key: str) -> ft.Control:
            pad = ft.Padding(T.px(8 + depth * 18), T.px(3), T.px(8), T.px(3))
            if isinstance(node, int):
                return ft.Container(C.meta(tr("… {node} more (use the filter to find them)").format(node=node)),
                                    padding=pad)
            key = f"{root_key}\0{node.path}"
            icon = (ft.Icons.FOLDER_OPEN_ROUNDED if key in expanded else ft.Icons.FOLDER_ROUNDED) if node.is_dir \
                else ft.Icons.INSERT_DRIVE_FILE_ROUNDED
            chevron = ft.Icon(ft.Icons.EXPAND_MORE_ROUNDED if key in expanded else ft.Icons.CHEVRON_RIGHT_ROUNDED,
                              size=T.px(16), color=T.TEXT_3) if node.is_dir else ft.Container(width=T.px(16))
            info = (tr("{human} · {files} files").format(human=human(node.size), files=node.files) if node.is_dir
                    else human(node.size))
            note = folder_note(node.path) if depth == 0 else ""
            return ft.Container(ft.Row([
                chevron, ft.Icon(icon, size=T.px(16), color=T.ACCENT if node.is_dir else T.TEXT_3),
                C.body(node.name, T.TEXT, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Container(C.meta(note, T.TEXT_3, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                             tooltip=note or None, expand=True),
                C.meta(info),
            ], spacing=T.px(6)), padding=pad, border_radius=T.RADIUS_XS, ink=node.is_dir,
                on_click=(lambda e, k=key: toggle(k)) if node.is_dir else None)

        def render():
            text = (search.value or "").strip()
            controls: list[ft.Control] = []
            for r, tree in trees:
                controls.append(ft.Container(ft.Row([
                    C.body(r["name"], T.TEXT, weight=ft.FontWeight.W_600),
                    C.meta(tr("{human} · {files} files").format(human=human(tree.size), files=tree.files)),
                    ft.Container(expand=True),
                    C.icon_btn(ft.Icons.CONTENT_COPY_ROUNDED, tr("Copy path"), lambda e, p=r["path"]: app.copy(p)),
                ], spacing=T.S2), padding=ft.Padding(T.px(8), T.S2, T.px(8), T.px(2))))
                controls.append(ft.Container(C.meta(r["path"], selectable=True),
                                             padding=ft.Padding(T.px(8), 0, T.px(8), T.px(4))))
                if text:
                    found = matches(r["files"], text)
                    controls += [ft.Container(ft.Row([
                        ft.Icon(ft.Icons.INSERT_DRIVE_FILE_ROUNDED, size=T.px(16), color=T.TEXT_3),
                        C.body(rel, T.TEXT, expand=True, selectable=True), C.meta(human(size))], spacing=T.px(6)),
                        padding=ft.Padding(T.px(8), T.px(3), T.px(8), T.px(3))) for rel, size in found]
                    if not found:
                        controls.append(ft.Container(C.meta(tr("No matching files")),
                                                     padding=ft.Padding(T.px(8), T.px(3), T.px(8), T.px(3))))
                else:
                    visible = visible_rows(tree, {k.split("\0", 1)[1] for k in expanded
                                                  if k.split("\0", 1)[0] == r["path"]})
                    controls += [row(d, n, r["path"]) for d, n in visible]
            rows.controls = controls
            C.update(rows)

        def toggle(key: str):
            expanded.symmetric_difference_update({key})
            render()

        search.on_change = lambda e: render()
        head: list[ft.Control] = []
        total = sum(t.size for _, t in trees)
        count = sum(t.files for _, t in trees)
        summary = (tr("{count} files · {human}").format(count=count, human=human(total))
                   + (tr(" (list cut off)") if result.get("truncated") else ""))
        missing = result.get("missing") or []
        if missing:
            lines = "\n".join(f"{rel}: " + ("missing" if actual is None else f"{human(actual)} of {human(size)}")
                              for rel, size, actual in missing[:8])
            more = tr_n("\n… and {n} more", "\n… and {n} more", len(missing) - 8) if len(missing) > 8 else ""
            head.append(C.callout(tr_n("{n} file is missing or incomplete compared to what was uploaded. "
                                       "Install the game again to re-send it.\n{lines}{more}",
                                       "{n} files are missing or incomplete compared to what was uploaded. "
                                       "Install the game again to re-send them.\n{lines}{more}",
                                       len(missing), lines=lines, more=more), "warn"))
        elif result.get("kind") == "pcvr":
            head.append(C.callout(tr("Every uploaded file is present with the right size."), "ok"))
        body.controls = [ft.Row([search, C.meta(summary)], spacing=T.S3), *head, rows]
        render()
        C.update(body)

    def run():
        try:
            load()
        except Exception as exc:  # noqa: BLE001
            status.value = tr("Couldn't read the file list: {exc}").format(exc=explain(exc))
            body.controls = [status]
            C.update(body)
    app.run_bg(run)
