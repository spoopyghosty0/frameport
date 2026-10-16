"""Offline launch settings, bounded bench and fp_mem v3; never loads SteamVR or launches Wine."""
import ctypes
import importlib.util
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def perf(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in ("fexrift.py", "fexrift_perf.py", "fexrift_games.json"):
        shutil.copyfile(ROOT / "agent" / name, artifacts / name)
    spec = importlib.util.spec_from_file_location("fex_perf_core", artifacts / "fexrift.py")
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    core.load_extensions()
    # A registered function's globals are the isolated extension's namespace (no sys.path imports under -I).
    mod = SimpleNamespace(**core.COMMANDS["bench"].__globals__)
    return core, mod


def test_settings_defaults_overrides_and_reset(perf):
    core, p = perf
    app = "oculus-first-contact"
    baseline = p.settings_get({"app": app})
    assert baseline["settings"]["fp_mem"] is True
    assert baseline["settings"]["bigcache_mb"] == 256
    assert p.settings_get({"app": "other"})["settings"]["fp_mem"] is False
    p.settings_set({"app": app, "eye_scale": 1.25, "fp_mem": False, "cores": "0-3,7"})
    got = core.COMMANDS["settings_get"]({"app": app})
    assert got["settings"]["eye_scale"] == 1.25 and got["settings"]["fp_mem"] is False
    p.settings_set({"app": app, "fp_mem": None})
    assert p.settings_get({"app": app})["settings"]["fp_mem"] is True
    path = Path(core.ROOT) / "settings" / (app + ".json")
    assert json.loads(path.read_text()) == {"eye_scale": 1.25, "cores": "0-3,7"}
    assert "fp_mem" not in p.settings_get({"app": app})["overrides"]
    core.GAMES[app]["perf"] = {"motion_smoothing": 1}
    assert p.settings_get({"app": app})["settings"]["motion_smoothing"] == 1


@pytest.mark.parametrize("values", [{"eye_scale": True}, {"eye_scale": 1.4}, {"cores": "7-4"},
                                   {"cores": "4-7;id"}, {"cores": "8"}, {"ntsync": "false"},
                                   {"dxgi_max_frame_latency": -1}, {"dxgi_sync_interval": True},
                                   {"bigcache_mb": 0}, {"motion_smoothing": 3}, {"unknown": 1},
                                   {"fex": {"FEX_ENV": "LD_PRELOAD=bad"}},
                                   {"fex": {"FEX_X87REDUCEDPRECISION": "1;id"}}])
def test_reject_settings_without_partial_write(perf, values):
    core, p = perf
    with pytest.raises(ValueError):
        p.settings_set(dict(values, app="test-app", fp_mem_zero=True))
    assert not Path(p.settings_path("test-app")).exists()


@pytest.mark.parametrize("app", ["../escape", "a/b", "..", "", "a'bad", "a\n", None])
def test_app_path_confined(perf, app):
    _, p = perf
    with pytest.raises(ValueError):
        p.settings_get({"app": app})


def pe_revive():
    # One synthetic .text section, same RVA->file-offset shift as 3.2.0. Tests don't need a proprietary DLL.
    d = bytearray(0x13000)
    d[:2] = b"MZ"
    struct.pack_into("<I", d, 0x3c, 0x80)
    d[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", d, 0x86, 1)
    struct.pack_into("<H", d, 0x94, 0xf0)
    struct.pack_into("<8sIIII", d, 0x80 + 24 + 0xf0, b".text", 0x13000, 0x1000, 0x13000, 0x400)
    d[0x12291] = 0xc3
    d[0x12292:0x122a0] = b"\xcc" * 14
    d[0x122ab:0x122b3] = bytes.fromhex("f30f102515bb0400")
    return bytes(d)


def test_eye_scale_changes_only_instruction_and_padding(perf):
    _, p = perf
    d = pe_revive()
    for scale in (1.0, 1.25, 1.5):
        out = p.eye_variant(d, scale)
        assert struct.unpack_from("<f", out, 0x1229c)[0] == scale
        assert out[0x122ab:0x122af] == bytes.fromhex("f30f1025")
        assert 0x12eb3 + struct.unpack_from("<i", out, 0x122af)[0] == 0x12e9c
        assert all(a == b for i, (a, b) in enumerate(zip(d, out, strict=True))
                   if not (0x1229c <= i < 0x122a0 or 0x122af <= i < 0x122b3))
    bad = bytearray(d)
    bad[0x1229c] = 0x90
    with pytest.raises(ValueError, match="instruction/padding"):
        p.eye_variant(bad, 1.25)


def test_prepare_scales_hardlinks_and_private_dll(perf, monkeypatch):
    core, p = perf
    src = Path(core.REVIVE)
    src.mkdir(parents=True)
    (src / "LibRevive64.dll").write_bytes(pe_revive())
    (src / "ReviveInjector.exe").write_bytes(b"fake injector")
    # Exercise the real copying/patching after faking only the outer pinned-upload hash check.
    monkeypatch.setattr(p.hashlib, "sha256", lambda _: SimpleNamespace(hexdigest=lambda:
                        "eda2a449d230832c95734a03e8c83eeb531668ce676693f636e19c315765af4e"))
    p.prepare_scales()
    for scale in (1.0, 1.25, 1.5):
        dest = Path(p.scale_folder(scale))
        assert os.stat(dest / "ReviveInjector.exe").st_ino == os.stat(src / "ReviveInjector.exe").st_ino
        assert os.stat(dest / "LibRevive64.dll").st_ino != os.stat(src / "LibRevive64.dll").st_ino
        assert struct.unpack_from("<f", (dest / "LibRevive64.dll").read_bytes(), 0x1229c)[0] == scale
    assert (src / "LibRevive64.dll").read_bytes() == pe_revive()


def test_prepare_reads_new_settings_each_launch(perf, tmp_path):
    core, p = perf
    app = "oculus-first-contact"
    folder = Path(p.scale_folder(1.5))
    folder.mkdir(parents=True)
    (folder / "LibRevive64.dll").touch()
    p.settings_set({"app": app, "fp_mem": False, "ntsync": False, "fp_mem_zero": True,
                    "fex": {"FEX_X87REDUCEDPRECISION": 1}, "motion_smoothing": 0})
    first = p.perf_prepare({"app": app})
    config = Path(core.ROOT) / "settings" / (app + ".dxvk.conf")
    # all DXVK settings at their defaults: no config at all (= the launch verified in the headset)
    assert not config.exists() and "DXVK_CONFIG_FILE" not in first["shell"]
    assert f"FP_REVIVE_DIR={core.REVIVE}" in first["shell"] and "WINE_LARGE_ADDRESS_AWARE" not in first["shell"]
    assert "export FP_MEM=''" in first["shell"] and "PROTON_NO_NTSYNC=1" in first["shell"]
    assert 'FEX_X87REDUCEDPRECISION=1' in first["shell"] and 'FP_MEM_ZERO=1' in first["shell"]
    p.settings_set({"app": app, "dxvk_disable_msaa": True, "motion_smoothing": 1})
    second = p.perf_prepare({"app": app})
    assert "d3d11.disableMsaa = True" in config.read_text()
    assert '\"value\": 1' in second["shell"]
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    core.write_launcher(app, core.GAMES[app], str(anchor))
    text = (anchor / "launch.sh").read_text()
    assert "perf_prepare" in text and text.index("perf_prepare") < text.index("OVRServer_x64.exe")
    assert 'taskset -c "$FP_CORES"' in text and '"$FP_REVIVE_DIR/ReviveInjector.exe"' in text
    assert 'vrsetting "$FP_MOTION_SMOOTHING"' in text
    subprocess.run(["bash", "-n", str(anchor / "launch.sh")], check=True)


def test_drm_parsing_and_counter_reset(perf, tmp_path):
    _, p = perf
    text = "drm-client-id: 72\ndrm-pdev: platform:gpu\ndrm-engine-gpu: 1234 ns\ndrm-engine-copy: 44 ns\n"
    assert p.drm_engines(text) == {"gpu": 1234, "copy": 44}
    assert p.drm_engines("drm-engine-gpu: nan ns\ndrm-engine-gpu: 10 ms") == {}
    base = tmp_path / "32"
    (base / "fdinfo").mkdir(parents=True)
    for fd in ("1", "2"):
        (base / "fdinfo" / fd).write_text(text)
    (base / "task/33").mkdir(parents=True)
    (base / "task/33/wchan").write_text("futex_wait_queue")
    engines, waits = p.process_sample([32], str(tmp_path))
    assert sum(engines.values()) == 1278 and waits == {"futex_wait_queue": 1}
    assert p.gpu_delta({"a": 99, "b": 10}, {"a": 120, "b": 1, "new": 999}) == 21
    assert ctypes.sizeof(p.FrameTiming) == 184


@pytest.mark.parametrize("row,busy", [(("steam", "/usr/bin/steam"), False),
                                    (("vrserver", "/opt/steamvr/vrserver"), False),
                                    (("reaper", "reaper SteamLaunch AppId=250820 -- vrstartup"), False),
                                    (("reaper", "reaper SteamLaunch AppId=123 -- native-game"), True),
                                    (("wine", "game.exe -foo"), True),
                                    (("bash", "bash /path/proton run game"), True),
                                    (("wineserver", "wineserver -p"), True),
                                    (("conmon", "conmon --name lepton-steamlaunch-123"), True)])
def test_busy_guard(perf, row, busy):
    _, p = perf
    assert bool(p.busy_processes({10: row})) == busy


class FakeVR:
    def activity(self):
        return 3

    def screen_timeout(self):
        return 300.0

    def timings(self):
        return []

    def scene_pid(self):
        return 42


def fake_bench(perf, monkeypatch):
    core, p = perf
    # Patch the actual function globals rather than the SimpleNamespace's copies.
    g = core.COMMANDS["bench"].__globals__
    events = []
    monkeypatch.setitem(g, "processes", lambda: {})
    monkeypatch.setitem(g, "recent_steam_input", lambda _: False)
    monkeypatch.setitem(g, "vr_set", lambda *args: events.append(args))
    monkeypatch.setitem(g, "stop_launcher", lambda child: events.append(("stop", child)))
    monkeypatch.setattr(p.signal, "signal", lambda *args: 0)
    return p, g, events


@pytest.mark.parametrize("failure", ["first_setting", "second_setting", "launch", "sample", "stop", "restore"])
def test_bench_restores_even_on_failure(perf, monkeypatch, failure):
    p, g, events = fake_bench(perf, monkeypatch)
    vr = FakeVR()
    def setting(section, key, kind, value):
        events.append((section, key, kind, value))
        if ((failure == "first_setting" and value is False) or
            (failure == "second_setting" and key == "turnOffScreensTimeout" and value != 300.0) or
            (failure == "restore" and value is True)):
            raise RuntimeError(failure)
    monkeypatch.setitem(g, "vr_set", setting)
    child = SimpleNamespace(poll=lambda: None)
    def launch(*args, **kwargs):
        if failure == "launch":
            raise RuntimeError("launch")
        return child
    monkeypatch.setattr(p.subprocess, "Popen", launch)
    if failure == "sample":
        vr.timings = lambda: (_ for _ in ()).throw(RuntimeError("sample"))
    if failure == "stop":
        def stop(child):
            events.append(("stop", child))
            raise RuntimeError("stop")
        monkeypatch.setitem(g, "stop_launcher", stop)
    clock = [0.0]
    monkeypatch.setattr(p.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(p.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    with pytest.raises(RuntimeError, match=failure):
        p.bench_run({"app": "test", "seconds": 1, "exe": "game.exe"}, "/fake/launch.sh", vr)
    assert ("power", "pauseCompositorOnStandby", "b", True) in events
    assert events[-1] == ("power", "turnOffScreensTimeout", "f", 300.0)
    assert any(e[0] == "stop" for e in events)


@pytest.mark.parametrize("activity,seconds", [(0, 1), (1, 1), (2, 1), (4, 1), (-1, 1), (3, 0), (3, 121), (3, True)])
def test_bench_guard_no_side_effects(perf, monkeypatch, activity, seconds):
    p, _, events = fake_bench(perf, monkeypatch)
    vr = FakeVR()
    vr.activity = lambda: activity
    with pytest.raises(ValueError):
        p.bench_run({"app": "test", "seconds": seconds}, "/fake/launch.sh", vr)
    assert events == []


def test_bench_summary_deduplicates_ring_frames(perf, monkeypatch):
    p, g, events = fake_bench(perf, monkeypatch)
    count, clock = [0], [0.0]
    def rows():
        count[0] += 1
        return {} if count[0] == 1 else {42: ("game", "game.exe")}
    monkeypatch.setitem(g, "processes", rows)
    monkeypatch.setitem(g, "process_sample", lambda _: ({"gpu": int(clock[0] * 1e9)}, {"0": 1}))
    monkeypatch.setattr(p.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(p.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(p.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(poll=lambda: None))
    vr = FakeVR()
    record = {"index": 3, "presents": 3, "gpu_ms": 8, "cpu_ms": 10, "system_time": 1.0}
    vr.timings = lambda: [record] if count[0] > 1 else []
    summary = p.bench_run({"app": "test", "seconds": 2, "exe": "game.exe"}, "/fake/launch.sh", vr)
    assert summary["frames"] == summary["hitches"] == 1
    assert summary["valid"] is False and summary["fps"] is None
    assert summary["game_gpu_busy_pct"] == 100.0
    assert summary["render_gpu_ms"] == 8
    assert events[-1] == ("power", "turnOffScreensTimeout", "f", 300.0)


C_HARNESS = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
/* Disable only constructors and the unused mmap64 alias; test the production functions with fake syscalls. */
#define __attribute__(x)
#define mmap fp_mmap
#define mmap64 fp_mmap64
#define munmap fp_munmap
#define mprotect fp_mprotect
#define madvise fp_madvise
int fp_madvise(void *, size_t, int);
#include "fp_mem.c"
static char region[16384] __attribute__((aligned(4096)));
static size_t advised_len, unmapped_len;
static int advice_fail, map_calls;
static void *fake_map(void *a, size_t n, int p, int f, int fd, off_t off) { map_calls++; return a; }
static int fake_unmap(void *a, size_t n) { unmapped_len += n; return 0; }
static int fake_protect(void *a, size_t n, int p) { return 0; }
int fp_madvise(void *a, size_t n, int advice) {
    assert(advice == MADV_DONTNEED);
    advised_len += n;
    if (advice_fail) return -1;
    memset(a, 0, n);
    return 0;
}
static void reset(void) {
    memset(cache, 0, sizeof(cache)); memset(region, 0x5a, sizeof(region));
    kept_total = advised_len = unmapped_len = map_calls = advice_fail = 0;
    cache_min = 4096; thp = 0;
}
int main(int argc, char **argv) {
    fp_mem_init();
    real_mmap = fake_map; real_munmap = fake_unmap; real_mprotect = fake_protect;
    int expected_zero = atoi(argv[1]);
    assert(zero == expected_zero);
    for (int mode = 0; mode < 3; mode++) {
        reset();
        size_t old = 8192, want = mode == 0 ? old : mode == 1 ? 4096 : 12288;
        assert(!fp_munmap(region, old)); assert(kept_total == old);
        assert(fp_mmap(region, want, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS|MAP_FIXED, -1, 0) == region);
        assert(!cache[0].used && kept_total == 0 && hits == (unsigned long)mode+1);
        assert(advised_len == (expected_zero ? (want < old ? want : old) : 0));
        assert(region[0] == (expected_zero ? 0 : 0x5a));
        assert(unmapped_len == (want < old ? old-want : 0));
        if (want > old) assert(region[old] == 0x5a); /* fake fresh tail was not advised */
    }
    if (expected_zero) {
        assert(zero_calls == 3 && zero_bytes == 20480 && zero_failures == 0);
        reset(); store(region, 8192, 1, KEPT_PROT); advice_fail = 1;
        assert(fp_mmap(region, 8192, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS|MAP_FIXED, -1, 0) == region);
        assert(zero_failures == 1 && map_calls == 1 && drops == 1 && kept_total == 0);
        reset(); store(region, 8192, 1, KEPT_PROT); advice_fail = 1;
        fp_mmap(region, 12288, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS|MAP_FIXED, -1, 0);
        assert(zero_failures == 2 && map_calls == 2 && kept_total == 0 && unmapped_len == 12288);
    } else assert(zero_calls == 0 && zero_bytes == 0 && zero_failures == 0);
    return 0;
}
'''


@pytest.mark.parametrize("value,zero", [(None, 0), ("0", 0), ("1", 1)])
def test_fp_mem_fake_syscalls(tmp_path, value, zero):
    cc = shutil.which("cc")
    if not cc:
        pytest.skip("host C compiler unavailable")
    harness = tmp_path / "harness.c"
    harness.write_text(C_HARNESS)
    binary = tmp_path / "harness"
    subprocess.run([cc, "-O0", "-I", str(ROOT / "native/fexwine"), str(harness), "-ldl", "-lpthread", "-o",
                    str(binary)], check=True, capture_output=True, text=True)
    env = dict(os.environ)
    env.pop("FP_MEM_ZERO", None)
    if value is not None:
        env["FP_MEM_ZERO"] = value
    subprocess.run([str(binary), str(zero)], check=True, capture_output=True, text=True, env=env)


def test_cli_settings_and_bench_routing(monkeypatch):
    from typer.testing import CliRunner

    from frameport import cli

    calls = []
    target = SimpleNamespace(frame=SimpleNamespace(agent=lambda name, **args: calls.append((name, args)) or {}))
    monkeypatch.setattr(cli, "_fex_target", lambda frame: target)
    monkeypatch.setattr(cli, "_update_hint", lambda: None)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["fex", "settings", "test-app", "cores=7", "ntsync=false", "eye_scale=1.25",
                                     'fex={"FEX_X87REDUCEDPRECISION":1}', "fp_mem_zero=null"])
    assert result.exit_code == 0, result.output
    assert calls[-1] == ("fex_settings", {"app": "test-app", "values": {
        "cores": "7", "ntsync": False, "eye_scale": 1.25, "fex": {"FEX_X87REDUCEDPRECISION": 1},
        "fp_mem_zero": None}})
    assert runner.invoke(cli.app, ["fex", "settings", "test-app"]).exit_code == 0
    assert calls[-1] == ("fex_settings", {"app": "test-app"})
    assert runner.invoke(cli.app, ["fex", "settings", "test-app", "cores=null"]).exit_code == 0
    assert calls[-1] == ("fex_settings", {"app": "test-app", "values": {"cores": None}})
    result = runner.invoke(cli.app, ["fex", "bench", "test-app", "--seconds", "3"])
    assert result.exit_code == 0 and result.output.count("\n") == 1
    assert calls[-1] == ("fex_bench", {"app": "test-app", "seconds": 3, "timeout": 253})
    count = len(calls)
    assert runner.invoke(cli.app, ["fex", "bench", "test-app", "--seconds", "121"]).exit_code == 2
    assert runner.invoke(cli.app, ["fex", "settings", "test-app", "broken"]).exit_code == 2
    assert len(calls) == count


def test_agent_perf_commands_use_installed_launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("perf_agent", ROOT / "agent/frameport_agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    anchor = Path(agent.ANCHORS) / "rift.test"
    anchor.mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"kind": "frame_fex", "app": "test-app"}))
    calls = []
    monkeypatch.setattr(agent, "fex_helper", lambda command, args, timeout: calls.append((command, args, timeout)))
    monkeypatch.setattr(agent, "game_running", lambda: False)
    agent.cmd_fex_settings({"app": "test-app", "values": {"ntsync": False}})
    assert calls[-1] == ("settings_set", {"app": "test-app", "ntsync": False}, 60)
    agent.cmd_fex_settings({"app": "test-app"})
    assert calls[-1] == ("settings_get", {"app": "test-app"}, 60)
    agent.cmd_fex_bench({"app": "test-app", "seconds": 5})
    assert calls[-1] == ("bench", {"app": "test-app", "seconds": 5, "launcher": str(anchor / "launch.sh")}, 245)
    count = len(calls)
    with pytest.raises(agent.AgentError):
        agent.cmd_fex_bench({"app": "test-app", "seconds": 121})
    with pytest.raises(agent.AgentError):
        agent.cmd_fex_settings({"app": "test-app", "values": {"app": "other"}})
    monkeypatch.setattr(agent, "game_running", lambda: True)
    with pytest.raises(agent.AgentError, match="running"):
        agent.cmd_fex_bench({"app": "test-app"})
    assert len(calls) == count


def test_profile_dxvk_lines_share_the_one_config(perf, tmp_path):
    """The compat profile's dxvk.conf lines and these settings end up in one per-game config; settings win."""
    core, p = perf
    app = "oculus-first-contact"
    folder = Path(p.scale_folder(1.5))
    folder.mkdir(parents=True)
    (folder / "LibRevive64.dll").touch()
    p.settings_set({"app": app, "dxvk_disable_msaa": True})
    p.perf_prepare({"app": app, "dxvk": ["dxgi.maxFrameRate = 72", "d3d11.disableMsaa = False", "bad\nline"]})
    text = (Path(core.ROOT) / "settings" / (app + ".dxvk.conf")).read_text()
    assert "dxgi.maxFrameRate = 72" in text and "bad" not in text
    assert text.count("d3d11.disableMsaa") == 1 and "d3d11.disableMsaa = True" in text   # the setting wins
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    core.write_launcher(app, dict(core.GAMES[app], dxvk=["dxgi.maxFrameRate = 72"]), str(anchor))
    assert "dxgi.maxFrameRate = 72" in (anchor / "launch.sh").read_text()


def test_prepare_scales_accepts_the_real_setup_upload(perf, tmp_path):
    """Against the real Revive 3.2.0 DLL patched the way `fex setup` uploads it (skipped without Revive)."""
    from frameport import fexrift as pc
    from frameport.tools import revive as revive_tool

    try:
        stock = revive_tool.revive_dir()
    except Exception:
        pytest.skip("no Revive install")
    if not stock or not (Path(stock) / "LibRevive64.dll").is_file():
        pytest.skip("no Revive install")
    core, p = perf
    pc.patch_revive(Path(stock), Path(core.REVIVE))
    p.prepare_scales()
    for scale in (1.0, 1.25, 1.5):
        dll = (Path(p.scale_folder(scale)) / "LibRevive64.dll").read_bytes()
        assert struct.unpack_from("<f", dll, 0x1229c)[0] == scale


@pytest.mark.parametrize("how", ["crash", "result", "error"])
def test_bench_guarded_restores_display_when_the_measurement_dies(perf, monkeypatch, tmp_path, how):
    """A native crash in the ctypes SteamVR calls skips bench_run's finally (seen on the Frame): the parent must
    still restore the display settings and stop the game from the guard file."""
    core, p = perf
    g = core.COMMANDS["bench"].__globals__
    Path(core.ROOT).mkdir(parents=True, exist_ok=True)
    log = tmp_path / "events.txt"

    def run(args, launcher, vr):
        g["guard_write"](screen=300.0, pgid=999999)
        if how == "crash":
            os.kill(os.getpid(), 9)                   # what a segfault in vrclient.so looks like to Python
        if how == "error":
            raise ValueError("refused")
        return {"frames": 3}
    monkeypatch.setitem(g, "VR", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setitem(g, "bench_run", run)
    monkeypatch.setitem(g, "vrsetting_subprocess",
                        lambda *a: log.open("a").write(json.dumps(a) + "\n"))
    def run_cmd(argv, **k):
        if str(argv[0]).endswith("wineserver"):
            log.open("a").write('["wineserver -k"]\n')
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    monkeypatch.setattr(p.subprocess, "run", run_cmd)
    if how == "crash":
        with pytest.raises(ValueError, match="died .*display settings restored"):
            g["bench_guarded"]({"app": "x"}, "/fake/launch.sh")
        lines = [json.loads(l) for l in log.read_text().splitlines()]
        assert ["power", "pauseCompositorOnStandby", "b", True] in lines
        assert ["power", "turnOffScreensTimeout", "f", 300.0] in lines and ["wineserver -k"] in lines
    elif how == "error":
        with pytest.raises(ValueError, match="refused"):
            g["bench_guarded"]({"app": "x"}, "/fake/launch.sh")
    else:
        assert g["bench_guarded"]({"app": "x"}, "/fake/launch.sh") == {"frames": 3}
    assert not Path(g["guard_path"]()).exists()


def test_vr_set_runs_each_write_in_its_own_process(perf, monkeypatch):
    core, p = perf
    g = core.COMMANDS["bench"].__globals__
    calls = []

    def run(argv, **k):
        calls.append(argv)
        ok = json.loads(argv[-1])["value"] is not None
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "result": {"error": 0 if ok else 1}}))
    monkeypatch.setattr(p.subprocess, "run", run)
    monkeypatch.setitem(g, "CORE", SimpleNamespace(HERE="/a", cmd_vrsetting=lambda a: pytest.fail("in-process")))
    g["vr_set"]("power", "pauseCompositorOnStandby", "b", False)
    g["vr_set"]("power", "turnOffScreensTimeout", "f", 30.0)
    assert len(calls) == 2 and all(c[-2] == "vrsetting" for c in calls)
    with pytest.raises(ValueError, match="refused"):
        g["vr_set"]("power", "x", "b", None)
