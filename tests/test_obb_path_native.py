"""native/obbpath (frame.obb_mount_emul, GitHub #308): the mounted-OBB path string is handed to the caller in a block
from the caller's own operator new. Built for the host with stand-ins for both libraries: a "libandroid_utils" whose
strings come from a libc++-like operator new, and a "game" library with its own operator new that frees the result
with its own operator delete (like libgrid.so).

Opt-in (FRAMEPORT_NATIVE_TESTS=1, -m native): a host C compiler only, no NDK, no device."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CC = shutil.which("cc") or shutil.which("clang")
pytestmark = [pytest.mark.native, pytest.mark.skipif(
    not (os.environ.get("FRAMEPORT_NATIVE_TESTS") and CC),
    reason="set FRAMEPORT_NATIVE_TESTS=1 (needs a host C compiler)")]

LOG_H = """#include <stdio.h>
#define ANDROID_LOG_INFO 4
#define __android_log_print(prio, tag, ...) (fprintf(stderr, __VA_ARGS__), fputc('\\n', stderr))
"""

# libc++ stand-in: operator new/delete that count (the callee's dependency, like libc++_shared.so)
CXX = r"""
#include <stdlib.h>
int cxx_news, cxx_deletes;
void *_Znwm(unsigned long n) { cxx_news++; return malloc(n); }
void _ZdlPv(void *p) { if (p) cxx_deletes++; free(p); }
"""

# libandroid_utils stand-in: returns a std::string (libc++ layout) by value; long ones on its operator new's heap
CALLEE = r"""
#include <stdint.h>
#include <string.h>
struct ndk_string { uint64_t cap; uint64_t size; char *data; };
extern void *_Znwm(unsigned long);
struct ndk_string _ZN12AndroidUtils21getMountedOBBFullPathEPKc(const char *name) {
    struct ndk_string s = {0, 0, 0};
    size_t n = strlen(name);
    if (n < 23) {  /* short form: size << 1 in byte 0, characters in place */
        unsigned char *b = (unsigned char *)&s;
        b[0] = (unsigned char)(n << 1);
        memcpy(b + 1, name, n + 1);
        return s;
    }
    size_t cap = (n + 16) & ~(size_t)15;
    s.data = _Znwm(cap);
    memcpy(s.data, name, n + 1);
    s.cap = cap | 1;
    s.size = n;
    return s;
}
"""

# the game library: its own operator new puts a header in front of the block; its delete checks it (libgrid's
# inlined delete reads such a header and crashed on a foreign block). game_mount_point(name, out, via_helper): the
# import as FramePort leaves it (renamed to the helper's) or the original one (what the game did before)
GAME = r"""
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define MAGIC 0x4e454f4e4e454f4eull
struct ndk_string { uint64_t cap; uint64_t size; char *data; };
struct ndk_string fp_obb_mounted_path(const char *);
struct ndk_string _ZN12AndroidUtils21getMountedOBBFullPathEPKc(const char *);
int game_news, foreign;
void *_Znwm(unsigned long n) { uint64_t *p = malloc(n + 16); p[0] = MAGIC; game_news++; return p + 2; }
static void game_delete(void *p) { uint64_t *h = (uint64_t *)p - 2; if (h[0] != MAGIC) { foreign++; return; } free(h); }
const char *game_mount_point(const char *name, char *out, int via_helper) {
    struct ndk_string s = via_helper ? fp_obb_mounted_path(name) : _ZN12AndroidUtils21getMountedOBBFullPathEPKc(name);
    const char *text = (s.cap & 1) ? s.data : (const char *)&s + 1;
    strcpy(out, text);
    if (s.cap & 1) game_delete(s.data);
    return out;
}
"""

MAIN = r"""
#include <stdio.h>
#include <string.h>
extern int cxx_news, cxx_deletes, game_news, foreign;
const char *game_mount_point(const char *, char *, int);
int main(void) {
    char out[256];
    const char *names[] = {"/mnt/obb/obb:0",
                           "/storage/emulated/0/Android/obb/com.codemasters.gridlegends/DataSet16.obb"};
    for (int via = 0; via < 2; via++) {
        for (int i = 0; i < 2; i++) {
            game_mount_point(names[i], out, via);
            printf("%s|%d\n", strcmp(out, names[i]) == 0 ? "same" : out, i);
        }
        printf("cxx_news=%d cxx_deletes=%d game_news=%d foreign=%d\n", cxx_news, cxx_deletes, game_news, foreign);
    }
    return 0;
}
"""


def _build(tmp: Path) -> Path:
    (tmp / "android").mkdir()
    (tmp / "android/log.h").write_text(LOG_H)
    for name, text in (("cxx", CXX), ("callee", CALLEE), ("game", GAME), ("main", MAIN)):
        (tmp / f"{name}.c").write_text(text)
    lib = ["-shared", "-fPIC", "-O1", "-Wall", "-Werror"]
    run = lambda *a: subprocess.run([CC, *a], check=True, cwd=tmp)  # noqa: E731
    run(*lib, "cxx.c", "-o", "libcxx.so")
    run(*lib, "callee.c", "-L.", "-lcxx", "-o", "libcallee.so")
    run(*lib, "-D_GNU_SOURCE", "-I", str(tmp), "-fvisibility=hidden", str(ROOT / "native/obbpath/obbpath.c"),
        "-ldl", "-lpthread", "-o", "libfp_obbpath.so")
    run(*lib, "game.c", "-L.", "-lfp_obbpath", "-lcallee", "-o", "libgame.so")
    # libcxx first: the callee's operator new is libc++'s, as libandroid_utils.so's (loaded before the game) is
    run("main.c", "-L.", "-Wl,--no-as-needed", "-lcxx", "-lcallee", "-lgame", "-Wl,-rpath-link,.", "-o", "main")
    return tmp / "main"


def test_long_path_comes_from_the_callers_allocator(tmp_path):
    p = subprocess.run([str(_build(tmp_path))], capture_output=True, text=True, check=True,
                       env={**os.environ, "LD_LIBRARY_PATH": str(tmp_path)})
    lines = p.stdout.splitlines()
    # the original import: the short path is fine, the long one reaches the game's delete from libc++'s heap
    assert lines[:3] == ["same|0", "same|1", "cxx_news=1 cxx_deletes=0 game_news=0 foreign=1"]
    # through the helper: the long path arrives in a block of the game's own operator new; nothing foreign is freed
    # (the callee's block is left alone, never freed by the wrong allocator)
    assert lines[3:] == ["same|0", "same|1", "cxx_news=2 cxx_deletes=0 game_news=1 foreign=1"]
    assert "handed to" in p.stderr and "libgame.so" in p.stderr
