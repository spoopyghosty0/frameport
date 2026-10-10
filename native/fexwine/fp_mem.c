/* fp_mem.so: x86_64 LD_PRELOAD for Wine under FEX (give it to the guest with FEX_ENV=LD_PRELOAD=..., the FEX
 * compat tool strips LD_PRELOAD).
 *
 * FP_BIGCACHE_MB=<n>: keep the pages of one released anonymous region of >= n MB and hand them back when the same
 *   address + size is mapped again. Unreal's large-block allocator gives blocks above its cache limit straight back
 *   to the OS, and some games (Oculus First Contact: 514 MB every 1-8 s) fill such a block completely each time:
 *   under FEX every 4 KB page then faults and is zeroed by the kernel (~130k faults, ~110 ms on a game task the game
 *   thread waits for). The reused block is NOT zeroed (Windows guarantees zeroed commits), so this is only for games
 *   that overwrite the whole block; any other use of the address range drops the cache first (fresh zero pages).
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

static size_t cache_min, thp_min = (size_t)32 << 20;
static int debug, thp;
static int (*real_mprotect)(void *, size_t, int);
static int (*real_munmap)(void *, size_t);
static void *(*real_mmap)(void *, size_t, int, int, int, off_t);
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static struct { char *addr; size_t len; int valid, via_munmap; unsigned long hits, stores; } cache;

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
}

static void advise(void *addr, size_t len)
{
    uintptr_t s = ((uintptr_t)addr + 0x1fffff) & ~(uintptr_t)0x1fffff;
    uintptr_t e = ((uintptr_t)addr + len) & ~(uintptr_t)0x1fffff;
    if (e > s) madvise((void *)s, e - s, MADV_HUGEPAGE);
}

static int overlaps(const void *addr, size_t len)
{
    return cache.valid && (const char *)addr < cache.addr + cache.len && cache.addr < (const char *)addr + len;
}

/* lock held: give the cached pages up (the range ends as it would have without the cache) */
static void drop_cache(const char *why)
{
    if (!cache.valid) return;
    if (debug) fprintf(stderr, "fp_mem: drop %p+%zx (%s)\n", cache.addr, cache.len, why);
    if (cache.via_munmap) real_munmap(cache.addr, cache.len);
    else real_mmap(cache.addr, cache.len, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED | MAP_NORESERVE, -1, 0);
    cache.valid = 0;
}

/* lock held: keep [addr, len) instead of releasing it */
static int store(void *addr, size_t len, int via_munmap)
{
    if (cache.valid || real_mprotect(addr, len, PROT_NONE)) return 0;
    cache.addr = addr; cache.len = len; cache.valid = 1; cache.via_munmap = via_munmap; cache.stores++;
    if (debug) fprintf(stderr, "fp_mem: keep %p+%zx (%s, kept %lu, reused %lu)\n", addr, len,
                       via_munmap ? "munmap" : "release", cache.stores, cache.hits);
    return 1;
}

void *mmap(void *addr, size_t len, int prot, int flags, int fd, off_t off)
{
    resolve();
    if (cache_min && len >= cache_min / 4) {
        pthread_mutex_lock(&lock);
        if (cache.valid && (char *)addr == cache.addr && len == cache.len && (flags & MAP_ANONYMOUS) && fd == -1 &&
            (prot & PROT_WRITE) && !real_mprotect(addr, len, prot)) {
            cache.valid = 0; cache.hits++;
            if (debug) fprintf(stderr, "fp_mem: reuse %p+%zx\n", addr, len);
            pthread_mutex_unlock(&lock);
            return addr;
        }
        if (len >= cache_min && (flags & MAP_ANONYMOUS) && (flags & MAP_FIXED) && prot == PROT_NONE && fd == -1 &&
            !overlaps(addr, len) && store(addr, len, 0)) {
            pthread_mutex_unlock(&lock);
            return addr;
        }
        pthread_mutex_unlock(&lock);
    }
    if (cache.valid) {
        pthread_mutex_lock(&lock);
        if (overlaps(addr, len)) drop_cache("mmap overlap");
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
    if (cache_min && (len >= cache_min || cache.valid)) {
        pthread_mutex_lock(&lock);
        if (overlaps(addr, len)) drop_cache("munmap overlap");
        else if (len >= cache_min && store(addr, len, 1)) { pthread_mutex_unlock(&lock); return 0; }
        pthread_mutex_unlock(&lock);
    }
    return real_munmap(addr, len);
}

int mprotect(void *addr, size_t len, int prot)
{
    resolve();
    if (cache.valid) {
        pthread_mutex_lock(&lock);
        if (overlaps(addr, len)) drop_cache("mprotect overlap");
        pthread_mutex_unlock(&lock);
    }
    int r = real_mprotect(addr, len, prot);
    if (thp && !r && (prot & PROT_WRITE) && len >= thp_min) advise(addr, len);
    return r;
}
