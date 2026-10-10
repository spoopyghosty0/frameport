// SPDX-License-Identifier: GPL-3.0-only
// Shader dump for the Vulkan shim (adapter setting vk_shader_dump=1, diagnostics): every distinct SPIR-V module the
// game creates is written once to <files>/fp_vk_shaders/<size>_<sha256>.spv (written to a temporary name, then
// renamed: a game killed mid-write leaves no partial module), and every vkCreateShaderModule call gets one line in
// <files>/fp_vk_shaders/index.txt:
//   <seq> <ms since the process's first module> <unix ms> <size>_<sha256>.spv new|known|again|failed
// new = first written now, known = already on disk from an earlier run, again = created before in this run,
// failed = couldn't be written. Each
// process start adds "# start <unix s> pid <pid>". With the kernel's "hangcheck detected gpu lockup" time (journal),
// the modules created last before a GPU hang are the candidates for a vk_shader_fix. A module is hashed on every call
// but written only on first sight; index lines are single O_APPEND writes (they survive the game being killed).
// Needs LOG(...) and sha256() before inclusion (vkshim.c; tests/test_vk_shader_dump.py builds it for the host).
#pragma once
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define DUMP_SET 16384           // first sights remembered per process (a power of two)
#define DUMP_MAX_INDEX 200000    // index lines per process
static int dump_on;              // vk_shader_dump
static char dump_dir[512];
static int dump_index_fd = -1;
static pthread_mutex_t dump_lock = PTHREAD_MUTEX_INITIALIZER;
static uint64_t dump_seen[DUMP_SET];  // first 8 digest bytes; 0 = empty
static unsigned dump_nseen, dump_seq, dump_written;
static int64_t dump_t0;

static int64_t dump_ms(clockid_t clock) {
    struct timespec ts;
    clock_gettime(clock, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

// Turns the dump on for modules created from now on; `files` = the app's files dir. Returns 1 when it can write.
static int dump_init(const char *files) {
    if (!files || !*files || strlen(files) > sizeof dump_dir - 64) return 0;
    snprintf(dump_dir, sizeof dump_dir, "%s/fp_vk_shaders", files);
    if (mkdir(dump_dir, 0775) != 0 && errno != EEXIST) {
        LOG("vk shim: shader dump: can't create %s (%s)", dump_dir, strerror(errno));
        return 0;
    }
    char path[600];
    snprintf(path, sizeof path, "%s/index.txt", dump_dir);
    dump_index_fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0664);
    if (dump_index_fd < 0) {
        LOG("vk shim: shader dump: can't open %s (%s)", path, strerror(errno));
        return 0;
    }
    char line[96];
    int n = snprintf(line, sizeof line, "# start %lld pid %d\n", (long long)(dump_ms(CLOCK_REALTIME) / 1000),
                     (int)getpid());
    if (write(dump_index_fd, line, (size_t)n) < 0) { /* the dump itself may still work */ }
    dump_t0 = dump_ms(CLOCK_MONOTONIC);
    dump_on = 1;
    LOG("vk shim: dumping SPIR-V modules to %s (index.txt: creation order and time)", dump_dir);
    return 1;
}

// 1 = first sight in this process (remembered now), 0 = seen before (or the set is full: the file check decides)
static int dump_first_sight(uint64_t key) {
    if (!key) key = 1;
    for (unsigned i = (unsigned)(key * 0x9E3779B97F4A7C15ull >> 50) & (DUMP_SET - 1), n = 0; n < DUMP_SET;
         i = (i + 1) & (DUMP_SET - 1), n++) {
        if (dump_seen[i] == key) return 0;
        if (!dump_seen[i]) {
            if (dump_nseen >= DUMP_SET * 3 / 4) return 1;
            dump_seen[i] = key;
            dump_nseen++;
            return 1;
        }
    }
    return 1;
}

static void dump_module(const uint32_t *code, size_t size) {
    if (!dump_on || !code || !size) return;
    uint8_t digest[32];
    sha256((const uint8_t *)code, size, digest);
    uint64_t key = 0;
    memcpy(&key, digest, 8);
    pthread_mutex_lock(&dump_lock);
    unsigned seq = ++dump_seq;
    int first = dump_first_sight(key);
    pthread_mutex_unlock(&dump_lock);
    char name[96], path[700];
    int len = snprintf(name, sizeof name, "%zu_", size);
    for (int i = 0; i < 32; i++) len += snprintf(name + len, sizeof name - (size_t)len, "%02x", digest[i]);
    snprintf(name + len, sizeof name - (size_t)len, ".spv");
    snprintf(path, sizeof path, "%s/%s", dump_dir, name);
    const char *what = "again";
    if (first) {
        what = "known";
        if (access(path, F_OK) != 0) {
            char tmp[720];
            snprintf(tmp, sizeof tmp, "%s.%d.tmp", path, (int)gettid());
            int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0664);
            ssize_t w = fd >= 0 ? write(fd, code, size) : -1;
            if (fd >= 0) close(fd);
            if (w == (ssize_t)size && rename(tmp, path) == 0) {
                what = "new";
                pthread_mutex_lock(&dump_lock);
                unsigned written = ++dump_written;
                pthread_mutex_unlock(&dump_lock);
                if (written == 1 || written % 500 == 0) LOG("vk shim: shader dump: %u module(s) written", written);
            } else {
                unlink(tmp);
                what = "failed";
            }
        }
    }
    if (dump_index_fd >= 0 && seq <= DUMP_MAX_INDEX) {
        char line[200];
        int n = snprintf(line, sizeof line, "%u %lld %lld %s %s\n", seq, (long long)(dump_ms(CLOCK_MONOTONIC) - dump_t0),
                         (long long)dump_ms(CLOCK_REALTIME), name, what);
        if (write(dump_index_fd, line, (size_t)n) < 0) { /* best effort */ }
        if (seq == DUMP_MAX_INDEX) LOG("vk shim: shader dump: index full (%d lines)", DUMP_MAX_INDEX);
    }
}
