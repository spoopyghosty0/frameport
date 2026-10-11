/* fp_mem.so: x86_64 LD_PRELOAD for Wine under FEX (give it to the guest with FEX_ENV=LD_PRELOAD=..., the FEX
 * compat tool strips LD_PRELOAD).
 *
 * FP_BIGCACHE_MB=<n>: keep the pages of released anonymous regions of >= n MB (up to FP_BIGCACHE_SLOTS regions,
 *   default 12, and FP_BIGCACHE_MAX_MB in total, default 6144) and hand them back when Wine maps memory at the
 *   same start address again. Unreal's large-block allocator gives blocks above its cache limit straight back to
 *   the OS, and some games (Oculus First Contact: 0.5-1.4 GB every few seconds while playing) fill such blocks
 *   completely each time: under FEX every 4 KB page then faults and is zeroed by the kernel (~130k faults, ~110 ms)
 *   and releasing them costs about as much again, on a game task the game thread waits for.
 *   Same start, other size: shorter = the rest is released; longer = only the extra part is mapped fresh.
 *   Reused memory is NOT zeroed (Windows guarantees zeroed commits), so this is only for games that overwrite
 *   the whole block; any other use of a kept range drops it first (fresh zero pages).
 *   Kept regions keep their protection (they are not touched while kept), so keeping and reusing is nearly free.
 * FP_THP=1: MADV_HUGEPAGE on large writable anonymous regions (only helps while 2 MB pages are free).
 * FP_MEM_DEBUG=1: log to stderr. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/types.h>

#define MAX_SLOTS 16
static size_t cache_min, cache_max = (size_t)6144 << 20, thp_min = (size_t)32 << 20;
static int debug, thp, slots = 12;
static int (*real_mprotect)(void *, size_t, int);
static int (*real_munmap)(void *, size_t);
static void *(*real_mmap)(void *, size_t, int, int, int, off_t);
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
struct entry { char *addr; size_t len; int prot, via_munmap, used; };
static struct entry cache[MAX_SLOTS];
static size_t kept_total;
static unsigned long hits, partial, stores, drops;

static void resolve(void)
{
    if (!real_mmap) real_mmap = dlsym(RTLD_NEXT, "mmap");
    if (!real_munmap) real_munmap = dlsym(RTLD_NEXT, "munmap");
    if (!real_mprotect) real_mprotect = dlsym(RTLD_NEXT, "mprotect");
}

__attribute__((constructor)) static void fp_mem_init(void)
{
    const char *e;
    resolve();
    debug = getenv("FP_MEM_DEBUG") != NULL;
    thp = getenv("FP_THP") != NULL;
    if ((e = getenv("FP_BIGCACHE_MB")) && atol(e) > 0) cache_min = (size_t)atol(e) << 20;
    if ((e = getenv("FP_BIGCACHE_MAX_MB")) && atol(e) > 0) cache_max = (size_t)atol(e) << 20;
    if ((e = getenv("FP_BIGCACHE_SLOTS")) && atoi(e) > 0) slots = atoi(e) > MAX_SLOTS ? MAX_SLOTS : atoi(e);
}

static void advise(void *addr, size_t len)
{
    uintptr_t s = ((uintptr_t)addr + 0x1fffff) & ~(uintptr_t)0x1fffff;
    uintptr_t e = ((uintptr_t)addr + len) & ~(uintptr_t)0x1fffff;
    if (e > s) madvise((void *)s, e - s, MADV_HUGEPAGE);
}

/* lock held: give part of a kept range up the way the program asked for it originally */
static void release_range(const struct entry *c, char *addr, size_t len)
{
    if (c->via_munmap) real_munmap(addr, len);
    else real_mmap(addr, len, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED | MAP_NORESERVE, -1, 0);
}

static void drop(struct entry *c, const char *why)
{
    if (debug) fprintf(stderr, "fp_mem: drop %p+%zx (%s)\n", c->addr, c->len, why);
    release_range(c, c->addr, c->len);
    kept_total -= c->len; c->used = 0; drops++;
}

/* lock held: drop every kept range overlapping [addr, len) */
static void drop_overlaps(const void *addr, size_t len, const char *why)
{
    for (int i = 0; i < slots; i++)
        if (cache[i].used && (const char *)addr < cache[i].addr + cache[i].len && cache[i].addr < (const char *)addr + len)
            drop(&cache[i], why);
}

static int any_kept(void)
{
    for (int i = 0; i < slots; i++) if (cache[i].used) return 1;
    return 0;
}

/* lock held: keep [addr, len) instead of releasing it (its pages and protection stay as they are) */
static int store(void *addr, size_t len, int via_munmap, int prot)
{
    int free_slot = -1;
    if (kept_total + len > cache_max) return 0;
    for (int i = 0; i < slots; i++) {
        if (!cache[i].used) { if (free_slot < 0) free_slot = i; continue; }
        if ((char *)addr < cache[i].addr + cache[i].len && cache[i].addr < (char *)addr + len) return 0;
    }
    if (free_slot < 0) return 0;
    cache[free_slot] = (struct entry){ addr, len, prot, via_munmap, 1 };
    kept_total += len; stores++;
    if (debug) fprintf(stderr, "fp_mem: keep %p+%zx (%s, kept %zu MB in total; stores %lu, hits %lu, partial %lu, drops %lu)\n",
                       addr, len, via_munmap ? "munmap" : "release", kept_total >> 20, stores, hits, partial, drops);
    return 1;
}

/* protection of a region before it is released: unknown here, so assume read/write (Wine's committed views) */
#define KEPT_PROT (PROT_READ | PROT_WRITE)

void *mmap(void *addr, size_t len, int prot, int flags, int fd, off_t off)
{
    resolve();
    if (cache_min && len >= cache_min / 4 && any_kept()) {
        pthread_mutex_lock(&lock);
        for (int i = 0; i < slots; i++) {
            struct entry *c = &cache[i];
            if (!c->used || (char *)addr != c->addr || fd != -1 || !(flags & MAP_ANONYMOUS) || !(prot & PROT_WRITE)) continue;
            if (len <= c->len) {
                if (prot != c->prot && real_mprotect(addr, len, prot)) break;
                if (len < c->len) { release_range(c, c->addr + len, c->len - len); partial++; }
                kept_total -= c->len; c->used = 0; hits++;
                if (debug) fprintf(stderr, "fp_mem: reuse %p+%zx of %zx\n", addr, len, c->len);
                pthread_mutex_unlock(&lock);
                return addr;
            }
            /* longer: map the extra part fresh behind the kept one */
            void *t = real_mmap(c->addr + c->len, len - c->len, prot, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
            if (t == (void *)(c->addr + c->len)) {
                if (prot != c->prot) real_mprotect(c->addr, c->len, prot);
                kept_total -= c->len; c->used = 0; hits++; partial++;
                if (debug) fprintf(stderr, "fp_mem: reuse %p+%zx of %zx (+ fresh tail)\n", addr, len, c->len);
                pthread_mutex_unlock(&lock);
                if (thp && len - c->len >= thp_min) advise(t, len - c->len);
                return addr;
            }
            if (t != MAP_FAILED) real_munmap(t, len - c->len);
            break;
        }
        pthread_mutex_unlock(&lock);
    }
    if (cache_min && len >= cache_min && (flags & MAP_ANONYMOUS) && (flags & MAP_FIXED) && prot == PROT_NONE && fd == -1) {
        /* Wine releasing (part of) a view inside its reserved areas */
        pthread_mutex_lock(&lock);
        int ok = store(addr, len, 0, KEPT_PROT);
        pthread_mutex_unlock(&lock);
        if (ok) return addr;
    }
    if (any_kept()) {
        pthread_mutex_lock(&lock);
        drop_overlaps(addr, len, "mmap overlap");
        pthread_mutex_unlock(&lock);
    }
    void *r = real_mmap(addr, len, prot, flags, fd, off);
    if (thp && r != MAP_FAILED && (flags & MAP_ANONYMOUS) && (prot & PROT_WRITE) && len >= thp_min) advise(r, len);
    return r;
}

void *mmap64(void *addr, size_t len, int prot, int flags, int fd, off_t off) __attribute__((alias("mmap")));

int munmap(void *addr, size_t len)
{
    resolve();
    if (cache_min && (len >= cache_min || any_kept())) {
        pthread_mutex_lock(&lock);
        drop_overlaps(addr, len, "munmap overlap");
        if (len >= cache_min && store(addr, len, 1, KEPT_PROT)) { pthread_mutex_unlock(&lock); return 0; }
        pthread_mutex_unlock(&lock);
    }
    return real_munmap(addr, len);
}

int mprotect(void *addr, size_t len, int prot)
{
    resolve();
    if (any_kept()) {
        pthread_mutex_lock(&lock);
        drop_overlaps(addr, len, "mprotect overlap");
        pthread_mutex_unlock(&lock);
    }
    int r = real_mprotect(addr, len, prot);
    if (thp && !r && (prot & PROT_WRITE) && len >= thp_min) advise(addr, len);
    return r;
}
