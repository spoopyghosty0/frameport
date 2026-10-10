"""Video adapter freshness and its native stereo projection regression."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_video_patches_outdate_only_the_recipes_that_gain_them():
    """Batman's cutscene setup moved from a package check in frame.adapter to its recipe (frame.hw_video_decode +
    adapter.surface_native): its build is outdated by the recipe change; other games' builds stay installed."""
    from frameport.patches.base import get, recipe_fingerprint
    from frameport.ui.components import install_state

    assert get("frame.adapter").revision == 1 and not get("frame.adapter").package_revisions
    before = {"patches": {"frame.adapter": {}}}
    after = {"patches": {"frame.adapter": {}, "frame.hw_video_decode": {}, "adapter.surface_native": {"value": 1}}}
    for package, recipe, expected in (("com.camouflaj.manta", after, "outdated"),
                                      ("com.example.other", before, "installed")):
        game = {"package": package, "recipe": recipe,
                "build": {"sha256": "installed", "recipe_fp": recipe_fingerprint(before, package)}}
        frame = {"installed": [{"package": package, "sha256": "installed"}]}
        assert install_state(game, frame) == expected


def test_projection_shader_validates(tmp_path):
    compiler, validator = shutil.which("glslangValidator"), shutil.which("spirv-val")
    if not compiler or not validator:
        pytest.skip("Shader regression requires glslangValidator and spirv-val")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "projection.spv"
    subprocess.run([compiler, "-V", "--target-env", "vulkan1.0",
                    str(root / "native/adapter/surface_projection.comp"), "-o", str(binary)], check=True)
    subprocess.run([validator, "--target-env", "vulkan1.0", str(binary)], check=True)
