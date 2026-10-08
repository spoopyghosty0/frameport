from frameport.apk import axml


def test_parse(quest_manifest):
    x = axml.Axml(quest_manifest)
    names = [e.name for e in x.elements()]
    assert names[:3] == ["manifest", "uses-permission", "uses-permission"]
    assert axml.categories(quest_manifest) == {axml.INFO}
    assert x.get_bool("application", "debuggable") is True


def test_fix_launcher(quest_manifest):
    fixed = axml.fix_launcher(quest_manifest)
    assert fixed and axml.categories(fixed) == {axml.LAUNCHER}
    # idempotent: a manifest that already has LAUNCHER is left alone
    assert axml.fix_launcher(fixed) is None
    # everything outside the string pool and the retargeted attribute is untouched
    assert len(fixed) > len(quest_manifest)


def test_nodebug(quest_manifest):
    fixed = axml.set_bool_attr(quest_manifest, "application", "debuggable", False)
    assert axml.Axml(fixed).get_bool("application", "debuggable") is False
    assert len(fixed) == len(quest_manifest)


def test_meta_permissions(quest_manifest):
    assert axml.undeclared_meta_permissions(quest_manifest) == ["com.oculus.permission.USE_SCENE"]
    fixed, added = axml.define_meta_permissions(quest_manifest)
    assert added == ["com.oculus.permission.USE_SCENE"]
    used, declared = axml.used_and_declared_permissions(fixed)
    assert "com.oculus.permission.USE_SCENE" in declared
    assert axml.define_meta_permissions(fixed) is None


def test_edits_compose(quest_manifest):
    m = axml.fix_launcher(quest_manifest)
    m = axml.set_bool_attr(m, "application", "debuggable", False)
    m, _ = axml.define_meta_permissions(m)
    assert axml.categories(m) == {axml.LAUNCHER}
    assert axml.Axml(m).get_bool("application", "debuggable") is False


def _two_activity_manifest():
    """WiiCompiled's shape: a 2D LauncherActivity (MAIN+LAUNCHER) and a QuestActivity (MAIN+VR+DEFAULT)."""
    from conftest import build_axml

    def activity(name, cats):
        out = [("start", "activity", [("name", "str", name)]), ("start", "intent-filter", []),
               ("start", "action", [("name", "str", "android.intent.action.MAIN")]), ("end", "action")]
        for c in cats:
            out += [("start", "category", [("name", "str", c)]), ("end", "category")]
        return out + [("end", "intent-filter"), ("end", "activity")]
    return build_axml([("start", "manifest", [("package", "str", "org.x.game")]), ("start", "application", []),
                       *activity("org.x.game.launcher.LauncherActivity", [axml.LAUNCHER]),
                       *activity("org.x.game.QuestActivity", [axml.VR_CATEGORY, axml.DEFAULT]),
                       ("end", "application"), ("end", "manifest")])


def _filters(manifest):
    x = axml.Axml(manifest)
    names = x.strings()
    return {n.rsplit(".", 1)[-1]: sorted(names[i] for k, _, i in items if k == "category")
            for _, n, items in axml.component_filters(manifest)}


def test_start_activity_moves_launcher_to_the_vr_activity():
    m = _two_activity_manifest()
    assert axml.vr_activity(m) == "org.x.game.QuestActivity"
    fixed = axml.set_start_activity(m, "QuestActivity")
    assert _filters(fixed) == {"LauncherActivity": [axml.INFO],
                               "QuestActivity": sorted([axml.LAUNCHER, axml.DEFAULT])}
    assert axml.vr_activity(fixed) is None  # the VR activity is the launcher now
    assert axml.set_start_activity(fixed, "QuestActivity") is None  # nothing left to do
    assert axml.set_start_activity(m, "NoSuchActivity") is None
    # only those two attribute values changed: the rest parses as before
    assert axml.Axml(fixed).attr_str(next(e for e in axml.Axml(fixed).elements() if e.name == "manifest"),
                                     "package") == "org.x.game"


def _sdk_manifest(min_sdk):
    from conftest import build_axml

    return build_axml([
        ("start", "manifest", [("package", "str", "org.x.app")]),
        ("start", "uses-sdk", [("minSdkVersion", "int", min_sdk), ("targetSdkVersion", "int", 34)]),
        ("end", "uses-sdk"),
        ("end", "manifest"),
    ])


def test_min_sdk(quest_manifest):
    assert axml.min_sdk(_sdk_manifest(34)) == 34
    assert axml.min_sdk(_sdk_manifest(29)) == 29
    assert axml.min_sdk(quest_manifest) is None  # no uses-sdk element


def _twa_manifest(value_kind, value):
    from conftest import build_axml

    return build_axml([
        ("start", "manifest", [("package", "str", "dev.pages.mahjong_vr.twa")]),
        ("start", "uses-sdk", [("minSdkVersion", "int", 23)]),
        ("end", "uses-sdk"),
        ("start", "application", []),
        ("start", "meta-data", [("name", "str", "asset_statements"), ("resource", "ref", 0x7F0F0001)]),
        ("end", "meta-data"),
        ("start", "activity", [("name", "str", "com.google.androidbrowserhelper.trusted.LauncherActivity")]),
        ("start", "meta-data", [("name", "str", "android.support.customtabs.trusted.DEFAULT_URL"),
                                ("value", value_kind, value)]),
        ("end", "meta-data"),
        ("start", "meta-data", [("name", "str", "com.example.flag"), ("value", "bool", True)]),
        ("end", "meta-data"),
        ("end", "activity"),
        ("end", "application"),
        ("end", "manifest"),
    ])


def test_meta_data_and_web_wrapper(tmp_path):
    import zipfile

    from frameport.analysis.detect import analyze, web_wrapper

    m = _twa_manifest("str", "https://mahjong-vr.pages.dev/")
    meta = axml.meta_data(m)
    assert meta == {"asset_statements": 0x7F0F0001, "android.support.customtabs.trusted.DEFAULT_URL":
                    "https://mahjong-vr.pages.dev/", "com.example.flag": True}
    assert web_wrapper(meta, axml.Axml(m).strings()) == {"url": "https://mahjong-vr.pages.dev/"}
    # @string/launchUrl (Bubblewrap) without a resource table: still a wrapper, URL unknown
    ref = _twa_manifest("ref", 0x7F0F0002)
    assert web_wrapper(axml.meta_data(ref), axml.Axml(ref).strings()) == {"url": None}
    assert web_wrapper({}, ["com.example.Main"]) is None
    apk = tmp_path / "twa.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", m)
    a = analyze(apk)
    assert a.extra["web_wrapper"] == {"url": "https://mahjong-vr.pages.dev/"} and a.extra["min_sdk"] == 23
