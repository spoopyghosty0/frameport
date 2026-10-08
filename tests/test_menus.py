"""Menu structure: game menus (Library right-click, game page "…"), the Monitor process menu and menu_items."""
import flet as ft

from frameport.ui import components as C
from frameport.ui import theme as T
from frameport.ui.menus import Header, MenuState, menu_sections, process_menu

DESTRUCTIVE = {"Uninstall from Frame…", "Uninstall from this PC…", "Remove from library…"}
QUEST = {"package": "com.q", "title": "Q"}
RIFT = {"package": "rift.r", "kind": "rift", "title": "R"}
LINUX = {"package": "linux.l", "kind": "linux", "title": "L"}


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
                                            settings=True, selectable=True)),
        ("quest not installed", QUEST, MenuState(connected=True, installs=[_install("Install on Frame")],
                                                 settings=True, selectable=True)),
        ("rift on PC", RIFT, MenuState(on_pc=True, plays=[_play("this PC")], programs=True, selectable=True,
                                       installs=[_install("Reinstall on this PC")])),
        ("linux on Frame", LINUX, MenuState(connected=True, on_frame=True, plays=[_play()], installs=[_install()],
                                            selectable=True)),
        ("job running", QUEST, MenuState(connected=True, on_frame=True, job=True, settings=True, selectable=True)),
    ]


def _items(entries):
    return [e for e in entries if e is not None and not isinstance(e, Header)]


def _section_of(entries, label):
    """The header the item sits under (None = top section)."""
    head = None
    for e in entries:
        if isinstance(e, Header):
            head = e.label
        elif e is not None and e[0] == label:
            return head
    raise AssertionError(f"{label} not in menu")


def test_library_menu_is_short():
    for name, g, st in _states():
        entries = C.menu_entries(menu_sections(g, st, _key))
        labels = [e[0] for e in _items(entries)]
        assert not any(isinstance(e, Header) for e in entries), name  # no section headers
        assert len(labels) <= 9, (name, labels)
        assert all(e[1] for e in _items(entries)), name
        assert "Open game page" in labels
        if st.job:
            assert labels == ["Show progress", "Cancel", "Open game page"]
            continue
        assert "Select" in labels[:3], (name, labels)
        assert "More actions…" in labels
        for gone in ("Find artwork…", "Analyze again", "Collect logs", "Screenshots", "Run launch test on Frame",
                     "Share working recipe…", "Type on Frame"):
            assert gone not in labels, (name, gone)
        # destructive items close the menu, after a divider
        n = sum(1 for lbl in labels if lbl in DESTRUCTIVE)
        assert labels[-n:] == [lbl for lbl in labels if lbl in DESTRUCTIVE] and labels[-1] == "Remove from library…"
        first = next(i for i, e in enumerate(entries) if e is not None and e[0] in DESTRUCTIVE)
        assert entries[first - 1] is None, name
        assert entries[first - 2][0] == "More actions…", name
        if st.plays:
            assert labels[0].startswith("Play on")
        if st.installs:
            assert st.installs[0][0] in labels
        assert ("Game settings…" in labels) == st.settings
        assert ("Uninstall from Frame…" in labels) == st.on_frame
        assert ("Uninstall from this PC…" in labels) == st.on_pc


def test_page_menu_is_the_full_one_without_its_buttons():
    for name, g, st in _states():
        st.quick = False
        entries = menu_sections(g, st, _key)
        labels = [e[0] for e in _items(entries)]
        for gone in ("Select", "Open game page", "Show progress", "Cancel", "Play on Frame", "Reinstall on Frame",
                     "Run launch test on Frame", "Uninstall from Frame…", "More actions…"):
            assert gone not in labels, (name, gone)
        assert labels[-1] == "Remove from library…"
        assert entries[-2] is None
        for i, e in enumerate(entries):  # every header has items under it
            if isinstance(e, Header):
                assert i + 1 < len(entries) and entries[i + 1] is not None and not isinstance(entries[i + 1], Header)
        assert all(e[1] for e in _items(entries)), name


def test_sections_and_conditions():
    _, g, st = _states()[0]
    st.quick = False
    entries = menu_sections(g, st, _key)
    heads = [e.label for e in entries if isinstance(e, Header)]
    assert heads == ["Artwork", "Recipe", "Troubleshoot"]
    assert _section_of(entries, "Analyze again") == "Troubleshoot"
    assert _section_of(entries, "Update Steam art on Frame") == "Artwork"
    assert _section_of(entries, "Share working recipe…") == "Recipe"
    linux_st = _states()[3][2]
    linux_st.quick = False
    linux = menu_sections(LINUX, linux_st, _key)
    assert "Recipe" not in [e.label for e in linux if isinstance(e, Header)]
    rift_st = _states()[2][2]
    rift_st.quick = False
    rift = [e[0] for e in _items(menu_sections(RIFT, rift_st, _key))]
    assert {"Check game files", "Change program…"} <= set(rift)
    assert "Analyze again" not in rift
    # the reset icon isn't the Frame's Restart icon
    reset = next(e for e in _items(entries) if e[0] == "Reset to suggested recipe…")
    assert reset[1] != ft.Icons.RESTART_ALT_ROUNDED


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
    labels = [e[0] if e else "—" for e in process_menu(proc, _key)]
    assert labels == ["Copy name", "Copy PID 5", "—", "End…", "Force quit…", "—", "End game…"]
    locked = process_menu({**proc, "locked": True}, _key)
    prot = next(e for e in locked if e and e[0] == "Protected by SteamOS")
    assert prot[2] is None and "End…" not in [e[0] for e in locked if e]
    group = [e[0] if e else "—" for e in process_menu({"kind": "group", "count": 3, "name": "g"}, _key)]
    assert group == ["Show its processes", "Copy name", "—", "End all 3…", "Force quit all 3…"]
    end = next(e for e in process_menu(proc, _key) if e and e[0] == "End…")
    assert end[1] != ft.Icons.CLOSE_ROUNDED
