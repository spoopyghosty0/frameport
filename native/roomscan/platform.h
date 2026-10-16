/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef FP_ROOMSCAN_PLATFORM_H
#define FP_ROOMSCAN_PLATFORM_H

/* The reconstruction sources use only this boundary, on Linux and in hosted tests. */
typedef __SIZE_TYPE__ usize;
typedef unsigned char u8;
typedef unsigned int u32;
typedef unsigned long long u64;
typedef long long i64;
int io_open(const char *path, int write);
long io_read(int fd, void *data, usize size);
int io_write(int fd, const void *data, usize size);
int io_close(int fd);
int io_rename(const char *old, const char *new);
void io_remove(const char *path);
int io_mkdir(const char *path);
void *io_alloc(usize size);
void io_free(void *data, usize size);
int io_random(void *data, usize size);
i64 io_time(int monotonic);
void *memcpy(void *dst, const void *src, usize size);
void *memset(void *dst, int value, usize size);

#endif
