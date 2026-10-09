"""Right-click menus in the Files and Screenshots tabs: what they offer, and that a right-click on one of several
selected items acts on the whole selection (as in a file manager)."""
from types import SimpleNamespace

from frameport.install.files import Entry
from frameport.ui import components as C
from frameport.ui.views.files import FilesView
from frameport.ui.views.screenshots import ALL, ScreenshotsView


class FakeApp(SimpleNamespace):
    def __init__(self):
        super().__init__(copied=[], page=SimpleNamespace(run_task=lambda fn, *a, **k: None))

    def copy(self, text):
        self.copied.append(text)


def labels(actions):
    return [a[0] if a else "—" for a in actions]


def test_menu_targets_selection_or_single():
    assert C.menu_targets("a", ["a", "b"]) == ["a", "b"]
    assert C.menu_targets("c", ["a", "b"]) == ["c"]  # outside the selection: only the clicked item
    assert C.menu_targets("a", ["a"]) == ["a"]


def files_view():
    v = FilesView(FakeApp())
    v.loc = {"id": "videos", "label": "Videos", "path": "/home/x/Videos", "android": "/sdcard/Movies"}
    v.path = "/home/x/Videos/trips"
    v.entries = [Entry("clips", "/home/x/Videos/trips/clips", True, 0, 0),
                 Entry("a.mp4", "/home/x/Videos/trips/a.mp4", False, 10, 0),
                 Entry("b.mp4", "/home/x/Videos/trips/b.mp4", False, 20, 0)]
    return v


def test_files_menu_single_entry_and_folder_space():
    v = files_view()
    folder = labels(v.menu_actions(v.entries[0]))
    assert folder[:4] == ["Open", "Download to this PC…", "Rename…", "Copy path"]
    assert "Select" in folder and folder[-1] == "Delete…"
    actions = v.menu_actions(v.entries[1])
    assert "Open" not in labels(actions)
    dict((a[0], a[2]) for a in actions if a)["Copy path"](None)
    assert v.app.copied == ["/sdcard/Movies/trips/a.mp4"]  # as games see it
    space = labels(v.menu_actions(None))
    assert space[:3] == ["Upload files…", "Upload folder…", "New folder…"] and "Refresh" in space


def test_files_menu_on_a_selection_acts_on_all_of_it():
    v = files_view()
    v.selected = {v.entries[1].path, v.entries[2].path}
    multi = labels(v.menu_actions(v.entries[2]))
    assert "Download 2 items to this PC…" in multi and "Delete 2 items…" in multi
    assert "Rename…" not in multi and "Clear selection" in multi
    assert "Download to this PC…" in labels(v.menu_actions(v.entries[0]))  # not in the selection: just that one


def test_files_menu_keeps_lepton_links_safe():
    v = files_view()
    v.loc = {"id": "app:x", "label": "Game", "path": "/base/external", "android": "/sdcard"}
    v.path = "/base/external"
    link = Entry("Movies", "/base/external/Movies", True, 0, 0, link=True)
    v.entries = [link]
    got = labels(v.menu_actions(link))
    assert "Rename…" not in got and "Delete…" not in got and "Select" not in got


def screenshots_view():
    v = ScreenshotsView(FakeApp())
    v.shots = [{"path": f"/s/{i}.jpg", "package": "com.x" if i < 2 else "", "title": "X", "time": 1} for i in range(3)]
    v._fill_dropdown()
    return v


def test_screenshots_menu():
    v = screenshots_view()
    v.games = [{"package": "com.x", "title": "X", "count": 2}, {"package": "", "count": 1}]
    v._fill_dropdown()
    one = labels(v.menu_actions(v.shots[0]))
    assert one[:4] == ["View", "Copy image", "Download…", "Show only this game's screenshots"]
    assert one[-1] == "Delete…"
    assert "Show only screenshots not from a FramePort game" in labels(v.menu_actions(v.shots[2]))
    v.filter = "com.x"
    assert not any("Show only" in x for x in labels(v.menu_actions(v.shots[0])))
    v.filter = ALL
    v.selected = {v.shots[0]["path"], v.shots[1]["path"]}
    multi = labels(v.menu_actions(v.shots[1]))
    assert "Download 2 screenshots…" in multi and "Delete 2 screenshots…" in multi and "View" not in multi
    assert "Copy image" not in multi  # (the clipboard holds one picture)
    assert labels(v.menu_actions(None))[:2] == ["Download all (3)…", "Select all"]


def test_drag_select_paints_a_selection():
    sel = set()
    d = C.DragSelect(lambda k: k in sel, lambda k, on: (sel.add if on else sel.discard)(k),
                     can_select=lambda k: k != "locked")
    d.hover("a", True)       # just hovering: nothing happens
    assert sel == set()
    d.start()                # press on "a" and drag
    for k in ("b", "locked", "c"):
        d.hover(k, True)
    d.hover("b", True)       # passing an item again doesn't flip it back
    d.end()
    assert sel == {"a", "b", "c"}
    d.hover("d", True)       # after the drag: hover only
    assert "d" not in sel


def test_drag_select_from_a_selected_item_deselects():
    sel = {"a", "b", "c"}
    d = C.DragSelect(lambda k: k in sel, lambda k, on: (sel.add if on else sel.discard)(k))
    d.hover("b", True)
    d.start()
    d.hover("c", True)
    d.end()
    assert sel == {"a"}


def test_drag_select_from_empty_space_selects_and_calls_on_start():
    sel, started = set(), []
    d = C.DragSelect(lambda k: k in sel, lambda k, on: (sel.add if on else sel.discard)(k),
                     on_start=lambda: started.append(1))
    d.hover("a", True)
    d.hover("a", False)      # left the item: the drag starts on empty space
    d.start()
    d.hover("x", True)
    assert sel == {"x"} and started == [1]
