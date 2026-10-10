// Host stand-in for Android's <android/log.h>: native libraries compiled for host tests log to stderr.
#pragma once
#include <stdarg.h>
#include <stdio.h>

#define ANDROID_LOG_INFO 4

__attribute__((format(printf, 3, 4))) static inline int __android_log_print(int prio, const char *tag,
                                                                             const char *fmt, ...) {
    (void)prio;
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "%s: ", tag);
    int n = vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
    return n;
}
