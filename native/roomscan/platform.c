/* SPDX-License-Identifier: GPL-3.0-only */
#include "platform.h"

#ifdef FP_HOSTED
#define _DEFAULT_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

int io_open(const char *p, int w) { return open(p, w ? O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW : O_RDONLY, 0600); }
long io_read(int fd, void *p, usize n)
{
    long r;
    do r = read(fd, p, n); while (r < 0 && errno == EINTR);
    return r;
}
static long put(int fd, const void *p, usize n)
{
    long r;
    do r = write(fd, p, n); while (r < 0 && errno == EINTR);
    return r;
}
int io_close(int fd) { return close(fd); }
int io_rename(const char *a, const char *b) { return rename(a, b); }
void io_remove(const char *p) { unlink(p); }
int io_mkdir(const char *p) { return mkdir(p, 0700) == 0 || errno == EEXIST; }
void *io_alloc(usize n)
{
    void *p = mmap(0, n, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
    return p == MAP_FAILED ? 0 : p;
}
void io_free(void *p, usize n) { if (p) munmap(p, n); }
int io_random(void *p, usize n)
{
    usize done = 0;
    while (done < n) {
        long r = getrandom((u8 *)p + done, n - done, 0);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) return 0;
        done += (usize)r;
    }
    return 1;
}
i64 io_time(int mono)
{
    struct timespec t;
    if (clock_gettime(mono ? CLOCK_MONOTONIC : CLOCK_REALTIME, &t)) return 0;
    return (i64)t.tv_sec * 1000000000 + t.tv_nsec;
}
#else
/* asm-generic Linux syscall numbers: this target has no libc or UAPI dependency. */
static long call(long n, long a, long b, long c, long d, long e, long f)
{
    register long x8 __asm__("x8") = n;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    register long x4 __asm__("x4") = e;
    register long x5 __asm__("x5") = f;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2), "r"(x3), "r"(x4), "r"(x5) : "memory", "cc");
    return x0;
}
int io_open(const char *p, int w) { return (int)call(56, -100, (long)p, w ? 1|64|128|131072 : 0, 0600, 0, 0); }
long io_read(int fd, void *p, usize n)
{
    long r;
    do r = call(63, fd, (long)p, (long)n, 0, 0, 0); while (r == -4);
    return r;
}
static long put(int fd, const void *p, usize n)
{
    long r;
    do r = call(64, fd, (long)p, (long)n, 0, 0, 0); while (r == -4);
    return r;
}
int io_close(int fd) { return (int)call(57, fd, 0, 0, 0, 0, 0); }
int io_rename(const char *a, const char *b) { return (int)call(38, -100, (long)a, -100, (long)b, 0, 0); }
void io_remove(const char *p) { call(35, -100, (long)p, 0, 0, 0, 0); }
int io_mkdir(const char *p)
{
    long r = call(34, -100, (long)p, 0700, 0, 0, 0);
    return r == 0 || r == -17;
}
void *io_alloc(usize n)
{
    long p = call(222, 0, (long)n, 3, 0x22, -1, 0);
    return (unsigned long)p > (unsigned long)-4096 ? 0 : (void *)p;
}
void io_free(void *p, usize n) { if (p) call(215, (long)p, (long)n, 0, 0, 0, 0); }
int io_random(void *p, usize n)
{
    usize done = 0;
    while (done < n) {
        long r = call(278, (long)((u8 *)p + done), (long)(n - done), 0, 0, 0, 0);
        if (r == -4) continue;
        if (r <= 0) return 0;
        done += (usize)r;
    }
    return 1;
}
i64 io_time(int mono)
{
    struct { long sec, ns; } t = {0, 0};
    call(113, mono ? 1 : 0, (long)&t, 0, 0, 0, 0);
    return (i64)t.sec * 1000000000 + t.ns;
}
void *memcpy(void *d, const void *s, usize n)
{
    u8 *a = d;
    const u8 *b = s;
    while (n--) *a++ = *b++;
    return d;
}
void *memset(void *d, int c, usize n)
{
    u8 *a = d;
    while (n--) *a++ = (u8)c;
    return d;
}
extern int main(int argc, char **argv);
__attribute__((used, noreturn)) void start_c(long *sp)
{
    int code = main((int)sp[0], (char **)(sp + 1));
    for (;;) call(94, code, 0, 0, 0, 0, 0);
}
__asm__(".text\n.global _start\n.type _start,%function\n_start:\n"
        "mov x29,#0\nmov x30,#0\nmov x0,sp\nbl start_c\nbrk #0\n");
#endif

int io_write(int fd, const void *p, usize n)
{
    while (n) {
        long r = put(fd, p, n);
        if (r <= 0) return 0;
        p = (const u8 *)p + r;
        n -= (usize)r;
    }
    return 1;
}
