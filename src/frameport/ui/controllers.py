"""Bundled controller mapping illustrations, installed with the other UI SVG assets at startup."""
from __future__ import annotations

import flet as ft

from ..artwork.thumbs import asset_url
from ..core.paths import user_data_dir
from ..i18n import tr
from . import glyphs as G
from . import theme as T


def diagram_source(mode: int) -> str:
    if mode not in (0, 1, 2, 3):
        mode = 0  # retain an unknown CLI value in the choice while keeping a valid preview asset
    return asset_url(user_data_dir() / G.FOLDER / f"quest-buttons-{mode}.svg")


def diagram(mode: int) -> ft.Image:
    return ft.Image(src=diagram_source(mode), width=T.px(440), height=T.px(322), fit=ft.BoxFit.CONTAIN,
                    semantics_label=tr("Steam Frame controllers: each button shows the Quest Touch button it sends. "
                                       "A dash means it sends nothing."))
