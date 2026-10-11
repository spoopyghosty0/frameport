"""The homepage's example "Install with FramePort" button (deeplink.DEMO_MANIFEST): every form of the link is
recognised as the demo, and nothing about it ever reaches the network."""
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from frameport import deeplink
from frameport.deeplink import DEMO_MANIFEST, LinkError

ENC = quote(DEMO_MANIFEST, safe="")
SITE = Path(__file__).resolve().parents[1] / "site" / "public" / "demo"


@pytest.mark.parametrize("link", [
    f"https://frameport.app/install/?manifest={ENC}",  # the homepage button
    f"https://frameport.app/install?manifest={ENC}",
    f"https://www.frameport.app/install/?manifest={DEMO_MANIFEST}",
    f"frameport://install?manifest={ENC}",
    f"framedrop://install?manifest={ENC}",
    f"https://framedropvr.com/install?manifest={ENC}",
    DEMO_MANIFEST,  # pasted on its own
    f'  "{DEMO_MANIFEST}"  ',
    "https://www.frameport.app/demo/cool-game.json",
])
def test_demo_link_forms(link):
    req = deeplink.parse(link)
    assert req.demo and req.manifest_url == DEMO_MANIFEST


@pytest.mark.parametrize("url", [
    "https://frameport.app/demo/cool-game.json?x=1",
    "https://frameport.app/demo/other.json",
    "https://frameport.app:8443/demo/cool-game.json",
    "https://frameport.app.evil.com/demo/cool-game.json",
    "http://frameport.app/demo/cool-game.json",
    "https://cdn.example.com/demo/cool-game.json",
])
def test_lookalikes_are_not_the_demo(url):
    assert not deeplink.is_demo(url)
    try:
        req = deeplink.parse(f"frameport://install?manifest={quote(url, safe='')}")
    except LinkError:
        return
    assert not req.demo


def test_the_demo_never_touches_the_network(monkeypatch):
    from frameport.core import cache

    def boom(*a, **k):
        raise AssertionError("network used for the demo link")
    monkeypatch.setattr(cache._session, "get", boom)
    monkeypatch.setattr(cache._session, "head", boom)
    monkeypatch.setattr(deeplink.socket, "getaddrinfo", boom)
    m = deeplink.fetch_manifest(deeplink.parse(f"frameport://install?manifest={ENC}"))
    assert m.demo and m.name == deeplink.DEMO_NAME and m.main.kind == deeplink.APK and m.icon is None
    assert deeplink.fetch_icon(m) is None
    with pytest.raises(LinkError, match="demo"):
        deeplink.download(m)
    # also a manifest that only names the demo address
    with pytest.raises(LinkError, match="demo"):
        deeplink.download(deeplink.Manifest("x", m.files, DEMO_MANIFEST))


def test_the_site_publishes_the_same_demo():
    data = json.loads((SITE / "cool-game.json").read_text(encoding="utf-8"))
    site = deeplink.manifest_from_data(data, DEMO_MANIFEST)
    built = deeplink.demo_manifest()
    assert (site.name, [f.url for f in site.files], site.description) == \
        (built.name, [f.url for f in built.files], built.description)
    assert (SITE / "cool-game.svg").read_bytes() == \
        (Path(deeplink.__file__).parent / "ui" / "icons" / "demo-cool-game.svg").read_bytes()


def test_cli_demo_does_nothing(monkeypatch):
    from typer.testing import CliRunner

    from frameport import cli

    def boom(*a, **k):
        raise AssertionError("the demo link must not fetch or download")
    monkeypatch.setattr(deeplink, "fetch_manifest", boom)
    monkeypatch.setattr(deeplink, "download", boom)
    res = CliRunner().invoke(cli.app, ["open-link", f"https://frameport.app/install/?manifest={ENC}"])
    assert res.exit_code == 0, res.output
    assert "demo link" in res.output


def test_dialog_route():
    from frameport.ui.views import link_dialog

    assert link_dialog.route(deeplink.parse(DEMO_MANIFEST)) == "demo"
    assert link_dialog.route(deeplink.parse("https://cdn.example.com/game.framedrop.json")) == "fetch"
    assert (Path(link_dialog.__file__).parents[1] / "icons" / f"{link_dialog.DEMO_COVER}.svg").is_file()
