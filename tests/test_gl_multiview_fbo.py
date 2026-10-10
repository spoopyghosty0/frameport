"""frame.gl_multiview_fbo (GitHub #77): multiview programs drawing into single-view framebuffers get a twin."""
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from frameport.analysis import elf
from frameport.analysis.detect import ANALYSIS_VERSION, multiview_glsl_libs
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.frame import artifact
from frameport.patches.frame.gl_multiview_fbo import GLES, SHIM
from frameport.patches.settings import adapter_settings
from frameport.validate.triage import triage

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("fixtures")
ENGINE = "libdoom3.so"


def _analysis(**kw):
    d = dict(package="com.drbeef.doom3quest", version="1.4.8", label="Doom3Quest", abis=["arm64-v8a"], engine="Other",
             xr="OpenXR", graphics="GLES or unknown (no Vulkan declaration)", direct_vrapi=False,
             libs=[ENGINE, "libSDL2.so", "libopenxr_loader.so"], launcher_activity=None, has_info_category=False,
             meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0, oculus_os_classes=False,
             is_overport_output=True, debuggable=False, extra={"gl_multiview_libs": [ENGINE]})
    d.update(kw)
    return Analysis(**d)


def test_rewriter_on_doom3quest_shaders(tmp_path):
    """Every Doom3Quest vertex shader loses `layout(num_views=NUM_VIEWS) in;`, every stage its gl_ViewID_OVR; the
    rest of each source is unchanged (C test: native/glmv/glmv_rewrite.h)."""
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc or os.name == "nt":
        pytest.skip("needs a POSIX host C compiler")
    shaders = sorted((FIXTURES / "doom3quest_glsl").glob("*.cpp"))
    assert len(shaders) == 19
    binary = tmp_path / "glmv-rewrite-test"
    subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "native/glmv"),
                    str(FIXTURES / "src/glmv_rewrite_test.c"), "-o", str(binary)], check=True)
    out = tmp_path / "rewritten"
    out.mkdir()
    subprocess.run([str(binary), str(out), *map(str, shaders)], check=True)
    rewritten = sorted(out.glob("*.glsl"))
    assert len(rewritten) == 19
    for f in rewritten:
        text = f.read_text()
        assert "num_views" not in text and "gl_ViewID_OVR" not in text
        assert text.lstrip().startswith("#version 300 es")
    vp = (out / "diffuseMapShaderVP.glsl").read_text()
    assert "u_viewMatrices[(0u)         ]" in vp and "#extension GL_OVR_multiview2 : enable" in vp
    # optional: a real GLSL ES front end (glslangValidator: OpenGL ES 3.00 without SPIR-V)
    validator = shutil.which("glslangValidator")
    if validator:
        for f in rewritten:
            stage = "vert" if f.stem.endswith("VP") else "frag"
            subprocess.run([validator, "-S", stage, str(f)], check=True, capture_output=True)


def _khronos_headers(dest: Path) -> Path | None:
    """GLES3/EGL/KHR headers only (never a whole Android sysroot on a host build): the system's, else the NDK's."""
    candidates = [Path("/usr/include")]
    candidates += sorted((ROOT / "native/.cache").glob("ndk-*/toolchains/llvm/prebuilt/*/sysroot/usr/include"))
    for inc in candidates:
        if all((inc / d).is_dir() for d in ("GLES3", "EGL", "KHR")):
            for d in ("GLES3", "EGL", "KHR"):
                shutil.copytree(inc / d, dest / d)
            return dest
    return None


def test_interposer_against_a_stand_in_gl(tmp_path):
    """glmv.c itself, built for the host against a stand-in GL that drops multiview draws into single-view
    framebuffers like Mesa: eye-buffer draws stay unchanged, HUD/window draws go through the single-view twin with the
    original's uniforms, attribute locations and block bindings, the original is bound again afterwards."""
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc or os.name == "nt" or not Path("/proc").exists():
        pytest.skip("needs a Linux host C compiler")
    inc = _khronos_headers(tmp_path / "include")
    if inc is None:
        pytest.skip("needs the GLES3/EGL/KHR headers (libgles-dev, or the NDK in native/.cache)")
    src = FIXTURES / "src"
    out = tmp_path / "lib"
    out.mkdir()
    warn = ["-Wall", "-Wextra", "-Werror"]
    subprocess.run([cc, "-std=gnu11", *warn, "-shared", "-fPIC", "-fvisibility=hidden", "-I", str(inc),
                    str(src / "glmv_fake_gles.c"), "-Wl,-soname,libglmv_fake_gles.so", "-ldl",
                    "-o", str(out / "libglmv_fake_gles.so")], check=True)
    subprocess.run([cc, "-std=gnu11", *warn, "-shared", "-fPIC", "-fvisibility=hidden", "-DEGL_NO_X11",
                    "-DMESA_EGL_NO_X11_HEADERS", '-DGLMV_GLES_LIB="libglmv_fake_gles.so"',
                    '-DGLMV_EGL_LIB="libglmv_fake_gles.so"', "-I", str(src / "host_include"), "-I", str(inc),
                    str(ROOT / "native/glmv/glmv.c"), "-Wl,--no-as-needed", "-L", str(out), "-lglmv_fake_gles",
                    "-Wl,--as-needed", "-Wl,-rpath,$ORIGIN", "-ldl", "-lpthread",
                    "-o", str(out / "libfpglmv_host.so")], check=True)
    subprocess.run([cc, "-std=gnu11", *warn, "-I", str(inc), str(src / "glmv_host_test.c"), "-ldl", "-pthread",
                    "-o", str(out / "glmv_host_test")], check=True)
    run = subprocess.run([str(out / "glmv_host_test"), str(out / "libfpglmv_host.so")], capture_output=True,
                         text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "GLMV: library active: single-view draws of multiview programs (gl_mv_debug=0" in run.stderr
    assert "GLMV: twin built: program 1 -> " in run.stderr
    assert "1 num_views layouts and 1 gl_ViewID_OVR replaced" in run.stderr
    # the same with gl_mv_debug=1 (from Lepton's per-game settings.conf)
    conf = tmp_path / "settings.conf"
    conf.write_text("scale=1.0\ngl_mv_debug=1\n")
    run = subprocess.run([str(out / "glmv_host_test"), str(out / "libfpglmv_host.so")], capture_output=True,
                         text=True, env={**os.environ, "FRAMEBRIDGE_CONFIG": str(conf)})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "(gl_mv_debug=1" in run.stderr and "GL error" not in run.stderr


def test_artifact_interposes_gl():
    lib = artifact("arm64-v8a", SHIM)
    assert len(SHIM) == len(GLES)  # the dlopen string is rewritten in place
    assert elf.soname(lib) == SHIM
    assert elf.needed(lib)[0] == GLES  # dlsym on its handle reaches every GL function it doesn't wrap
    exports = elf.dyn_symbols(lib, True)
    assert {"glUseProgram", "glLinkProgram", "glDeleteProgram", "glBindFramebuffer", "glFramebufferTexture2D",
            "glFramebufferRenderbuffer", "glFramebufferTextureLayer", "glDeleteFramebuffers", "glDrawArrays",
            "glDrawElements", "glDrawElementsInstanced", "eglGetProcAddress"} <= exports
    assert not {s for s in exports if not s.startswith(("gl", "egl"))}  # nothing else leaks into the game's lookups
    assert b"GLMV" in lib and b"gl_mv_debug=" in lib


def test_analysis_finds_multiview_glsl():
    engine = (FIXTURES / "libfakemultiview_arm64.so").read_bytes()
    plain = (FIXTURES / "libfakeengine_arm64.so").read_bytes()
    assert multiview_glsl_libs({"libgame.so": engine, "libother.so": plain, "libopenxr_loader.so": engine,
                                "libnot_elf.so": b"num_views gl_ViewID_OVR"}) == ["libgame.so"]
    assert ANALYSIS_VERSION >= 6  # older library entries are analysed again for the new field


def test_detect_and_applies():
    base.load_all()
    p = base.get("frame.gl_multiview_fbo")
    s = p.detect(_analysis())
    assert s is not None and not s.recommended and not p.default_on  # opt-in until verified in a headset
    assert p.applies(_analysis())
    assert not p.applies(_analysis(extra={}))
    assert not p.applies(_analysis(engine="Unity"))
    assert not p.applies(_analysis(graphics="Vulkan (declared in manifest)"))
    assert not p.applies(_analysis(abis=["armeabi-v7a"]))
    debug = base.get("adapter.gl_mv_debug")
    assert debug.default == 0 and debug.applies(_analysis()) and not debug.applies(_analysis(extra={}))
    assert adapter_settings({"adapter.gl_mv_debug": {"value": 1}})["gl_mv_debug"] == 1


def _apk(tmp_path, quest_manifest, engine: bytes):
    p = tmp_path / "in.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", quest_manifest)
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr(f"lib/arm64-v8a/{ENGINE}", engine)
        z.writestr("lib/arm64-v8a/libSDL2.so", (FIXTURES / "libfakeengine_arm64.so").read_bytes())
    return p


def test_apply_loads_gl_through_the_interposer(tmp_path, quest_manifest):
    engine = (FIXTURES / "libfakemultiview_arm64.so").read_bytes()
    assert engine.count(b"\0libGLESv3.so\0") == 2  # DT_NEEDED (.dynstr) + the dlopen string (.rodata)
    apk = _apk(tmp_path, quest_manifest, engine)
    p = base.get("frame.gl_multiview_fbo")
    with ApkWorkspace(apk) as ws:
        ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), {})
        assert p.apply(ctx)
        assert any("1 libGLESv3.so load(s) redirected" in n for n in ctx.notes)
        assert p.validate(ctx) == [("Multiview interposer present", True, SHIM)]
        assert not p.apply(base.ApkContext(ws, _analysis(), {}, Reporter(), {}))  # applying twice changes nothing
        out = ws.write(tmp_path / "out.apk")
    with zipfile.ZipFile(out) as z:
        patched = z.read(f"lib/arm64-v8a/{ENGINE}")
        shim = z.read(f"lib/arm64-v8a/{SHIM}")
        sdl = z.read("lib/arm64-v8a/libSDL2.so")
    assert elf.needed(patched)[0] == SHIM
    assert elf.needed(patched)[1:] == elf.needed(engine)  # libGLESv3.so stays a dependency
    assert elf.replace_rodata_string(patched, GLES, GLES)[1] == 0  # the dlopen string now names the interposer
    assert elf.replace_rodata_string(patched, SHIM, SHIM)[1] == 1
    assert elf.replace_rodata_string(patched, "libGLESv2.so", "libGLESv2.so")[1] == 1  # fallback untouched
    assert shim == artifact("arm64-v8a", SHIM)
    assert sdl == (FIXTURES / "libfakeengine_arm64.so").read_bytes()  # other libraries untouched


def test_apply_without_analysis_field_scans(tmp_path, quest_manifest):
    """An entry analysed before the field existed: the libraries are scanned at build time."""
    apk = _apk(tmp_path, quest_manifest, (FIXTURES / "libfakemultiview_arm64.so").read_bytes())
    with ApkWorkspace(apk) as ws:
        assert base.get("frame.gl_multiview_fbo").apply(base.ApkContext(ws, _analysis(extra={}), {}, Reporter(), {}))
        assert elf.needed(ws.read_lib(ENGINE))[0] == SHIM


def test_triage_reports_a_failed_twin():
    log = ("10-09 10:00:00.000  1150  1170 I GLMV    : twin of program 12: stage 0x8b31 failed to compile: 0:4(1): "
           "error: syntax error\n")
    r = triage(log, "RUNNING", "com.drbeef.doom3quest")
    assert "gl-multiview-twin-failed" in {f.id for f in r.findings}
    ok = "10-09 10:00:00.000  1150  1170 I GLMV    : twin built: program 12 -> 40 (9 uniforms, 2 blocks, 1 num_views\n"
    assert "gl-multiview-twin-failed" not in {f.id for f in triage(ok, "RUNNING", "com.drbeef.doom3quest").findings}
