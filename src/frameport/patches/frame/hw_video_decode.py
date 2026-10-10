"""Hardware video decoding: H.264, HEVC and VP9 on the Frame's Iris decoder instead of Android's software decoders.

Lepton's Android 11 has only Google's software codecs (OMX.google.*), which can't keep up with 4K/8K video. FramePort's
OMX plugin (native/hevc: `OMX.frameport.{avc,hevc,vp9}.decoder`, FFmpeg's v4l2m2m wrappers on /dev/video-dec0, with an
FFmpeg software fallback inside the component) is installed once per Frame by the agent (`install_video_codec`, shared
versioned store, see frame/connection.ensure_video_codec). This patch only selects it per game: the agent gives the
game's launcher the codec line when its recipe has this patch (deployment.json `hw_video_decode`), which puts the
shared Podman wrapper first on Lepton's PATH; the wrapper mounts the plugin read-only into this game's container only.
The APK is not changed (older builds' bundled codec assets are removed by frame.adapter). FramePort's setting
"Hardware video decoding" (library setting video.hw_decode) or FRAMEPORT_NO_HW_VIDEO=1 turns it off for every game.
"""
from __future__ import annotations

from ..base import Patch, Suggestion, register


class HwVideoDecode(Patch):
    id = "frame.hw_video_decode"
    title = "Hardware video decoding (H.264, HEVC, VP9)"
    description = ("Decodes the game's videos on the Frame's hardware video decoder instead of Android's software "
                   "decoders, which stutter or fall behind with 4K/8K video (e.g. video players, Batman: Arkham "
                   "Shadow's cutscenes). Adds FramePort's codec plugin (OMX.frameport.avc/hevc/vp9.decoder) to this "
                   "game's container only; the APK isn't changed. When the hardware is busy (e.g. Steam's own hardware "
                   "video decoding is on) the video is decoded in software as before.")
    stage = "install"  # the agent reads it from the recipe at finalize: no APK change
    order = 48
    needs_vr = False  # 2D video apps benefit as much

    def applies(self, a):
        return "arm64-v8a" in a.abis  # the plugin runs in Lepton's arm64 media service

    def detect(self, a):
        if self.applies(a) and (a.extra or {}).get("media_codec"):
            return Suggestion(True, "The game plays video through Android's decoders (MediaCodec, ExoPlayer or "
                                    "VLC): decode it on the Frame's hardware.")
        return None


register(HwVideoDecode)
