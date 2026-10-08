"""Game page: a hero with the one-click action, where the game is installed, what FramePort will do, and the full
patch list tucked under "Advanced"."""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import flet as ft

from ... import pipeline
from ...artwork import thumbs
from ...core import library
from ...i18n import tr, tr_n
from ...patches import base
from ...recommend import catalog, engine
from .. import components as C
from .. import glyphs as G
from .. import theme as T
from ..help import HELP

if TYPE_CHECKING:
    from ..app import FramePortApp

CATEGORY_TITLES = {"frame": tr("Steam Frame patches"), "overport": tr("OVRPort patches"),
                   "adapter": tr("Game settings"), "device": tr("Files and environment on the Frame"),
                   "pcvr": tr("PC VR (Revive / Proton)")}


def plain_reason(reason: str, default_on: bool = False) -> str:
    """Why a patch is on, in plain words (the exact reason is shown with technical details). Recipes store the
    reason text from when they were made, so this also covers older wordings."""
    low = reason.lower()
    if low.startswith("known-good recipe"):
        return tr("From the tested recipe for this game")
    if low in ("enabled by you.", "set by you.", "enabled by user."):
        return tr("Turned on by you")
    if low.startswith("suggested by log triage"):
        return tr("Suggested after a launch test")
    if low.startswith("needed by") or low.startswith("required by"):
        return tr("Needed by another patch")
    if low.startswith("applied automatically"):
        return tr("Added automatically when needed")
    if default_on or "default" in low or low.startswith(("recommended for every", "required for every")):
        return tr("Standard for every game")
    return tr("Suggested for this game")


def should_ask_to_share(g: dict, installed: bool) -> bool:
    """Invite the user to share a recipe the built-in catalog doesn't have yet: an untested Quest/Android game that
    they have installed, launch-tested or played, and haven't shared (or dismissed) already."""
    r = g.get("recipe") or {}
    if g.get("kind") in ("rift", "linux") or r.get("status", "unknown") != "unknown" or \
            str(r.get("source", "")).startswith("catalog") or g.get("shared_config") or g.get("share_dismissed"):
        return False
    return installed or (g.get("last_test") or {}).get("verdict") == "pass" or bool(g.get("last_played"))


def _ago(t: float | None) -> str:
    if not t:
        return ""
    d = time.time() - t
    return tr("just now") if d < 90 else tr("{int} min ago").format(int=int(d / 60)) if d < 3600 else \
        tr("{int} h ago").format(int=int(d / 3600)) if d < 86400 else time.strftime("%b %d", time.localtime(t))


class GameView:
    def __init__(self, app: FramePortApp, package: str, advanced: bool = False, show_all: bool = False):
        self.app, self.package, self.advanced, self.show_all = app, package, advanced, show_all
        from .library import twins

        self.g = library.game(package)
        self.games = library.games()
        self.twins = twins(self.games)
        self.rift = bool(self.g) and self.g.get("kind") == "rift"
        self.linux = bool(self.g) and self.g.get("kind") == "linux"  # native arm64 app: installed as it is

    # ---------------------------------------------------------------- hero
    def hero(self) -> ft.Control:
        app, g, pkg = self.app, self.g, self.package
        from .library import display_title

        a = g["analysis"]
        art = thumbs.url(pkg, ("hero", "landscape", "portrait", "square", "banner"), 1280, wait=False)
        platform = C.platform(g)[1]
        facts = " · ".join(x for x in (platform, *(() if self.linux else (a.get("engine"), a.get("xr"))))
                           if x and x not in ("?", "none"))
        recipe = g.get("recipe") or {}
        chips = [C.status_chip(recipe.get("status", "unknown"))]
        st = C.install_state(g, app.frame_info)
        if st in ("installed", "outdated"):
            chips.append(C.install_badge(st))
        if self.rift and pkg in app.pc_installs():
            chips.append(C.install_badge("on_pc"))
        actions = self.actions()
        return ft.Container(
            ft.Stack([
                C.art_fill(art, radius=T.px(16), hero=True, left=0, right=0, top=0, bottom=0,
                           placeholder_icon=C.platform_icon(g)),
                ft.Container(left=0, right=0, top=0, bottom=0, border_radius=T.px(16), gradient=ft.LinearGradient(
                    begin=ft.Alignment.CENTER_LEFT, end=ft.Alignment.CENTER_RIGHT,
                    colors=[T.soft(T.BG, 0.97), T.soft(T.BG, 0.80), T.soft(T.BG, 0.25)], stops=[0.0, 0.45, 1.0])),
                ft.Container(ft.Row([
                    ft.Column([
                        C.ghost(tr("Library"), ft.Icons.ARROW_BACK_ROUNDED, lambda e: app.go("library")),
                        ft.Container(expand=True),
                        C.meta(facts.upper(), T.TEXT_2, weight=ft.FontWeight.W_600),
                        ft.Text(display_title(g, self.twins), size=T.px(34), weight=ft.FontWeight.W_800, color=T.TEXT,
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Row(chips, spacing=T.S2, wrap=True),
                        ft.Container(height=T.S2),
                        actions,
                    ], spacing=T.S2, expand=True),
                ]), padding=ft.Padding(T.S4, T.S3, T.S5, T.S5), left=0, right=0, top=0, bottom=0),
            ]),
            height=T.px(330), border_radius=T.px(16), border=ft.Border.all(1, T.BORDER))

    def actions(self) -> ft.Control:
        app, g, pkg = self.app, self.g, self.package
        job = app.jobs.busy_with(pkg)
        if job:
            pct = (f" {job.fraction:.0%}" if job.fraction is not None else "") + \
                (f" · {job.speed.split(' · ')[0]}" if job.speed else "")
            return ft.Row([
                ft.FilledButton(content=ft.Row([ft.ProgressRing(width=T.px(16), height=T.px(16), stroke_width=T.px(2),
                                                                color=T.ON_ACCENT),
                                                ft.Text(f"{job.stage or tr('Queued')}{pct}", color=T.ON_ACCENT,
                                                        weight=ft.FontWeight.W_600)], spacing=T.px(10), tight=True),
                                on_click=lambda e: app.show_activity(True),
                                style=ft.ButtonStyle(bgcolor=T.ACCENT, shape=ft.RoundedRectangleBorder(radius=T.px(8)),
                                                     padding=ft.Padding(T.px(22), T.px(18), T.px(22), T.px(18)))),
                C.ghost(tr("Cancel"), ft.Icons.CLOSE_ROUNDED, lambda e: app.jobs.cancel(job)),
            ], spacing=T.S2)
        buttons: list[ft.Control] = []
        for i, (label, icon, handler, disabled, tip) in enumerate(app.play_options(g) + app.install_options(g)):
            buttons.append(C.primary(label, icon, handler, disabled, tip, big=True) if i == 0 else
                           C.secondary(label, icon, handler, disabled, tip))
        if C.is_media_player(g) and g.get("kind") != "rift" and app.frame_state == "connected" and \
                C.install_state(g, app.frame_info) in ("installed", "outdated"):
            buttons.append(C.secondary(tr("Add videos"), ft.Icons.VIDEO_LIBRARY_OUTLINED,
                                       lambda e: app.go("files", pkg), False,
                                       tr("Opens this player's storage on the Frame (Files tab): upload videos "
                                          "into the folder it lists")))
        more = ft.PopupMenuButton(icon=ft.Icons.MORE_HORIZ_ROUNDED, icon_color=T.TEXT_2, bgcolor=T.SURFACE_2,
                                  tooltip=tr("More actions"), items=C.menu_items(app.game_actions(pkg, quick=False)))
        return ft.Row(buttons + [more], spacing=T.S2, wrap=True)

    # ---------------------------------------------------------------- sections
    def where(self) -> ft.Control:
        app, g, pkg = self.app, self.g, self.package
        cards = []
        st = C.install_state(g, app.frame_info)
        last = g.get("last_test") or {}
        diff = C.settings_diff(g, app.frame_info)
        if diff:
            def names(ids):
                return ", ".join(tr(base.get(i).title) if i in base.REGISTRY else i for i in ids)
            changed = "; ".join(filter(None, [tr("on: {names}").format(names=names(diff[0])) if diff[0] else "",
                                              tr("off: {names}").format(names=names(diff[1])) if diff[1] else ""]))
        frame_line = {"installed": tr("Installed"),
                      "outdated": (tr("Installed · patch settings changed since ({changed}) — update to apply")
                                   .format(changed=changed) if diff else tr("Installed · a newer build is ready")),
                      "missing": tr("Not installed"), None: tr("Frame not connected")}[st]
        frame_ok = st in ("installed", "outdated")
        # Oculus/LibOVR Rift games need Revive, which can't run on the Frame — be honest about it
        patches = (g.get("recipe") or {}).get("patches", {})
        rift_oculus = self.rift and "pcvr.revive" in patches
        rift_repack = self.rift and "pcvr.repack_launcher" in patches
        rift_platform = self.rift and (g["analysis"].get("extra") or {}).get("platform_sdk")
        frame_color = (T.TEXT_3 if (rift_oculus or rift_platform) and not frame_ok else
                       T.OK if st == "installed" else T.WARN if st == "outdated" else T.TEXT_3)
        frame_sub = (tr("Oculus game — needs Revive, which doesn't run on the Frame. Play it on this PC (SteamVR).")
                     if rift_oculus else
                     tr("Needs the Oculus Platform (Meta Horizon app) for its license check, which the Frame doesn't "
                     "have — it crashes at startup there. Play it on this PC.") if rift_platform else
                     tr("Uses the repack's bundled Revive — experimental on the Frame") if rift_repack and not last else
                     tr("Last launch test: {get} · furthest: {value}")
                     .format(get=last.get('verdict'), value=last.get('milestone') or '—') if last
                     else tr("Runs directly — no Revive needed") if self.rift else
                     tr("Runs natively on SteamOS, started from the Frame's Steam library") if self.linux else "")
        cards.append(self.target_card(
            G.FRAME, tr("Steam Frame"), frame_line, frame_color, frame_sub,
            [C.icon_btn(G.TEST, C.tip(tr("Launch test on the Frame. ") + HELP["launch_test"]),
                        lambda e: app.test_game(pkg, "frame"), not frame_ok),
             C.icon_btn(ft.Icons.DELETE_OUTLINE_ROUNDED, C.tip(tr("Uninstall from the Frame. ") + HELP["uninstall"]),
                        lambda e: app.uninstall(pkg, "frame"), not frame_ok)] if frame_ok else []))
        if self.rift:
            dep = app.pc_installs().get(pkg)
            cards.append(self.target_card(
                G.PC, tr("This PC (Steam + Revive)"),
                (tr("In your Steam library · launch settings changed — update it") if C.pc_outdated(g, dep) else
                 tr("In your Steam library")) if dep else tr("Not installed"),
                (T.WARN if C.pc_outdated(g, dep) else T.PC) if dep else T.TEXT_3,
                (tr("Revive {value} · {value2} backend")
                 .format(value=dep.get('revive_version') or '', value2=dep.get('backend') or 'openxr')
                 if dep.get("revive_win") else tr("The repack's own Revive · runs the game directly")
                 if dep.get("launch") == "repack" else tr("Runs the game directly")) if dep else "",
                [C.icon_btn(G.TEST, tr("Launch test on this PC"),
                            lambda e: app.test_game(pkg, "pc")),
                 C.icon_btn(ft.Icons.DELETE_OUTLINE_ROUNDED, tr("Uninstall from this PC…"),
                            lambda e: app.uninstall(pkg, "pc"))] if dep else []))
        return C.section(tr("Where it's installed"), ft.Row(cards, spacing=T.S3), help="where")

    def target_card(self, icon, name, line, color, sub, buttons) -> ft.Control:
        return C.card(ft.Row([
            ft.Container(C.as_icon(icon, T.px(22), color), width=T.px(44), height=T.px(44),
                         border_radius=T.px(10), bgcolor=T.soft(color, 0.14), alignment=ft.Alignment.CENTER),
            ft.Column([C.body(name, T.TEXT, weight=ft.FontWeight.W_600), C.body(line, color, size=T.T_META)]
                      + ([C.meta(sub)] if sub else []), spacing=T.px(2), expand=True),
            *buttons,
        ], spacing=T.S3), expand=True)

    def notes(self) -> list[ft.Control]:
        g, pkg = self.g, self.package
        recipe = library.recipe_from_dict(g["recipe"])
        entry = catalog.lookup(pkg)
        out = []
        extra = g["analysis"].get("extra", {})
        missing = C.missing_libraries(g, self.app.frame_info) if self.linux else []
        if missing:
            out.append(C.callout(ft.Column([
                C.body(tr("It won't start on the Frame: SteamOS doesn't have these libraries and the app doesn't bring "
                          "them. Look for a build of the app that includes them, then install that one."),
                       T.TEXT),
                C.meta(", ".join(missing), T.TEXT_2, selectable=True)], spacing=T.px(4)), "error",
                ft.Icons.EXTENSION_OFF_ROUNDED))
        if self.rift and g.get("exe_confirmed") is False:
            out.append(C.callout(ft.Row([
                C.body(tr("FramePort picked {value} to start this game, but there are other candidates. "
                          "Check it before installing.").format(value=g.get('exe', '').rsplit('/', 1)[-1]),
                       T.TEXT, expand=True),
                C.secondary(tr("Check…"), ft.Icons.TERMINAL_ROUNDED, lambda e: self.app.choose_exe(pkg))]), "warn",
                ft.Icons.HELP_OUTLINE_ROUNDED))
        if self.rift and extra.get("platform_sdk"):
            out.append(C.callout(tr("Uses the Oculus Platform SDK, which checks your Oculus license: it needs the "
                                    "Oculus app on this PC and may quit right after starting. You can still try it."),
                                 "warn"))
        installed = C.install_state(g, self.app.frame_info) in ("installed", "outdated")
        if should_ask_to_share(g, installed):
            def dismiss(e):
                library.upsert_game(pkg, share_dismissed=True)
                self.app.render()
            out.append(C.callout(ft.Column([
                C.body(tr("Tried it in the headset? Tell us how it runs: with your recipe it can join FramePort's "
                          "built-in list, so it works out of the box for everyone."), T.TEXT),
                ft.Row([
                    C.secondary(tr("It works: share…"), ft.Icons.THUMB_UP_OUTLINED,
                                lambda e: self.app.share_config_dialog(pkg, "works")),
                    C.secondary(tr("It has issues: share…"), ft.Icons.BUILD_CIRCLE_OUTLINED,
                                lambda e: self.app.share_config_dialog(pkg, "issues")),
                    C.ghost(tr("It doesn't run: report…"), ft.Icons.BUG_REPORT_OUTLINED,
                            lambda e: self.app.report_problem_dialog(pkg)),
                    C.ghost(tr("Not now"), on_click=dismiss),
                ], spacing=T.S2, run_spacing=T.S2, wrap=True),
            ], spacing=T.S2), "info", ft.Icons.VOLUNTEER_ACTIVISM_OUTLINED))
        if g.get("catalog_update") and recipe.source == "user":
            def take_update(e):
                from ... import pipeline

                pipeline.apply_catalog_update(pkg)
                self.app.toast(tr("New recipe applied: use Update on Frame to install it"))
                self.app.render()
            out.append(C.callout(ft.Row([
                C.body(tr("A newer known-good recipe for this game is available (you changed this game's settings, "
                          "so it wasn't applied by itself)."), T.TEXT, expand=True),
                C.secondary(tr("Use the new recipe"), ft.Icons.AUTO_FIX_HIGH_OUTLINED, take_update)],
                vertical_alignment=ft.CrossAxisAlignment.CENTER), "info", ft.Icons.NEW_RELEASES_OUTLINED))
        if g.get("steam_art_stale") and installed:
            out.append(C.callout(ft.Row([
                C.body(tr("The Frame's Steam library still shows the old artwork."), T.TEXT, expand=True),
                C.secondary(tr("Update Steam art on Frame"), ft.Icons.IMAGE_OUTLINED,
                            lambda e: self.app.update_steam_art(pkg))]), "info", ft.Icons.IMAGE_OUTLINED))
        if recipe.status == "unsupported":
            out.append(C.callout(recipe.notes or tr("This game can't run on the Steam Frame."), "error"))
        elif recipe.notes and not (self.rift and extra.get("platform_sdk")):
            out.append(C.callout(recipe.notes, "info"))
        if entry and entry.pcvr_alternative:
            out.append(C.callout(tr("PC VR alternative: {pcvr_alternative}")
                                 .format(pcvr_alternative=entry.pcvr_alternative), "pc"))
        from .library import counterparts

        links = []
        for r in counterparts(g, self.games):
            other_rift = r.get("kind") == "rift"
            links.append(C.ghost(tr("Also in your library: {value} version")
                                 .format(value=tr("Rift") if other_rift else tr("Quest")),
                                 G.PC if other_rift else G.FRAME,
                                 lambda e, p=r["package"]: self.app.open_game(p), color=T.ACCENT))
        if links:
            out.append(ft.Row(links, spacing=T.S2, wrap=True))
        warns = engine.warnings(recipe)
        if warns:
            out.append(C.callout("\n".join(warns), "warn"))
        return out

    def about(self) -> ft.Control | None:
        from ...artwork import details as det

        d = self.g.get("details") or {}
        shots = det.screenshot_files(self.package)
        if not d.get("description") and not shots and not d.get("genres"):
            if "details" in self.g:
                return None
            return C.section(tr("About this game"), C.card(ft.Row([
                ft.ProgressRing(width=T.px(16), height=T.px(16), stroke_width=T.px(2), color=T.ACCENT),
                C.meta(tr("Looking up the store description and screenshots…"))], spacing=T.S2)))
        parts: list[ft.Control] = []
        if shots:
            strip = ft.Row(spacing=T.S3, scroll=ft.ScrollMode.AUTO)
            for i, shot in enumerate(shots):
                url = thumbs.asset_url(thumbs.thumb(shot, 480, shot.stem))
                strip.controls.append(ft.Container(
                    C.art_fill(url, radius=T.RADIUS_SM, width=T.px(256), height=T.px(144)), border_radius=T.RADIUS_SM,
                    on_click=lambda e, i=i: self.lightbox(shots, i), ink=True, tooltip=tr("View screenshot")))
            parts.append(strip)
        facts = [(k, d.get(k)) for k in ("developer", "publisher", "release_date") if d.get(k)]
        if facts:
            parts.append(ft.Row([ft.Column([C.meta({"developer": "Developer", "publisher": "Publisher",
                                                     "release_date": "Released"}[k]), C.body(v, T.TEXT)],
                                           spacing=T.px(2))
                                 for k, v in facts], spacing=T.S6, wrap=True))
        if d.get("genres"):
            parts.append(ft.Row([C.pill(g, T.TEXT_2) for g in d["genres"][:8]], spacing=T.px(6), wrap=True))
        text = det.plain_description(d.get("description") or d.get("short") or "")
        if text:
            long = len(text) > 480
            short = text[:480].rsplit(" ", 1)[0] + "…"
            body = C.body(short if long else text, T.TEXT, selectable=True)
            parts.append(body)
            if long:
                state = {"open": False}

                def more(e):
                    state["open"] = not state["open"]
                    body.value = text if state["open"] else short
                    e.control.text = tr("Show less") if state["open"] else tr("Show more")
                    body.update()
                    e.control.update()
                parts.append(ft.TextButton(tr("Show more"), on_click=more, style=ft.ButtonStyle(color=T.ACCENT)))
        links = det.store_links(d)
        if links:
            parts.append(ft.Row([ft.TextButton(label, icon=ft.Icons.OPEN_IN_NEW_ROUNDED, url=url,
                                               style=ft.ButtonStyle(color=T.ACCENT)) for label, url in links],
                                spacing=T.S2, wrap=True))
        src = ", ".join({"oculusdb": "Meta store (OculusDB)", "steam": "Steam store"}.get(s, s)
                        for s in d.get("sources") or [])
        return C.section(tr("About this game"), C.card(ft.Column(parts, spacing=T.S4)),
                         subtitle=tr("From the {src}").format(src=src) if src else None)

    def lightbox(self, shots: list, index: int) -> None:
        page = self.app.page
        state = {"i": index}
        img = ft.Image(src=thumbs.asset_url(shots[index]), fit=ft.BoxFit.CONTAIN, width=T.px(1100), height=T.px(620),
                       border_radius=T.RADIUS_SM)
        counter = C.meta(f"{index + 1} / {len(shots)}")

        def show(delta):
            state["i"] = (state["i"] + delta) % len(shots)
            img.src = thumbs.asset_url(shots[state["i"]])
            counter.value = f"{state['i'] + 1} / {len(shots)}"
            img.update()
            counter.update()
        page.show_dialog(ft.AlertDialog(
            content=ft.Container(ft.Column([img, ft.Row([
                C.icon_btn(ft.Icons.CHEVRON_LEFT_ROUNDED, tr("Previous"), lambda e: show(-1)), counter,
                C.icon_btn(ft.Icons.CHEVRON_RIGHT_ROUNDED, tr("Next"), lambda e: show(1)),
                ft.Container(expand=True), C.ghost(tr("Close"), on_click=lambda e: page.pop_dialog())])],
                spacing=T.S2, tight=True), width=T.px(1100)),
            bgcolor=T.BG, shape=ft.RoundedRectangleBorder(radius=T.RADIUS), content_padding=T.S3))

    def tags(self) -> ft.Control:
        from .library import all_tags, auto_tags, normalize_tag, user_tags

        pkg = self.package
        row = ft.Row(spacing=T.S2, run_spacing=T.S2, wrap=True)

        def save(tags):
            library.upsert_game(pkg, tags=tags)
            self.g = library.game(pkg)
            fill()
            row.update()

        def remove(tag):
            save([t for t in user_tags(self.g) if t != tag])

        def add(e):
            tag = normalize_tag(e.control.value)
            e.control.value = ""
            if tag and tag.lower() not in {t.lower() for t in user_tags(self.g)}:
                save(user_tags(self.g) + [tag])
            else:
                e.control.update()

        field = ft.TextField(hint_text=tr("Add a tag"), dense=True, width=T.px(150), text_size=T.T_META,
                             border_radius=T.px(20), bgcolor=T.SURFACE_3, border_color=ft.Colors.TRANSPARENT,
                             focused_border_color=T.ACCENT,
                             content_padding=ft.Padding(T.px(12), T.px(6), T.px(12), T.px(6)), on_submit=add)

        def fill():
            mine = user_tags(self.g)
            chips = [ft.Container(ft.Row([C.body(t, T.TEXT, size=T.T_META),
                                          ft.Icon(ft.Icons.CLOSE_ROUNDED, size=T.px(13), color=T.TEXT_2)],
                                         spacing=T.px(4), tight=True),
                                  bgcolor=T.ACCENT_SOFT, border_radius=T.px(20),
                                  padding=ft.Padding(T.px(10), T.px(5), T.px(8), T.px(5)),
                                  on_click=lambda e, t=t: remove(t), tooltip=tr("Remove tag"))
                     for t in mine]
            chips += [ft.Container(C.meta(t), border=ft.Border.all(1, T.BORDER), border_radius=T.px(20),
                                   padding=ft.Padding(T.px(10), T.px(5), T.px(10), T.px(5)),
                                  tooltip=tr("Added automatically (engine, VR API or store genre)"))
                      for t in auto_tags(self.g) if t.lower() not in {m.lower() for m in mine}]
            used = {x for g in self.games for x in user_tags(g)}
            suggestions = [t for t in all_tags(self.games) if t in used and t not in mine][:6]
            chips.append(field)
            chips += [ft.Container(C.meta("+ " + t, T.ACCENT), padding=ft.Padding(T.px(6), T.px(5), T.px(6), T.px(5)),
                                   on_click=lambda e, t=t: save(user_tags(self.g) + [t]), tooltip=tr("Add this tag"))
                      for t in suggestions]
            row.controls = chips
        fill()
        return C.section(tr("Tags"), row, subtitle=tr("Use tags to group and filter your library"), help="tags")

    def linux_summary(self) -> ft.Control:
        """A Linux app has nothing to patch: what FramePort installs, and how it starts."""
        g, pkg = self.g, self.package
        extra = (g.get("analysis") or {}).get("extra") or {}
        vr = bool(extra.get("openxr"))
        lead = tr("Installs the app as it is, unpacked on the Frame, with an entry in its Steam library") \
            if extra.get("appimage") else tr("Installs the app as it is, with an entry in the Frame's Steam library")
        change = C.ghost(tr("Change…"), ft.Icons.TERMINAL_ROUNDED, lambda e: self.app.choose_exe(pkg)) \
            if self.app.linux_programs(g) else None
        rows = [
            C.kv(tr("Program"), ft.Row([C.body(g.get("exe") or "", T.TEXT, selectable=True)]
                                       + ([change] if change else []), spacing=T.S2, wrap=True)),
            C.kv(tr("AppImage"), tr("Yes") if extra.get("appimage") else tr("No"), "appimage"),
            C.kv(tr("CPU"), tr("x86_64: runs through FEX (x86 translation, slower; FramePort installs FEX on the "
                               "Frame)") if extra.get("x86_64") else tr("arm64 (runs natively)"), "linux_x86"),
            C.kv(tr("VR (OpenXR)"), tr("Yes: uses the Frame's OpenXR runtime") if vr else
                 tr("No: a 2D app")),
            C.kv(tr("Source"), extra.get("source") or g.get("game_dir") or ""),
        ]
        return C.section(tr("What FramePort will do"), C.card(ft.Column([
            ft.Row([ft.Icon(ft.Icons.TERMINAL_ROUNDED, color=T.PC, size=T.px(18)),
                    C.body(lead, T.TEXT, weight=ft.FontWeight.W_500, expand=True)], spacing=T.S2),
            *rows], spacing=T.S3)), help="linux_app")

    def recipe_summary(self) -> ft.Control:
        if self.linux:
            return self.linux_summary()
        recipe = library.recipe_from_dict(self.g["recipe"])
        entry = catalog.lookup(self.package)
        on = [base.get(pid) for pid in recipe.patches if pid in base.REGISTRY or _known(pid)]
        visible = [p for p in on if not p.default_on or p.category in ("pcvr",)]
        if entry and recipe.source.startswith("catalog"):
            lead = f"Known-good recipe for this game, tested {entry.verified.get('date', '')}".strip(", ")
            icon, color = G.RECIPE, T.OK
        elif recipe.source == "user":
            lead, icon, color = tr("Your custom recipe"), ft.Icons.TUNE_ROUNDED, T.ACCENT
        else:
            lead, icon, color = tr("Suggested by FramePort from the game's engine and APIs"), \
                ft.Icons.AUTO_AWESOME_ROUNDED, T.ACCENT
        chips = [ft.Container(C.body(tr(p.title), T.TEXT, size=T.T_META),
                              tooltip=C.tip(tr(recipe.reasons.get(p.id) or p.description)),
                              bgcolor=T.SURFACE_3, border_radius=T.px(6),
                              padding=ft.Padding(T.px(10), T.px(5), T.px(10), T.px(5)))
                 for p in visible]
        base_count = len(on) - len(visible)
        if base_count > 0:
            chips.append(C.with_help(C.meta(tr("+ {base_count} standard patches").format(base_count=base_count)),
                                     "standard_fixes"))
        as_is = recipe.as_is
        flat = library.analysis_from_dict(self.g["analysis"]).vr_kind == "none" if self.g.get("analysis") else False
        if flat:
            # an Android app without VR: the APK is never changed, but launch patches (e.g. the navigation bar) apply
            lead, icon, color = tr("Android app without VR: installed unchanged and shown as a flat window"), \
                ft.Icons.TABLET_ANDROID_ROUNDED, T.PC
        elif as_is and self.rift:
            # a pre-patched Rift copy still needs a VR runtime on the Frame: Revive is added at launch, not to its files
            lead, icon, color = (tr("Your copy is used as it is (only the Frame's copy gets launch patches)") +
                                 (tr(" · Revive provides the Oculus runtime") if "pcvr.revive" in recipe.patches
                                  else "")), \
                ft.Icons.INVENTORY_2_ROUNDED, T.PC
        elif as_is:
            lead, icon, color = tr("Installs the game exactly as it is: no patches (your copy is already patched)"), \
                ft.Icons.INVENTORY_2_ROUNDED, T.PC
            chips = []

        def toggle_as_is(e):
            r = library.recipe_from_dict(library.game(self.package)["recipe"])
            r.as_is = bool(e.control.value)  # Revive stays as it is: it isn't a change to the game's files
            r.source = "user"
            pipeline.set_recipe(self.package, r)
            self.app.open_game(self.package, advanced=self.advanced)
        switch = C.with_help(C.switch(value=as_is, active_color=T.PC, on_change=toggle_as_is,
                                       label=(tr("Already patched: don't change the game's files") if self.rift else
                                              tr("Already patched: install as is (skip patching)"))), "as_is")
        return C.section(
            tr("What FramePort will do"),
            C.card(ft.Column([
                ft.Row([C.as_icon(icon, T.px(18), color), C.body(lead, T.TEXT, weight=ft.FontWeight.W_500,
                                                                     expand=True)], spacing=T.S2),
                ft.Row(chips, spacing=T.S2, run_spacing=T.S2, wrap=True) if chips else
                (ft.Container() if as_is else C.meta(tr("Nothing to patch: it runs as is."))),
                *([] if flat else [ft.Divider(), switch]),
            ], spacing=T.S3)),
            action=C.ghost(tr("Hide patches") if self.advanced else tr("Customize"), ft.Icons.TUNE_ROUNDED,
                           lambda e: self.app.open_game(self.package, advanced=not self.advanced),
                           tooltip=None if self.advanced else tr("See every patch and turn them on or off")),
            help="recipe")

    def advanced_panel(self) -> ft.Control:
        app, g, package = self.app, self.g, self.package
        recipe = library.recipe_from_dict(g["recipe"])
        analysis = library.analysis_from_dict(g["analysis"])
        shown, hidden = engine.visible_patches(analysis, recipe)
        listed = {p.id for p in (shown + hidden if self.show_all else shown)}
        state = {"recipe": recipe}
        warn = C.body("", T.WARN)
        technical = bool(library.setting("ui.patch_details", False))  # remembered for every game

        def save(r):
            r.source = "user"
            pipeline.set_recipe(package, r)
            warn.value = "\n".join(engine.warnings(r))
            warn.update()

        def toggle(pid):
            def handler(e):
                r = state["recipe"]
                if e.control.value:
                    patch = base.get(pid)
                    r.patches[pid] = {"value": patch.params[0].default} if patch.category == "adapter" else \
                        {q.key: (r.patches.get(pid) or {}).get(q.key, q.default) for q in patch.params}
                    r.reasons[pid] = r.reasons.get(pid) or tr("Enabled by you.")
                else:
                    r.patches.pop(pid, None)
                save(r)
            return handler

        def set_value(pid, kind):
            def handler(e):
                try:
                    v = float(e.control.value) if kind == "float" else int(float(e.control.value))
                except ValueError:
                    return
                state["recipe"].patches[pid] = {"value": v}
                save(state["recipe"])
            return handler

        def choose(pid, key):
            def handler(e):
                r = state["recipe"]
                value = e.control.value or ""
                if value:  # a non-default choice is stored as the patch's parameter
                    r.patches[pid] = {key: value}
                    r.reasons[pid] = tr("Chosen by you.")
                else:
                    r.patches.pop(pid, None)
                save(r)
            return handler

        def set_param(pid, key):
            def handler(e):
                r = state["recipe"]
                r.patches.setdefault(pid, {})[key] = e.control.value or ""
                r.reasons[pid] = r.reasons.get(pid) or tr("Set by you.")
                save(r)
                app.open_game(package, advanced=True, show_all=self.show_all)  # the switch shows it's on now
            return handler

        sections = []
        for cat in ("pcvr", "frame", "overport", "adapter", "device"):
            if cat == "adapter":  # one row: the settings themselves are in the Game settings dialog (plain words)
                if app.has_game_settings(g):
                    from .adapter_dialog import default, recipe_values

                    vals = recipe_values(g)
                    changed = sum(1 for k, v in vals.items() if v != default(k))
                    sections.append(C.card(ft.Row([
                        ft.Column([ft.Row([C.body(CATEGORY_TITLES[cat], T.TEXT, weight=ft.FontWeight.W_600),
                                           C.help_icon("cat_adapter")], spacing=T.S2),
                                   C.meta(tr_n("{n} setting changed from the default",
                                               "{n} settings changed from the default", changed) if changed
                                          else tr("All at their defaults"))], spacing=T.px(2), expand=True),
                        C.secondary(tr("Change settings…"), ft.Icons.TUNE_ROUNDED,
                                    lambda e: app.settings_dialog(package)),
                    ], spacing=T.S3), padding=ft.Padding(T.S4, T.S3, T.S4, T.S3)))
                continue
            rows = []
            for p in [p for p in base.all_patches() if p.category == cat and p.id in listed]:
                on = p.id in recipe.patches
                reason = recipe.reasons.get(p.id, "")
                # plain by default; "Show technical details" adds the exact description, the id and the parameters
                sub = [C.meta(tr(p.summary or p.description), T.TEXT_2)]
                if technical and p.summary:
                    sub.append(C.meta(tr(p.description), T.TEXT_3, selectable=True))
                if reason:
                    shown = tr(reason).replace("overport", "OVRPort") if technical else \
                        plain_reason(reason, p.default_on)
                    sub.insert(0, C.meta(shown, T.ACCENT))
                extra = None
                choices = getattr(p, "CHOICES", None)
                if choices:  # a plain choice instead of a switch + text field (e.g. Proton: Experimental / Stable)
                    current = recipe.params(p.id).get(p.params[0].key, "") if on else ""
                    keys = [k for k, _label in choices]
                    if current not in keys:  # a tool name typed under technical details
                        choices = (*choices, (current, current))
                    rows.append(ft.Container(ft.Row([
                        ft.Column([C.body(tr(p.title), T.TEXT, weight=ft.FontWeight.W_500), *sub],
                                  spacing=T.px(3), expand=True),
                        ft.Dropdown(value=current, width=T.px(280), dense=True, border_color=T.BORDER,
                                    text_size=T.T_BODY,
                                    options=[ft.DropdownOption(key=k, text=tr(label)) for k, label in choices],
                                    on_select=choose(p.id, p.params[0].key)),
                    ], spacing=T.S3), padding=ft.Padding(T.S4, T.px(10), T.S4, T.px(10)), border=ft.Border(
                        top=ft.BorderSide(1, T.BORDER))))
                    continue
                if not technical:
                    pass
                elif cat == "adapter":
                    val = recipe.params(p.id).get("value", p.params[0].default)
                    extra = ft.TextField(value=str(val), width=T.px(90), dense=True, text_size=T.T_BODY,
                                         border_color=T.BORDER, on_blur=set_value(p.id, p.params[0].kind))
                elif p.params:
                    q = p.params[0]
                    multi = q.kind == "text"
                    extra = ft.TextField(value=str(recipe.params(p.id).get(q.key, q.default) or ""), hint_text=q.help,
                                         width=T.px(260), dense=True, multiline=multi, min_lines=1,
                                         max_lines=4 if multi else 1, text_size=T.T_BODY, border_color=T.BORDER,
                                         on_blur=set_param(p.id, q.key))
                rows.append(ft.Container(ft.Row([
                    ft.Column([ft.Row([C.body(tr(p.title), T.TEXT, weight=ft.FontWeight.W_500)]
                                      + ([C.pill(tr("experimental"), T.WARN, tooltip=C.tip(HELP["experimental"]))]
                                         if p.experimental else [])
                                      + ([C.meta(p.id)] if technical else []), spacing=T.S2, wrap=True), *sub],
                              spacing=T.px(3), expand=True),
                    *([extra] if extra else []),
                    ft.Switch(value=on, on_change=toggle(p.id), active_color=T.ACCENT),
                ], spacing=T.S3), padding=ft.Padding(T.S4, T.px(10), T.S4, T.px(10)), border=ft.Border(
                    top=ft.BorderSide(1, T.BORDER))))
            if not rows:
                continue
            count = sum(1 for p in base.all_patches() if p.category == cat and p.id in recipe.patches)
            sections.append(C.card(ft.Column([
                ft.Container(ft.Row([C.body(CATEGORY_TITLES[cat], T.TEXT, weight=ft.FontWeight.W_600),
                                     C.help_icon(f"cat_{cat}"), C.meta(tr("{count} on").format(count=count))],
                                    spacing=T.S2),
                             padding=ft.Padding(T.S4, T.S3, T.S4, T.S3)),
                *rows], spacing=0), padding=0))

        def set_alt(e):
            state["recipe"].use_alt = e.control.value
            save(state["recipe"])
        def set_technical(e):
            library.set_setting("ui.patch_details", bool(e.control.value))
            app.open_game(package, advanced=True, show_all=self.show_all)
        top = [C.with_help(C.switch(tr("Show technical details (patch ids, exact effects, parameters)"),
                                    value=technical, on_change=set_technical), "patch_details")]
        if recipe.alt_patches:
            alt = ", ".join(recipe.alt_patches if technical else
                            [tr(base.get(p).title) if p in base.REGISTRY else p for p in recipe.alt_patches])
            top.append(C.with_help(C.switch(label=tr("Install the alternate build (") + alt
                                             + ")", value=recipe.use_alt, on_change=set_alt, active_color=T.ACCENT),
                                   "alt_build"))
        if hidden:
            top.append(C.with_help(C.switch(
                label=tr("Show all patches ({len} don't apply to this game)").format(len=len(hidden)),
                value=self.show_all,
                active_color=T.ACCENT, on_change=lambda e: app.open_game(package, advanced=True,
                                                                         show_all=e.control.value)), "show_all"))
        warn.value = "\n".join(engine.warnings(recipe))
        return C.section(tr("Patches"), *top, warn, *sections,
                         subtitle=tr("Changes apply to the next install. Hover a chip above for why it was suggested."))

    def details(self) -> ft.Control:
        g, a = self.g, self.g["analysis"]
        rows = [C.kv(tr("Package"), g["package"])]
        if self.linux:
            rows += [C.kv(tr("Folder"), g.get("game_dir") or ""),
                     C.kv(tr("Size"), tr("{value:.1f} GiB").format(value=(g.get("data_bytes") or 0) / 2**30)
                          if (g.get("data_bytes") or 0) >= 2**30 else
                          tr("{value:.0f} MiB").format(value=(g.get("data_bytes") or 0) / 2**20))]
            return ft.ExpansionTile(title=C.body(tr("Details"), T.TEXT, weight=ft.FontWeight.W_600), controls=[
                ft.Container(ft.Column(rows, spacing=T.S2), padding=ft.Padding(T.S4, 0, T.S4, T.S4))],
                bgcolor=T.SURFACE, collapsed_bgcolor=T.SURFACE)
        if self.rift:
            rows += [C.kv(tr("Folder"), g.get("game_dir") or ""), C.kv(tr("Program"), g.get("exe") or ""),
                     C.kv(tr("Type"), f"{a['abis'][0]} · {a['graphics']}"),
                     C.kv(tr("Size"), tr("{value:.1f} GiB")
                          .format(value=(a.get('extra', {}).get('data_bytes') or 0) / 2**30))]
        else:
            rows += [C.kv(tr("Version"), a.get("version") or ""),
                     C.kv(tr("ABIs"), ", ".join(a.get("abis") or []), "abis"),
                     C.kv(tr("Graphics"), a.get("graphics") or "", "graphics"), C.kv(tr("APK"), g.get("apk") or ""),
                     C.kv(tr("Data"), tr("{value:.1f} GiB").format(value=(g.get('data_bytes') or 0) / 2**30)
                          + (f" · {g.get('data_dir')}" if g.get("data_dir") else ""))]
        b = g.get("build") or {}
        if b.get("apk"):
            rows.append(C.kv(tr("Last build"), b["apk"]))
        rows.append(C.kv(tr("Recipe"), (g.get("recipe") or {}).get("source", ""), "recipe_source"))
        return ft.ExpansionTile(title=C.body(tr("Details"), T.TEXT, weight=ft.FontWeight.W_600), controls=[
            ft.Container(ft.Column(rows, spacing=T.S2), padding=ft.Padding(T.S4, 0, T.S4, T.S4))],
            bgcolor=T.SURFACE, collapsed_bgcolor=T.SURFACE)

    def build(self) -> ft.Control:
        if not self.g:
            return C.empty_state(ft.Icons.SEARCH_OFF_ROUNDED, tr("Game not found"),
                                 tr("It was removed from the library."),
                                 C.primary(tr("Back to library"), on_click=lambda e: self.app.go("library")))
        about = self.about()
        body = [self.hero(), *self.notes(), self.where(), *([about] if about else []), self.tags(),
                self.recipe_summary()]
        if self.advanced and not self.linux:  # (a Linux app has no patches)
            body.append(self.advanced_panel())
        body.append(self.details())
        app = self.app

        def remember(e):
            app.game_scroll = e.pixels
        col = ft.Column([ft.Container(ft.Column(body, spacing=T.S5), padding=ft.Padding(0, 0, T.S3, T.S6))],
                        scroll=ft.ScrollMode.AUTO, expand=True, on_scroll=remember, scroll_interval=100)
        offset = getattr(app, "game_scroll", 0.0)
        if offset > 0:  # back to where the user was, once the new page is on screen
            def restore():
                try:
                    app.page.run_task(col.scroll_to, offset=offset, duration=0)
                except Exception:  # noqa: BLE001 - the page changed again meanwhile
                    pass
            threading.Timer(0.08, restore).start()
        return col


def _known(pid: str) -> bool:
    try:
        base.get(pid)
        return True
    except KeyError:
        return False
