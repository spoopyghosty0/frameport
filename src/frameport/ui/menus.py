"""A game's menu (Library right-click, game page "…") as sections: a pure builder, no controls and no app.

Entries: (label, icon, handler) | Header(label) | None (divider). C.menu_items renders them (headers as small caps,
compact rows). Flet 1.0's PopupMenuItem can't nest, so groups get a header instead of a submenu."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple

import flet as ft

from ..i18n import tr
from . import glyphs as G


class Header(NamedTuple):
    """A group heading inside a menu (not clickable)."""
    label: str


@dataclass
class MenuState:
    """What the menu depends on, gathered by the app (FramePortApp.game_actions)."""
    quick: bool = True             # True: Library right-click; False: the game page's "…" (hero/cards show the rest)
    connected: bool = False        # a Frame is connected
    on_frame: bool = False         # installed on the Frame (current or outdated)
    on_pc: bool = False            # a PC VR game installed on this PC
    job: bool = False              # a job runs (or waits) for this game
    plays: list = field(default_factory=list)     # where it can be started now [(label, icon, handler)]
    installs: list = field(default_factory=list)  # install/update/reinstall options [(label, icon, handler)]
    settings: bool = False         # has Game settings
    programs: bool = False         # "Change program…" makes sense
    selectable: bool = False       # the Library view exists (Select)
    media_button: bool = False     # the game page shows "Add videos" already


def menu_sections(g: dict, st: MenuState, act: Callable[[str], Callable]) -> list:
    """The game's menu. `act(key)` gives the handler for an action key (tests pass `lambda k: k`)."""
    rift = g.get("kind") == "rift"
    linux = g.get("kind") == "linux"  # installed as it is: nothing to analyze, convert or share as a recipe
    quick, job = st.quick, st.job
    top: list = []
    if job and quick:  # the game page shows progress + Cancel in its hero
        top += [(tr("Show progress"), ft.Icons.SYNC_ROUNDED, act("progress")),
                (tr("Cancel"), ft.Icons.CLOSE_ROUNDED, act("cancel"))]
    if quick and not job:
        top += st.plays
    if quick:
        top.append((tr("Open game page"), ft.Icons.OPEN_IN_NEW_ROUNDED, act("open")))
        if st.selectable:
            top.append((tr("Select"), ft.Icons.CHECKLIST_ROUNDED, act("select")))
        if not job:
            top += st.installs
    if st.settings and not job:
        top.append((tr("Game settings…"), ft.Icons.TUNE_ROUNDED, act("settings")))
    if st.connected:
        top.append((tr("Screenshots"), G.SHOT, act("screenshots")))
    if st.on_frame and not rift and not linux and not job and (quick or not st.media_button):
        top.append((tr("Add videos and files"), ft.Icons.VIDEO_LIBRARY_OUTLINED, act("files")))

    artwork = [(tr("Find artwork…"), ft.Icons.IMAGE_SEARCH_ROUNDED, act("find_art")),
               (tr("Use your own artwork…"), ft.Icons.UPLOAD_FILE_OUTLINED, act("custom_art"))]
    if not job and st.on_frame:
        artwork.append((tr("Update Steam art on Frame"), ft.Icons.WALLPAPER_ROUNDED, act("steam_art")))

    recipe = [] if linux else [
        (tr("Reset to suggested recipe…"), ft.Icons.SETTINGS_BACKUP_RESTORE_ROUNDED, act("reset_recipe")),
        (tr("Save as known-good recipe"), G.RECIPE, act("save_recipe")),
        (tr("Share working recipe…"), ft.Icons.SHARE_ROUNDED, act("share"))]

    trouble: list = []
    if quick and not job and st.on_frame:  # the game page has these on its "Where it's installed" cards
        trouble.append((tr("Run launch test on Frame"), G.TEST, act("test_frame")))
    if quick and not job and st.on_pc:
        trouble.append((tr("Run launch test on this PC"), G.TEST, act("test_pc")))
    if not job and not rift and not linux:
        trouble.append((tr("Analyze again"), ft.Icons.MANAGE_SEARCH_ROUNDED, act("analyze")))
    if not job and not linux:
        trouble.append((tr("Check game files") if rift else tr("Rebuild only (no install)"), G.PORT, act("build")))
    if st.programs:
        trouble.append((tr("Change program…"), ft.Icons.SWAP_HORIZ_ROUNDED, act("program")))
    trouble += [(tr("Refresh store details"), ft.Icons.PUBLISHED_WITH_CHANGES_ROUNDED, act("details")),
                (tr("Collect logs"), ft.Icons.FOLDER_ZIP_OUTLINED, act("logs")),
                (tr("Report a problem…"), ft.Icons.BUG_REPORT_OUTLINED, act("report"))]

    danger: list = []
    if quick and not job and st.on_frame:
        danger.append((tr("Uninstall from Frame…"), ft.Icons.DELETE_OUTLINE_ROUNDED, act("uninstall_frame")))
    if quick and not job and st.on_pc:
        danger.append((tr("Uninstall from this PC…"), ft.Icons.DELETE_OUTLINE_ROUNDED, act("uninstall_pc")))
    danger.append((tr("Remove from library…"), ft.Icons.REMOVE_CIRCLE_OUTLINE_ROUNDED, act("remove")))

    out: list = list(top)
    for head, items in ((tr("Artwork"), artwork), (tr("Recipe"), recipe), (tr("Troubleshoot"), trouble)):
        if items:
            out += [None, Header(head), *items]
    return out + [None, *danger]


def process_menu(p: dict, act: Callable[[str], Callable]) -> list:
    """Monitor's right-click menu for a process row or a group row (`p` = the row). Harmless items first, then
    End…/Force quit…, End game… last; a process SteamOS protects shows that instead of the End items."""
    out: list = []
    if p.get("kind") == "group":
        n = p.get("count", 0)
        out += [(tr("Hide its processes") if p.get("expanded") else tr("Show its processes"),
                 ft.Icons.UNFOLD_MORE_ROUNDED, act("toggle")),
                (tr("Copy name"), ft.Icons.CONTENT_COPY_ROUNDED, act("copy_name")), None]
        if p.get("locked"):
            out.append((tr("Protected by SteamOS"), ft.Icons.LOCK_OUTLINE_ROUNDED, None))
        else:
            out += [(tr("End all {n}…").format(n=n), ft.Icons.LOGOUT_ROUNDED, act("term")),
                    (tr("Force quit all {n}…").format(n=n), ft.Icons.DANGEROUS_OUTLINED, act("kill"))]
    else:
        out += [(tr("Copy name"), ft.Icons.CONTENT_COPY_ROUNDED, act("copy_name")),
                (tr("Copy PID {pid}").format(pid=p.get("pid")), ft.Icons.CONTENT_COPY_ROUNDED, act("copy_pid")), None]
        if p.get("locked"):
            out.append((tr("Protected by SteamOS"), ft.Icons.LOCK_OUTLINE_ROUNDED, None))
        else:
            out += [(tr("End…"), ft.Icons.LOGOUT_ROUNDED, act("term")),
                    (tr("Force quit…"), ft.Icons.DANGEROUS_OUTLINED, act("kill"))]
    if p.get("game"):
        out += [None, (tr("End game…"), ft.Icons.STOP_CIRCLE_OUTLINED, act("end_game"))]
    return out
