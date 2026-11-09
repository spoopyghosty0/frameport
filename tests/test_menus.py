"""Menu structure: game menus (Library right-click, game page "…"), the selection and Monitor process menus, and
menu_items (Flet's stock popup menus)."""
import flet as ft

from frameport.ui import components as C
from frameport.ui import theme as T
from frameport.ui.menus import (
    RECIPE_HELP,
    Header,
    Item,
    MenuState,
    Submenu,
    group,
    labels,
    menu_sections,
    process_menu,
    recipe_actions,
    selection_menu,
)

DESTRUCTIVE = {"Uninstall from Frame…", "Uninstall from this PC…", "Remove from library…"}
QUEST = {"package": "com.q", "title": "Q"}
RIFT = {"package": "rift.r", "kind": "rift", "title": "R"}
LINUX = {"package": "linux.l", "kind": "linux", "title": "L"}
HERO = {"Open game page", "Select", "Show progress", "Cancel"}  # what the page's hero has instead


def _key(k):
    return k


def _play(where="Frame"):
    return (f"Play on {where}", ft.Icons.PLAY_ARROW_ROUNDED, f"play_{where}")


def _install(label="Reinstall on Frame"):
    return (label, "fp:frame", "install")


def _states():
    """(name, game, state) for the cases the menus have to handle."""
    return [
        ("quest on Frame", QUEST, MenuState(connected=True, on_frame=True, plays=[_play()], installs=[_install()],
                                            settings=True, selectable=True, android=True, saves=True)),
        ("quest not installed", QUEST, MenuState(connected=True, installs=[_install("Install on Frame")],
                                                 settings=True, selectable=True, android=True)),
        ("rift on PC", RIFT, MenuState(on_pc=True, plays=[_play("this PC")], programs=True, selectable=True,
                                       installs=[_install("Reinstall on this PC")], new_version=True)),
        ("linux on Frame", LINUX, MenuState(connected=True, on_frame=True, plays=[_play()], installs=[_install()],
                                            selectable=True, new_version=True)),
        ("linux job running", LINUX, MenuState(connected=True, on_frame=True, job=True, selectable=True,
                                               new_version=True)),
        ("job running", QUEST, MenuState(connected=True, on_frame=True, job=True, settings=True, selectable=True,
                                         android=True)),
    ]


def _menus(g, st):
    """(right-click menu, page "…" menu) for one state."""
    st.quick = True
    quick = menu_sections(g, st, _key)
    st.quick = False
    page = menu_sections(g, st, _key)
    return quick, page


def _top(entries):
    return [e[0] for e in entries if e is not None and not isinstance(e, Header)]


def _group_of(entries, label):
    """The submenu an item is in (None = the top level)."""
    for e in entries:
        if isinstance(e, Submenu) and label in labels(e.entries):
            return e.label
        if e is not None and not isinstance(e, (Header, Submenu)) and e[0] == label:
            return None
    raise AssertionError(f"{label} not in menu")


def _submenus(entries):
    out = []
    for e in entries:
        if isinstance(e, Submenu):
            out += [e, *_submenus(e.entries)]
    return out


def test_library_menu_has_the_common_actions_first_then_the_groups():
    """GitHub #414: Play, Install/Update, Game settings | the page, Select | grouped submenus | uninstall, remove."""
    for name, g, st in _states():
        quick, _ = _menus(g, st)
        top = _top(quick)
        if st.job:
            assert top[:2] == ["Show progress", "Cancel"], name
        else:
            first = [p[0] for p in st.plays] + [i[0] for i in st.installs] + \
                (["Game settings…"] if st.settings else [])
            assert top[:len(first)] == first, (name, top)
            assert quick[len(first)] is None, name
        assert "Open game page" in top and "Select" in top and top.index("Select") == top.index("Open game page") + 1
        # the destructive items close the menu, after a divider, drawn as such
        n = sum(1 for lbl in top if lbl in DESTRUCTIVE)
        assert top[-n:] == [lbl for lbl in top if lbl in DESTRUCTIVE] and top[-1] == "Remove from library…"
        assert quick[-n - 1] is None
        assert all(isinstance(e, Item) and e.danger for e in quick[-n:]), name
        assert ("Uninstall from Frame…" in top) == (st.on_frame and not st.job)
        assert ("Uninstall from this PC…" in top) == (st.on_pc and not st.job)
        assert not any(isinstance(e, Header) for e in quick), name  # groups are submenus, not headings
        assert "More actions…" not in labels(quick)
        assert len(top) <= 15, (name, top)


def test_everything_is_in_the_right_click_menu_and_the_page_menu_has_the_same_structure():
    """The page's "…" is the same menu without what its hero shows (play/install, the page itself, Select,
    progress); the right-click menu reaches everything again."""
    for name, g, st in _states():
        quick, page = _menus(g, st)
        hero = HERO | {p[0] for p in st.plays} | {i[0] for i in st.installs}
        assert [x for x in labels(quick) if x not in hero] == labels(page), name
        assert [e.label for e in _submenus(quick)] == [e.label for e in _submenus(page)], name
        for gone in HERO:
            assert gone not in labels(page), (name, gone)
        assert "Rename…" in labels(page), name  # every kind of game, also while a job runs


def test_groups():
    for name, g, st in _states():
        for entries in _menus(g, st):
            for sub in _submenus(entries):
                assert len(sub.entries) >= 2, (name, sub.label)  # a lone item isn't hidden in a submenu
                assert sub.icon and all(e[1] for e in sub.entries if e is not None)
    one = ("A", "i", "a")
    assert group("G", "i", []) == [] and group("G", "i", [one]) == [one]
    assert group("G", "i", [one, one]) == [Submenu("G", "i", [one, one])]

    quick, page = _menus(*_states()[0][1:])
    for menu in (quick, page):
        assert [e.label for e in _submenus(menu)] == ["Update", "On the Frame", "Saves", "Name and artwork",
                                                      "Recipe", "Troubleshoot"]
        assert _group_of(menu, "Analyze again") == "Troubleshoot"
        assert _group_of(menu, "Run launch test on Frame") == "Troubleshoot"
        assert _group_of(menu, "Update art on Frame") == "Name and artwork"
        assert _group_of(menu, "Rename…") == "Name and artwork"
        assert _group_of(menu, "Back up saves") == "Saves"
        assert _group_of(menu, "Update with a newer APK…") == "Update"
        assert _group_of(menu, "Screenshots") == "On the Frame"
        assert _group_of(menu, "Game settings…") is None
        for e in recipe_actions(QUEST, _key):
            assert _group_of(menu, e[0]) == "Recipe"
    _, linux = _menus(*_states()[3][1:])
    assert "Recipe" not in [e.label for e in _submenus(linux)]
    _, rift = _menus(*_states()[2][1:])
    assert {"Check game files", "Change program…", "Run launch test on this PC"} <= set(labels(rift))
    assert "Analyze again" not in labels(rift)
    reset = next(e for e in recipe_actions(QUEST, _key) if e[0] == "Reset to suggested recipe…")
    assert reset[1] != ft.Icons.RESTART_ALT_ROUNDED  # not the Frame's Restart icon


def test_library_menu_on_a_selection_acts_on_all_of_it():
    got = [e[0] if e else None for e in selection_menu(3, 1, True, True, _key)]
    assert got == ["Install 3 on Frame", "Install 1 on this PC", None, "Select all shown", "Clear selection"]
    keys = [e[2] for e in selection_menu(3, 0, False, True, _key) if e]
    assert keys[0] is None  # no Frame: shown, but disabled
    assert "install_pc" not in keys  # no PC VR game in the selection
    assert [e[2] for e in selection_menu(2, 2, True, False, _key) if e][1] is None  # no Windows: disabled


def test_recipe_actions_are_for_games_with_a_recipe():
    assert [e[2] for e in recipe_actions(QUEST, _key)] == ["reset_recipe", "pick_recipe", "save_recipe", "share"]
    assert [e[0] for e in recipe_actions(RIFT, _key)][0] == "Reset to suggested recipe…"
    assert recipe_actions(LINUX, _key) == []
    assert len(RECIPE_HELP) == len(recipe_actions(QUEST, _key))
    from frameport.ui.help import HELP
    assert all(HELP[k] for k in RECIPE_HELP)


def test_update_app_only_for_quest_games_on_the_frame():
    """Discussion #180: the new build without replacing game data changed on the Frame (e.g. a modded OBB)."""
    for name, g, st in _states():
        for menu in _menus(g, st):
            assert ("Update app only…" in labels(menu)) == (name == "quest on Frame"), name


def test_newer_versions_in_both_menus():
    """GitHub #161: Quest games get "Update with a newer APK…", Linux apps and PC VR games "Update with a newer
    version…" (a file or folder); never while a job runs."""
    for name, g, st in _states():
        for menu in _menus(g, st):
            assert ("Update with a newer APK…" in labels(menu)) == (g is QUEST and not st.job), name
            assert ("Update with a newer version…" in labels(menu)) == (g is not QUEST and not st.job), name


def test_save_backups_for_quest_games():
    """Discussion #352: back up / restore a Quest game's saves (installed on the Frame, no job running); the backups
    folder for every Quest game; nothing for PC VR games and Linux apps."""
    for name, g, st in _states():
        for menu in _menus(g, st):
            got = labels(menu)
            live = g is QUEST and st.saves and not st.job
            assert ("Back up saves" in got) == live, name
            assert ("Restore saves…" in got) == live, name
            assert ("Open backups folder" in got) == (g is QUEST), name


def test_check_for_updates_for_games_from_install_links():
    """Games installed from an install link get "Check for updates" (asks the link now); never while a job runs,
    never for other games."""
    for name, g, st in _states():
        assert not any("Check for updates" in labels(m) for m in _menus(g, st)), name
        st.link = True
        for menu in _menus(g, st):
            assert ("Check for updates" in labels(menu)) == (not st.job), name


def test_menu_rows_wrap_instead_of_running_past_the_edge():
    """GitHub #414 (Flet's stock menus: filter chips, Add games): Flet's own icon row gave a long label no width
    limit; ours lets it take the rest and wrap."""
    item = C.menu_items([("Update with a newer APK…", ft.Icons.UPGRADE_ROUNDED, lambda e: None)])[0]
    assert item.icon is None
    row = item.content
    assert isinstance(row, ft.Row) and len(row.controls) == 2
    text = row.controls[1]
    assert isinstance(text, ft.Text) and text.expand and text.value == "Update with a newer APK…"
    assert text.max_lines is None and text.no_wrap is not True
    plain = C.menu_items([("Plain", None, lambda e: None)])[0].content
    assert plain.controls[0].expand
    disabled = C.menu_items([("Protected", ft.Icons.LOCK_OUTLINE_ROUNDED, None)])[0]
    assert disabled.disabled and disabled.content.controls[1].color == T.TEXT_3
    danger = C.menu_items([Item("Remove", ft.Icons.DELETE_OUTLINE_ROUNDED, lambda e: None, True)])[0]
    assert danger.content.controls[1].value == "Remove"


def test_menu_buttons_widen_with_the_ui_scale():
    try:
        T.set_scale(1.5)
        wide = C.menu_button([]).size_constraints
        assert wide.max_width == T.px(C.MENU_MAX_W + 40) > C.MENU_MAX_W * 1.5
    finally:
        T.set_scale(1.0)
    assert C.menu_button([]).size_constraints.max_width == C.MENU_MAX_W + 40


def test_menu_entries_trim_headers_and_dividers():
    act = ("A", ft.Icons.ADD_ROUNDED, None)
    raw = [None, Header("Empty"), None, Header("Kept"), act, None, None, Header("Last")]
    assert C.menu_entries(raw) == [Header("Kept"), act]
    items = C.menu_items([act, None, Header("H"), act])
    assert items[0].height == items[3].height == T.px(32) and items[2].height == T.px(24)
    assert items[1].content is None  # divider
    assert items[2].disabled and items[0].disabled  # no handler: shown disabled


def test_process_menu():
    proc = {"name": "x", "pid": 5, "game": "com.q"}
    got = [e[0] if e else "—" for e in process_menu(proc, _key)]
    assert got == ["Copy name", "Copy PID 5", "—", "End…", "Force quit…", "—", "End game…"]
    locked = process_menu({**proc, "locked": True}, _key)
    prot = next(e for e in locked if e and e[0] == "Protected by SteamOS")
    assert prot[2] is None and "End…" not in [e[0] for e in locked if e]
    grp = [e[0] if e else "—" for e in process_menu({"kind": "group", "count": 3, "name": "g"}, _key)]
    assert grp == ["Show its processes", "Copy name", "—", "End all 3…", "Force quit all 3…"]
    end = next(e for e in process_menu(proc, _key) if e and e[0] == "End…")
    assert end[1] != ft.Icons.CLOSE_ROUNDED
