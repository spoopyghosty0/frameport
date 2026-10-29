"""gl_gamma_space (GL shim): games that render in gamma colour space get their colours converted to sRGB a second
time on the Frame, which takes only sRGB eye images (PLAY'A VR: washed out, grey instead of black)."""
from frameport.patches import base
from frameport.validate.triage import triage

PKG = "com.swearl.playa"
REFUSED = ("10-28 19:43:41.257  1211  1239 I FrameBridge: xrCreateSwapchain 1728x1728 format=32856 samples=1 array=2 "
           "faces=1 usage=0x21 flags=0x0 mips=1 next=0 result=-26 handle=0x0\n"
           "10-28 19:43:41.271  1211  1239 I FrameBridge:   retry format=35907 result=0\n")
FIXED = ("10-28 20:43:56.646  1211  1239 I GLShim  : GL shim per-game fixes: gl_int_attribs=0 "
         "gl_highp_markers=(none) gl_gamma_space=1\n")


def test_setting_registered_for_gles_games():
    base.load_all()
    p = base.get("adapter.gl_gamma_space")
    assert p.key == "gl_gamma_space" and p.default == 0


def test_linear_eye_images_refused_suggests_gamma_space():
    r = triage(REFUSED, "RUNNING", PKG)
    found = [f for f in r.findings if f.id == "gles-gamma-space"]
    assert [f.suggest for f in found] == [["adapter.gl_gamma_space"]]
    assert not [f for f in r.findings if f.id == "swapchain-format"]  # the fallback worked: no format error


def test_no_suggestion_once_the_shim_applies_it():
    r = triage(REFUSED + FIXED, "RUNNING", PKG)
    assert not [f for f in r.findings if f.id == "gles-gamma-space"]
