"""vk_shader_dump (GitHub #140): the Vulkan shim's SPIR-V dump, its setting, the GPU-hang triage per graphics API and
the shader dumps in diagnostics."""
import base64
import hashlib
import importlib.util
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from frameport.core.models import Analysis
from frameport.diag import bundle, redact
from frameport.patches import base
from frameport.patches.settings import UI
from frameport.validate import session
from frameport.validate.triage import graphics_api, triage

ROOT = Path(__file__).resolve().parents[1]
PKG = "com.example.game"
START = "09-28 17:39:01.000  1000  1000 I ActivityManager: Start proc 1147:com.example.game/u0a55 for activity\n"
HANG = "kernel: 1790000000.0 steamos kernel: msm_dpu: [drm:a6xx_hangcheck] hangcheck detected gpu lockup\n"


def fb(msg: str) -> str:
    return f"09-28 17:39:02.000  1147  1174 I FrameBridge: {msg}\n"


def swapchain(fmt: int, result: int = 0) -> str:
    return fb(f"xrCreateSwapchain 1680x1760 format={fmt} samples=1 array=2 faces=1 usage=0x21 flags=0x0 "
              f"result={result}")


def _analysis(**kw):
    d = dict(package=PKG, version="1.0", label="Game", abis=["arm64-v8a"], engine="Unreal", xr="OpenXR",
             graphics="GLES or unknown (no Vulkan declaration)", direct_vrapi=False, libs=["libUE4.so"],
             launcher_activity=None, has_info_category=True, meta_permissions=[], uses_glad_gl=False,
             unity_msaa_levels=0, oculus_os_classes=False, is_overport_output=True, debuggable=False, extra={})
    d.update(kw)
    return Analysis(**d)


def test_setting_registered_like_zink_shader_dump():
    base.load_all()
    p = base.REGISTRY["adapter.vk_shader_dump"]
    assert p.category == "adapter" and p.default == 0 and not p.default_on
    assert "fp_vk_shaders" in p.description
    assert UI["vk_shader_dump"]["group"] == "troubleshooting" and UI["vk_shader_dump"]["level"] == "advanced"
    assert p.applies(_analysis()) and p.applies(_analysis(engine="Other"))
    assert not p.applies(_analysis(engine="Unity", libs=["libunity.so"]))  # no Vulkan shim in Unity games
    assert not p.applies(_analysis(abis=["armeabi-v7a"]))  # the shim is arm64 only


def test_graphics_api_from_swapchain_formats():
    assert graphics_api(swapchain(43)) == "vulkan"
    assert graphics_api(swapchain(35907)) == "gles"
    assert graphics_api(swapchain(43, result=-26)) is None  # refused swapchains don't count
    assert graphics_api(swapchain(43) + swapchain(35907)) is None
    assert graphics_api(START) is None


def test_gpu_hang_suggests_the_dump_for_the_sessions_api():
    def hang(log):
        r = triage(START + log, "UNKNOWN", PKG, kernel=HANG)
        return next(f for f in r.findings if f.id == "gpu-hang")

    assert hang(swapchain(43)).suggest == ["adapter.vk_shader_dump"]
    assert hang(swapchain(35907)).suggest == ["adapter.zink_shader_dump"]
    assert hang("").suggest == ["adapter.vk_shader_dump", "adapter.zink_shader_dump"]  # unknown: both
    # the game's analysis filters too: a Unity game never gets the Vulkan shim
    entry = {"analysis": _analysis(engine="Unity", libs=["libunity.so"]).__dict__}
    assert session.applicable(entry, ["adapter.vk_shader_dump", "adapter.zink_shader_dump"]) == \
        ["adapter.zink_shader_dump"]
    vulkan_unity = {"analysis": _analysis(engine="Unity", graphics="Vulkan (declared in manifest)").__dict__}
    assert session.applicable(vulkan_unity, ["adapter.vk_shader_dump", "adapter.zink_shader_dump"]) == []


def test_artifact_reads_the_setting():
    so = (ROOT / "artifacts/arm64-v8a/libfp_vk.so").read_bytes()
    assert b"vk_shader_dump=" in so and b"fp_vk_shaders" in so and b"index.txt" in so


HOST_TEST = r"""
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#define LOG(...) (fprintf(stderr, __VA_ARGS__), fputc('\n', stderr))
#include "sha256.h"
#include "shader_dump.h"

int main(int argc, char **argv) {
    if (argc < 2 || !dump_init(argv[1])) return 2;
    uint32_t a[5] = {0x07230203, 0x00010000, 0, 10, 0}, b[7] = {0x07230203, 0x00010300, 0, 20, 0, 1, 2};
    dump_module(a, sizeof a);
    dump_module(b, sizeof b);
    dump_module(a, sizeof a);
    return 0;
}
"""


def _host_binary(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc or os.name == "nt":
        pytest.skip("needs a Linux host C compiler")
    src = tmp_path / "t.c"
    src.write_text(HOST_TEST)
    exe = tmp_path / "t"
    subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "native/vkshim"), str(src),
                    "-pthread", "-o", str(exe)], check=True)
    return exe


def test_dump_writes_each_module_once_with_an_index(tmp_path):
    exe = _host_binary(tmp_path)
    files = tmp_path / "files"
    files.mkdir()
    before = int(time.time() * 1000)
    run = subprocess.run([str(exe), str(files)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert "dumping SPIR-V modules to" in run.stderr and "1 module(s) written" in run.stderr
    a = b"".join(w.to_bytes(4, "little") for w in (0x07230203, 0x00010000, 0, 10, 0))
    b = b"".join(w.to_bytes(4, "little") for w in (0x07230203, 0x00010300, 0, 20, 0, 1, 2))
    name_a, name_b = (f"{len(x)}_{hashlib.sha256(x).hexdigest()}.spv" for x in (a, b))
    out = files / "fp_vk_shaders"
    assert sorted(p.name for p in out.iterdir()) == sorted([name_a, name_b, "index.txt"])  # no temporary files
    assert (out / name_a).read_bytes() == a and (out / name_b).read_bytes() == b
    lines = (out / "index.txt").read_text().splitlines()
    assert lines[0].startswith("# start ") and "pid " in lines[0]
    rows = [ln.split() for ln in lines[1:]]
    assert [(r[0], r[3], r[4]) for r in rows] == [("1", name_a, "new"), ("2", name_b, "new"), ("3", name_a, "again")]
    assert all(int(r[2]) >= before - 1000 for r in rows) and int(rows[0][1]) <= int(rows[2][1])
    # a second run: modules already on disk are "known", nothing rewritten; the index keeps both runs
    run = subprocess.run([str(exe), str(files)], capture_output=True, text=True)
    assert run.returncode == 0 and "module(s) written" not in run.stderr
    lines = (out / "index.txt").read_text().splitlines()
    assert sum(ln.startswith("# start ") for ln in lines) == 2
    assert [ln.split()[4] for ln in lines[-3:]] == ["known", "known", "again"]


def _load_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("frameport_agent", ROOT / "agent/frameport_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_agent_collects_the_newest_modules(monkeypatch, tmp_path):
    agent = _load_agent(monkeypatch, tmp_path)
    files = tmp_path / "files"
    d = files / "fp_vk_shaders"
    d.mkdir(parents=True)
    now = time.time()
    names = []
    for i in range(5):
        data = bytes([i]) * 1000
        n = f"{len(data)}_{hashlib.sha256(data).hexdigest()}.spv"
        (d / n).write_bytes(data)
        os.utime(d / n, (now - 100 + i, now - 100 + i))  # i = 4 is the newest
        names.append(n)
    (d / "index.txt").write_text("# start 1 pid 2\n1 0 1000 x.spv new\n")
    out = agent.shader_dumps(str(files), max_bytes=2500)
    res = out["fp_vk_shaders"]
    assert set(res["modules"]) == {names[4], names[3]} and res["total"] == 5  # the newest two fit in 2500 bytes
    assert base64.b64decode(res["modules"][names[4]]) == bytes([4]) * 1000
    assert res["index"].startswith("# start")
    assert "fp_spirv" not in out and agent.shader_dumps(str(tmp_path / "nothing")) == {}
    assert agent.AGENT_VERSION >= 72


def _module(d, i, size=1000):
    data = bytes([i]) * size
    n = f"{len(data)}_{hashlib.sha256(data).hexdigest()}.spv"
    (d / n).write_bytes(data)
    return n


def test_agent_collects_the_newest_session(monkeypatch, tmp_path):
    """GitHub #140: every module of the newest session (index.txt), last used first, not the newest written."""
    agent = _load_agent(monkeypatch, tmp_path)
    files = tmp_path / "files"
    d = files / "fp_vk_shaders"
    d.mkdir(parents=True)
    old = [_module(d, i) for i in range(3)]  # only in the first session
    a, b, c = (_module(d, i) for i in (10, 11, 12))
    big = _module(d, 13, size=3000)
    now = time.time()
    for i, n in enumerate(old):
        os.utime(d / n, (now + i, now + i))  # the newest written: would have won before
    (d / "index.txt").write_text(
        "# start 100 pid 1\n" + "".join(f"{i} 0 1000 {n} new\n" for i, n in enumerate(old + [a]))
        + "# start 200 pid 2\n"
        f"1 0 2000 {a} known\n2 5 2005 {b} new\n3 9 2009 {big} new\n4 10 2010 {c} new\n5 20 2020 {b} again\n"
        f"6 21 2021 {'1_' + 'f' * 64}.spv failed\n")
    res = agent.shader_dumps(str(files))["fp_vk_shaders"]
    assert res["session"] == 5 and res["total"] == 7
    assert list(res["modules"]) == [b, c, big, a]  # last use in the session, newest first; missing file skipped
    assert not set(old) & set(res["modules"])
    assert res["index"].startswith("[... earlier sessions left out ...]\n# start 200") and "pid 1" not in res["index"]
    assert res["skipped"] == 1
    # the total cap: 1000 + 1000 fit, the 3000-byte module doesn't, the next smaller one still does
    capped = agent.shader_dumps(str(files), session_bytes=3000)["fp_vk_shaders"]
    assert list(capped["modules"]) == [b, c, a] and capped["skipped"] == 2
    monkeypatch.setattr(agent, "SHADER_MODULE_BYTES", 2000)  # each module's cap
    assert big not in agent.shader_dumps(str(files))["fp_vk_shaders"]["modules"]
    # an index whose newest session names no module: the newest written, as before
    (d / "index.txt").write_text("# start 300 pid 3\n")
    fallback = agent.shader_dumps(str(files), max_bytes=2500)["fp_vk_shaders"]
    assert "session" not in fallback and list(fallback["modules"]) == [old[2], old[1]]
    assert agent.AGENT_VERSION >= 74


def test_bundle_drops_old_modules_to_fit(tmp_path):
    w = bundle._Writer(redact.Redactor())
    names = []
    for _ in range(16):
        data = os.urandom(20000)  # incompressible
        names.append(f"{len(data)}_{hashlib.sha256(data).hexdigest()}.spv")
        w.files["s/" + names[-1]] = data
    w.fit(limit=200000)
    kept = [n for n in names if "s/" + n in w.files]
    assert kept and kept == names[:len(kept)] and len(kept) < 16  # the newest (first) stay
    assert any("shader module" in m for m in w.warnings)
    w2 = bundle._Writer(redact.Redactor())
    bundle.add_shader_dumps(w2, "g/", {"fp_vk_shaders": {"total": 50, "session": 3, "modules": {}}})
    assert any("newest session" in m for m in w2.warnings)


def test_bundle_writes_the_dumps(tmp_path):
    w = bundle._Writer(redact.Redactor())
    data = b"\x03\x02\x23\x07" * 5
    name = f"{len(data)}_{hashlib.sha256(data).hexdigest()}.spv"
    bundle.add_shader_dumps(w, "games/x/target/shaders/", {
        "fp_vk_shaders": {"total": 3, "index": "1 0 1 a.spv new /home/someone/x\n",
                          "modules": {name: base64.b64encode(data).decode(), "../evil.spv": "AAAA"}},
        "../other": {"modules": {name: "AAAA"}}})
    assert w.files["games/x/target/shaders/fp_vk_shaders/" + name] == data
    assert b"someone" not in w.files["games/x/target/shaders/fp_vk_shaders/index.txt"]
    assert not any("evil" in n or "other" in n for n in w.files)
    assert any("older module" in m for m in w.warnings)
