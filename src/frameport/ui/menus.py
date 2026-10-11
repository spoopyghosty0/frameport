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
    quick: bool = True             # True: Library right-click (quick_menu); False: the game page's full "…" menu
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
    movable: bool = False          # the Frame knows which drive it's on (agent v63+, microSD): Move to…


def menu_sections(g: dict, st: MenuState, act: Callable[[str], Callable]) -> list:
    """The game's menu. `act(key)` gives the handler for an action key (tests pass `lambda k: k`)."""
    rift = g.get("kind") == "rift"
    linux = g.get("kind") == "linux"  # installed as it is: nothing to analyze, convert or share as a recipe
    if st.quick:
        return quick_menu(st, act)
    job = st.job
    top: list = []
    if st.settings and not job:
        top.append((tr("Game settings…"), ft.Icons.TUNE_ROUNDED, act("settings")))
    if st.connected:
        top.append((tr("Screenshots"), G.SHOT, act("screenshots")))
    if st.on_frame and not rift and not linux and not job and not st.media_button:
        top.append((tr("Add videos and files"), ft.Icons.VIDEO_LIBRARY_OUTLINED, act("files")))
    if st.movable and st.on_frame and not job:
        top.append((tr("Move to…"), ft.Icons.DRIVE_FILE_MOVE_ROUNDED, act("move")))

    artwork = [(tr("Rename…"), ft.Icons.DRIVE_FILE_RENAME_OUTLINE_OUTLINED, act("rename")),
               (tr("Find artwork…"), ft.Icons.IMAGE_SEARCH_ROUNDED, act("find_art")),
               (tr("Use your own artwork…"), ft.Icons.UPLOAD_FILE_OUTLINED, act("custom_art"))]
    if not job and st.on_frame:
        artwork.append((tr("Update art on Frame"), ft.Icons.WALLPAPER_ROUNDED, act("steam_art")))

    recipe = [] if linux else [
        (tr("Reset to suggested recipe…"), ft.Icons.SETTINGS_BACKUP_RESTORE_ROUNDED, act("reset_recipe")),
        (tr("Save as known-good recipe"), G.RECIPE, act("save_recipe")),
        (tr("Share working recipe…"), ft.Icons.SHARE_ROUNDED, act("share"))]

    trouble: list = []  # launch tests and uninstalls are on the page's "Where it's installed" cards
    if not job and not rift and not linux:
        trouble.append((tr("Analyze again"), ft.Icons.MANAGE_SEARCH_ROUNDED, act("analyze")))
    if not job and not linux:
        trouble.append((tr("Check game files") if rift else tr("Rebuild only"), G.PORT, act("build")))
    if st.programs:
        trouble.append((tr("Change program…"), ft.Icons.SWAP_HORIZ_ROUNDED, act("program")))
    trouble += [(tr("Refresh store details"), ft.Icons.PUBLISHED_WITH_CHANGES_ROUNDED, act("details")),
                (tr("Collect logs"), ft.Icons.FOLDER_ZIP_OUTLINED, act("logs")),
                (tr("Report a problem…"), ft.Icons.BUG_REPORT_OUTLINED, act("report"))]

    danger = [(tr("Remove from library…"), ft.Icons.REMOVE_CIRCLE_OUTLINE_ROUNDED, act("remove"))]

    out: list = list(top)
    for head, items in ((tr("Name and artwork"), artwork), (tr("Recipe"), recipe), (tr("Troubleshoot"), trouble)):
        if items:
            out += [None, Header(head), *items]
    return out + [None, *danger]


def quick_menu(st: MenuState, act: Callable[[str], Callable]) -> list:
    """The Library's right-click menu: only the key actions, no section headers. "More actions…" opens the game
    page's full "…" menu (menu_sections with quick=False)."""
    opened = (tr("Open game page"), ft.Icons.OPEN_IN_NEW_ROUNDED, act("open"))
    if st.job:
        return [(tr("Show progress"), ft.Icons.SYNC_ROUNDED, act("progress")),
                (tr("Cancel"), ft.Icons.CLOSE_ROUNDED, act("cancel")), opened]
    out: list = [*st.plays[:1], opened]
    if st.selectable:
        out.append((tr("Select"), ft.Icons.CHECKLIST_ROUNDED, act("select")))
    out += st.installs[:1]
    if st.settings:
        out.append((tr("Game settings…"), ft.Icons.TUNE_ROUNDED, act("settings")))
    out.append((tr("Rename…"), ft.Icons.DRIVE_FILE_RENAME_OUTLINE_OUTLINED, act("rename")))
    out += [None, (tr("More actions…"), ft.Icons.MORE_HORIZ_ROUNDED, act("more")), None]
    if st.on_frame:
        out.append((tr("Uninstall from Frame…"), ft.Icons.DELETE_OUTLINE_ROUNDED, act("uninstall_frame")))
    if st.on_pc:
        out.append((tr("Uninstall from this PC…"), ft.Icons.DELETE_OUTLINE_ROUNDED, act("uninstall_pc")))
    return out + [(tr("Remove from library…"), ft.Icons.REMOVE_CIRCLE_OUTLINE_ROUNDED, act("remove"))]


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
