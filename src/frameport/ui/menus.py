"""A game's menu (Library right-click, game page "…") and the other context menus: pure builders, no controls and
no app.

Entries: (label, icon, handler) | Item(label, icon, handler, danger) | Submenu(label, icon, entries) | Header(label)
| None (divider). ui/popmenu.PopMenu draws them (FramePort's own menu, with hover submenus); C.menu_items renders
the flat ones for Flet's stock popup menus (filter chips, Add games)."""
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


class Item(NamedTuple):
    """A menu item that needs more than (label, icon, handler): `danger` = drawn as destructive (red icon)."""
    label: str
    icon: object
    handler: Callable | None
    danger: bool = False


class Submenu(NamedTuple):
    """A group that opens to the side (hover, click, or → on the keyboard)."""
    label: str
    icon: object
    entries: list


def group(label: str, icon, items: list) -> list:
    """A submenu for two or more items; a single item stays in the menu as it is, none gives nothing."""
    if len(items) > 1:
        return [Submenu(label, icon, items)]
    return list(items)


@dataclass
class MenuState:
    """What the menu depends on, gathered by the app (FramePortApp.game_actions)."""
    quick: bool = True             # True: Library right-click; False: the game page's "…" (its hero has the rest)
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
    android: bool = False          # a Quest/Android game (not PC VR, not a Linux app): it can get a newer APK
    new_version: bool = False      # a Linux app or PC VR game: it can get a newer version (file or folder)
    saves: bool = False            # a Quest/Android game installed on the connected Frame: back up / restore saves
    link: bool = False             # installed from an install link that can be fetched again: Check for updates


def menu_sections(g: dict, st: MenuState, act: Callable[[str], Callable]) -> list:
    """The game's menu, the same structure in the Library (right-click, quick=True) and on the game page ("…",
    quick=False, which leaves out what the page's hero has: play/install, its page, Select, Add videos):

        Play / Install-Update / Game settings… | Open game page / Select | Update ▸ / On the Frame ▸ / Saves ▸ /
        Name and artwork ▸ / Recipe ▸ / Troubleshoot ▸ | Uninstall… / Remove from library…

    A group with a single item shows that item instead of a submenu (`group`). `act(key)` gives the handler for an
    action key (tests pass `lambda k: k`)."""
    rift = g.get("kind") == "rift"
    linux = g.get("kind") == "linux"  # installed as it is: nothing to analyze, convert or share as a recipe
    job, quick = st.job, st.quick
    out: list = []
    if job:  # the game's install/test runs: its progress first (the page's hero shows it already)
        if quick:
            out += [(tr("Show progress"), ft.Icons.SYNC_ROUNDED, act("progress")),
                    (tr("Cancel"), ft.Icons.CLOSE_ROUNDED, act("cancel"))]
    elif quick:
        out += [*st.plays, *st.installs]
    if st.settings and not job:
        out.append((tr("Game settings…"), ft.Icons.TUNE_ROUNDED, act("settings")))
    if quick:
        out.append(None)
        out.append((tr("Open game page"), ft.Icons.OPEN_IN_NEW_ROUNDED, act("open")))
        if st.selectable:
            out.append((tr("Select"), ft.Icons.CHECKLIST_ROUNDED, act("select")))

    update: list = []
    if st.android and not job:  # GitHub #161: the game keeps its name, art and recipe
        update.append((tr("Update with a newer APK…"), ft.Icons.UPGRADE_ROUNDED, act("new_apk")))
    if st.new_version and not job:  # the same for Linux apps and PC VR games
        update.append((tr("Update with a newer version…"), ft.Icons.UPGRADE_ROUNDED, act("new_version")))
    if st.on_frame and not rift and not linux and not job:
        # the new build only; the Frame's game data stays (e.g. changed OBBs a normal update would replace)
        update.append((tr("Update app only…"), ft.Icons.SYSTEM_UPDATE_ALT_ROUNDED, act("update_app")))
    if st.link and not job:  # ask the install link now instead of waiting for the 6-hourly check
        update.append((tr("Check for updates"), ft.Icons.REFRESH_ROUNDED, act("check_link")))

    frame: list = []
    if st.connected:
        frame.append((tr("Screenshots"), G.SHOT, act("screenshots")))
    if st.on_frame and not rift and not linux and not job and (quick or not st.media_button):
        frame.append((tr("Add videos and files"), ft.Icons.VIDEO_LIBRARY_OUTLINED, act("files")))
    if st.movable and st.on_frame and not job:
        frame.append((tr("Move to…"), ft.Icons.DRIVE_FILE_MOVE_ROUNDED, act("move")))

    saves: list = []  # GitHub discussion #352: Steam doesn't sync Quest games' saves
    if st.saves and not job:
        saves += [(tr("Back up saves"), ft.Icons.BACKUP_ROUNDED, act("backup_saves")),
                  (tr("Restore saves…"), ft.Icons.RESTORE_ROUNDED, act("restore_saves"))]
    if st.android:
        saves.append((tr("Open backups folder"), ft.Icons.FOLDER_OPEN_ROUNDED, act("saves_folder")))

    artwork = [(tr("Rename…"), ft.Icons.DRIVE_FILE_RENAME_OUTLINE_OUTLINED, act("rename")),
               (tr("Find artwork…"), ft.Icons.IMAGE_SEARCH_ROUNDED, act("find_art")),
               (tr("Use your own artwork…"), ft.Icons.UPLOAD_FILE_OUTLINED, act("custom_art"))]
    if not job and st.on_frame:
        artwork.append((tr("Update art on Frame"), ft.Icons.WALLPAPER_ROUNDED, act("steam_art")))

    trouble: list = []  # (launch tests and uninstalls are also on the page's "Where it's installed" cards)
    if st.on_frame and not job:
        trouble.append((tr("Run launch test on Frame"), G.TEST, act("test_frame")))
    if st.on_pc and not job:
        trouble.append((tr("Run launch test on this PC"), G.TEST, act("test_pc")))
    if not job and not rift and not linux:
        trouble.append((tr("Analyze again"), ft.Icons.MANAGE_SEARCH_ROUNDED, act("analyze")))
    if not job and not linux:
        trouble.append((tr("Check game files") if rift else tr("Rebuild only"), G.PORT, act("build")))
    if st.programs:
        trouble.append((tr("Change program…"), ft.Icons.SWAP_HORIZ_ROUNDED, act("program")))
    trouble += [(tr("Refresh store details"), ft.Icons.PUBLISHED_WITH_CHANGES_ROUNDED, act("details")),
                (tr("Collect logs"), ft.Icons.FOLDER_ZIP_OUTLINED, act("logs")),
                (tr("Report a problem…"), ft.Icons.BUG_REPORT_OUTLINED, act("report"))]

    out.append(None)
    out += group(tr("Update"), ft.Icons.UPGRADE_ROUNDED, update)
    out += group(tr("On the Frame"), G.FRAME, frame)
    out += group(tr("Saves"), ft.Icons.SAVE_ROUNDED, saves)
    out += group(tr("Name and artwork"), ft.Icons.IMAGE_OUTLINED, artwork)
    out += group(tr("Recipe"), G.RECIPE, recipe_actions(g, act))
    out += group(tr("Troubleshoot"), ft.Icons.BUILD_ROUNDED, trouble)

    out.append(None)
    if st.on_frame and not job:
        out.append(Item(tr("Uninstall from Frame…"), ft.Icons.DELETE_OUTLINE_ROUNDED, act("uninstall_frame"), True))
    if st.on_pc and not job:
        out.append(Item(tr("Uninstall from this PC…"), ft.Icons.DELETE_OUTLINE_ROUNDED, act("uninstall_pc"), True))
    out.append(Item(tr("Remove from library…"), ft.Icons.REMOVE_CIRCLE_OUTLINE_ROUNDED, act("remove"), True))
    return out


def labels(entries: list) -> list[str]:
    """Every clickable item's label, submenus included (depth first): what a menu can do."""
    out: list[str] = []
    for e in entries:
        if isinstance(e, Submenu):
            out += labels(e.entries)
        elif e is not None and not isinstance(e, Header):
            out.append(e[0])
    return out


# ui/help.py keys of recipe_actions, in order
RECIPE_HELP = ("reset_recipe", "pick_recipe", "save_recipe", "share_config")


def recipe_actions(g: dict, act: Callable[[str], Callable]) -> list:
    """The recipe's own actions: the menus' Recipe ▸ and buttons on the game page's recipe card ("What FramePort will
    do"). Linux apps are installed as they are and have no recipe."""
    if g.get("kind") == "linux":
        return []
    return [(tr("Reset to suggested recipe…"), ft.Icons.SETTINGS_BACKUP_RESTORE_ROUNDED, act("reset_recipe")),
            (tr("Use a recipe from the catalog…"), ft.Icons.MANAGE_SEARCH_ROUNDED, act("pick_recipe")),
            (tr("Save as known-good recipe"), G.RECIPE, act("save_recipe")),
            (tr("Share working recipe…"), ft.Icons.SHARE_ROUNDED, act("share"))]


def selection_menu(n: int, pc: int, connected: bool, pc_available: bool, act: Callable[[str], Callable]) -> list:
    """The Library's right-click menu on one of several selected games: what the selection bar does, for all of them
    (C.menu_targets). `pc` = how many of them are PC VR games (installable on this PC). Install on Frame is shown
    disabled (handler None) while no Frame is connected."""
    out: list = [(tr("Install {n} on Frame").format(n=n), G.FRAME, act("install_frame") if connected else None)]
    if pc:
        out.append((tr("Install {len} on this PC").format(len=pc), G.PC, act("install_pc") if pc_available else None))
    return out + [None, (tr("Select all shown"), ft.Icons.CHECKLIST_ROUNDED, act("select_all")),
                  (tr("Clear selection"), ft.Icons.CLOSE_ROUNDED, act("clear"))]


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
