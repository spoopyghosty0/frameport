from types import SimpleNamespace

import flet as ft

from frameport.mr.room import box_room, dumps, summary
from frameport.ui.views.frame import FrameView, room_state


def walk(control):
    yield control
    for child in control._get_children() if hasattr(control, "_get_children") else []:
        yield from walk(child)


def fake_app(answer):
    pending, refreshes, toasts = [], [], []
    target = SimpleNamespace(frame=SimpleNamespace(agent=lambda command: answer))
    return SimpleNamespace(target=target, frame_state="connected", run_bg=lambda fn: pending.append(fn),
                           refresh_view=lambda: refreshes.append(1), pending=pending, refreshes=refreshes,
                           toast=lambda message, **kw: toasts.append(message), toasts=toasts)


def test_room_status_loads_off_render_path_and_caches(tmp_path):
    from frameport.core.paths import user_data_dir

    room = box_room(4, 3)
    app = fake_app(dict(room_text=dumps(room), room=dict(source="scan", summary=summary(room)),
                        aligned=True, scanner_installed=True, default_circle=False))
    state = room_state(app)
    assert state["loading"] and len(app.pending) == 1
    # Building the card while loading does not write a preview or query SSH.
    assert FrameView(app).room_card()
    assert not (user_data_dir() / "room-previews").exists()
    app.pending.pop()()
    assert state["preview"].endswith(".svg")
    assert (user_data_dir() / state["preview"]).exists()
    assert len(app.refreshes) == 1
    assert FrameView(app).room_card()
    room_state(app)
    assert not app.pending
    room_state(app, force=True)
    assert len(app.pending) == 1


def test_room_status_error_and_reconnection():
    app = fake_app({})
    def fail(command):
        raise RuntimeError("query failed")
    app.target.frame.agent = fail
    state = room_state(app)
    app.pending.pop()()
    assert "query failed" in state["error"]
    assert FrameView(app).room_card()
    app.frame_state = "disconnected"
    assert room_state(app)["target"] is None
    app.frame_state = "connected"
    assert room_state(app) is not state


def test_card_shows_preview_and_default_circle_hint():
    app = fake_app({})
    app._room_state = dict(target=app.target, status=dict(default_circle=True, room=None),
                           preview=None, error=None, loading=False, time=float("inf"))
    control = FrameView(app).room_card()
    # Building with each documented state catches invalid Flet properties without a desktop or socket.
    assert isinstance(control, ft.Control)
    app._room_state["status"].update(default_circle=False, aligned=True, scanner_installed=True,
                                    room=dict(source="boundary", summary=summary(box_room(3, 4))))
    app._room_state["preview"] = "room-previews/example.svg"
    assert FrameView(app).room_card()
