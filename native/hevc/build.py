#!/usr/bin/env python3
"""Build the Android media-service plugin on Linux with NDK r27c and a Lepton rootfs.

The runtime's private SoftOMX ABI is deliberately pinned. This plugin is mounted
only when the tested library fingerprint matches; it never replaces system files.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE.parents[1] / "artifacts/hevc"
FFMPEG_SHA = "733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"
RUNTIME_SHA = "456e912c75cd389abcf6a63bc80e2a53bdc334371d00b200c93680388ae955e2"
NDK_REVISION = "27.2.12479018"
FLUSH_SOURCE = r"""static void v4l2_flush(AVCodecContext *avctx)
{
    V4L2m2mPriv *priv = avctx->priv_data;
    V4L2m2mContext *s = priv->context;
    struct v4l2_decoder_cmd cmd = { .cmd = V4L2_DEC_CMD_START };

    av_packet_unref(&s->buf_pkt);
    priv->frameport_flush_error = 0;
    /* Only a drained decoder restarts in place: it has returned every picture
     * and is stopped until V4L2_DEC_CMD_START (both queues keep streaming).
     * Restarting a running decoder's queues failed Iris session admission and
     * left its buffers unreturned (kernel warnings): callers reopen instead. */
    if (!s->draining || !s->capture.done) {
        priv->frameport_flush_error = AVERROR(EINVAL);
        av_log(avctx, AV_LOG_WARNING, "flush before the drain completed; reopen the decoder\n");
        return;
    }
    if (ioctl(s->fd, VIDIOC_DECODER_CMD, &cmd) < 0) {
        priv->frameport_flush_error = AVERROR(errno);
        av_log(avctx, AV_LOG_ERROR, "V4L2_DEC_CMD_START after drain: %s\n", av_err2str(priv->frameport_flush_error));
        return;
    }
    s->draining = 0;
    s->output.done = s->capture.done = 0;
}
"""


def run(args, cwd=None, env=None):
    # PWD keeps the shell's (and FFmpeg configure's) idea of the directory on the space-free link path
    env = dict(env or os.environ, **({"PWD": str(cwd)} if cwd else {}))
    subprocess.run([str(a) for a in args], check=True, cwd=cwd, env=env)


def space_free(links: Path, name: str, target: Path) -> Path:
    """FFmpeg's configure splits --extra-cflags on whitespace, so a repo/NDK/rootfs path with spaces breaks the
    build. Build through unresolved symlinks in a space-free temp dir; the prefix maps name these link paths, so the
    output is the same bytes wherever the real folders are."""
    link = links / name
    link.symlink_to(target, target_is_directory=True)
    return link


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ndk", type=Path, required=True)
    parser.add_argument("--lepton-root", type=Path, required=True)
    args = parser.parse_args()
    ndk_root = args.ndk.resolve()
    if not any(line.strip() == f"Pkg.Revision = {NDK_REVISION}"
               for line in (ndk_root / "source.properties").read_text().splitlines()):
        parser.error(f"use Android NDK r27c ({NDK_REVISION})")
    ndk = ndk_root / "toolchains/llvm/prebuilt/linux-x86_64"
    if not (ndk / "bin/clang++").exists():
        parser.error("use the Linux NDK r27c (Windows: run this builder inside WSL)")
    runtime = args.lepton_root.resolve()
    with tempfile.TemporaryDirectory(prefix="fp-hevc-") as tmp:
        links = Path(tmp)
        if any(c.isspace() for c in str(links)):
            parser.error(f"temp dir {links} has spaces: set TMPDIR to a folder without spaces")
        build(parser, space_free(links, "repo", HERE.parents[1]), space_free(links, "ndk", ndk_root),
              space_free(links, "rootfs", runtime))


def build(parser, repo: Path, ndk_root: Path, runtime: Path):
    here = repo / HERE.relative_to(HERE.parents[1])
    artifacts = repo / ARTIFACTS.relative_to(HERE.parents[1])
    ndk = ndk_root / "toolchains/llvm/prebuilt/linux-x86_64"
    if hashlib.sha256((runtime / "vendor/lib64/libstagefright_softomx.so").read_bytes()).hexdigest() != RUNTIME_SHA:
        parser.error("unverified SoftOMX ABI; validate and update the fingerprint before rebuilding")
    cache = here.parent / ".cache/hevc"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / "ffmpeg-7.1.1.tar.xz"
    if not archive.exists():
        urllib.request.urlretrieve("https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz", archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != FFMPEG_SHA:
        raise RuntimeError("FFmpeg source checksum mismatch")
    source = cache / "ffmpeg-7.1.1"
    install = cache / "ffmpeg-install"
    # Discard previous objects/configuration so a host compiler or changed
    # configure flags cannot silently survive a rebuild (a distclean keyed on
    # config.mak never ran: FFmpeg 7 keeps it in ffbuild/), and start from
    # unpatched sources (the edits below are applied once).
    for old in (source, install):
        if old.exists():
            shutil.rmtree(old)
    with tarfile.open(archive) as tar:
        tar.extractall(cache, filter="data")
    # Unlike FFmpeg's software VP9 decoder, its V4L2 wrapper does not split
    # packed VP9 superframes. Iris requires the same individual-frame input.
    decoder_source = source / "libavcodec/v4l2_m2m_dec.c"
    decoder_text = decoder_source.read_text()
    original_vp9 = 'M2MDEC(vp9,   "VP9",   AV_CODEC_ID_VP9,        NULL);'
    if decoder_text.count(original_vp9) != 1:
        raise RuntimeError("unexpected FFmpeg VP9 wrapper source")
    decoder_text = decoder_text.replace(original_vp9, 'M2MDEC(vp9, "VP9", AV_CODEC_ID_VP9, "vp9_superframe_split");')
    # The wrapper has no flush callback, so restarting after EOS (replay, loop)
    # could only close and reopen the hardware session. Add the stateful
    # decoder's restart after a completed drain (V4L2_DEC_CMD_START).
    close_fn = "static av_cold int v4l2_decode_close(AVCodecContext *avctx)\n"
    close_cb = "        .close          = v4l2_decode_close, \\\n"
    if decoder_text.count(close_fn) != 1 or decoder_text.count(close_cb) != 1:
        raise RuntimeError("unexpected FFmpeg V4L2 decoder source")
    decoder_text = decoder_text.replace(close_fn, FLUSH_SOURCE + "\n" + close_fn).replace(
        close_cb, close_cb + "        .flush          = v4l2_flush, \\\n")
    decoder_source.write_text(decoder_text)
    # avcodec_flush_buffers has no return value. Expose restart status through
    # an AVOption rather than having the OMX component inspect private structs.
    private_header = source / "libavcodec/v4l2_m2m.h"
    private_text = private_header.read_text()
    field = "    int num_capture_buffers;\n} V4L2m2mPriv;"
    option_end = "    { NULL},\n};"
    if private_text.count(field) != 1 or decoder_text.count(option_end) != 1:
        raise RuntimeError("unexpected FFmpeg V4L2 private options source")
    private_header.write_text(private_text.replace(field, "    int num_capture_buffers;\n"
                                                  "    int frameport_flush_error;\n} V4L2m2mPriv;"))
    decoder_source.write_text(decoder_text.replace(option_end,
        '    { "frameport_flush_error", "Last drained-session restart error",\n'
        '        OFFSET(frameport_flush_error), AV_OPT_TYPE_INT, {.i64 = 0}, INT_MIN, 0,\n'
        '        FLAGS | AV_OPT_FLAG_READONLY },\n' + option_end))
    buffers_source = source / "libavcodec/v4l2_buffers.c"
    buffers_source.write_text(buffers_source.read_text() + (here / "v4l2_export.c.inc").read_text())
    env = dict(os.environ, PATH=str(ndk / "bin") + os.pathsep + os.environ["PATH"])
    prefix_maps = [f"-ffile-prefix-map={repo}=.", f"-ffile-prefix-map={ndk_root}=android-ndk-r27c",
                   f"-ffile-prefix-map={runtime}=lepton-rootfs",
                   # clang otherwise resolves its own path (through the link) and embeds its real include dir
                   "-no-canonical-prefixes"]
    run([
        source / "configure", f"--prefix={install}", "--target-os=android", "--arch=aarch64",
        "--enable-cross-compile", "--cc=aarch64-linux-android30-clang", "--cxx=aarch64-linux-android30-clang++",
        "--ld=aarch64-linux-android30-clang", "--ar=llvm-ar", "--nm=llvm-nm", "--ranlib=llvm-ranlib",
        "--extra-cflags=" + " ".join(prefix_maps),
        "--extra-cxxflags=" + " ".join(prefix_maps),
        "--disable-everything", "--disable-autodetect", "--enable-v4l2-m2m", "--disable-programs",
        "--disable-doc", "--enable-pic", "--enable-static", "--disable-shared",
        "--enable-decoder=hevc_v4l2m2m,h264_v4l2m2m,vp9_v4l2m2m,hevc,h264,vp9",
        "--enable-parser=hevc,h264,vp9", "--enable-bsf=hevc_mp4toannexb,h264_mp4toannexb,vp9_superframe_split",
        "--enable-demuxer=mov,matroska", "--enable-protocol=file", "--enable-avcodec", "--enable-avformat",
        "--enable-avutil", "--disable-avdevice", "--disable-avfilter", "--disable-swscale",
        "--disable-swresample", "--disable-postproc",
    ], cwd=source, env=env)
    # FFmpeg embeds its configure command as a runtime diagnostic string.
    # Prefix-map flags cannot rewrite string literals, so normalize that
    # generated string too, retaining the options without host paths.
    config = source / "config.h"
    text = config.read_text()
    for path, replacement in ((ndk_root, "android-ndk-r27c"), (runtime, "lepton-rootfs"), (repo, ".")):
        text = text.replace(str(path), replacement)
    config.write_text(text)
    run(["make", f"-j{os.cpu_count() or 4}"], cwd=source, env=env)
    run(["make", "install"], cwd=source, env=env)
    # Use the platform libc++ namespace. Do not change the NDK's own headers or
    # distribute another libc++, which would create a conflicting private ABI.
    cpp = cache / "cpp"
    shutil.copytree(ndk / "sysroot/usr/include/c++/v1", cpp, dirs_exist_ok=True)
    site = cpp / "__config_site"
    site.write_text(site.read_text().replace("_LIBCPP_ABI_NAMESPACE __ndk1", "_LIBCPP_ABI_NAMESPACE __1"))
    artifacts.mkdir(parents=True, exist_ok=True)
    run([
        ndk / "bin/aarch64-linux-android30-clang++", "-std=gnu++17", "-O3", "-fPIC", "-shared",
        *prefix_maps,
        "-fno-rtti", "-fno-exceptions", "-nostdlib++", "-nostdinc++", "-Wall", "-Wextra", "-Werror",
        "-D_LIBCPP_VERBOSE_ABORT(...)=__builtin_abort()",
        "-Wno-unused-private-field", "-isystem", cpp, "-I", here / "platform",
        "-I", here / "platform/media/openmax", "-I", install / "include", here / "frameport_hevc.cpp",
        "-L", install / "lib", "-lavcodec", "-lavutil", "-L", runtime / "vendor/lib64",
        "-L", runtime / "system/lib64", "-lstagefright_softomx", "-lstagefright_foundation", "-lutils",
        "-llog", "-lnativewindow", "-lyuv", "-lvulkan", "-l:libc++.so", "-lm", "-ldl", "-Wl,--no-undefined",
        "-Wl,-z,max-page-size=16384",
        "-Wl,-soname,libstagefrighthw.so", "-o", artifacts / "libstagefrighthw.so",
    ], env=env)
    for name in ("podman.py", "media_codecs_frameport.xml"):
        shutil.copyfile(here / name, artifacts / (name + ".txt" if name == "podman.py" else name))
    shutil.copyfile(source / "COPYING.LGPLv2.1", artifacts / "COPYING.FFmpeg")
    files = {}
    for name in ("libstagefrighthw.so", "podman.py", "media_codecs_frameport.xml", "COPYING.FFmpeg"):
        path = artifacts / (name + ".txt" if name == "podman.py" else name)
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # bump the revision with every change: a Frame keeps an installed codec of the same or a newer revision
    manifest = {"revision": 7, "runtime_sha256": RUNTIME_SHA, "files": files,
                "codecs": ["video/hevc", "video/avc", "video/x-vnd.on2.vp9"],
                "build": {"ndk_revision": NDK_REVISION, "ffmpeg_source_sha256": FFMPEG_SHA}}
    (artifacts / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
