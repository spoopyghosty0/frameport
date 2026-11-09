// Guest test library (armv7, C): libgcc's ARM EHABI unwinder (gnustl-era libraries such as Unity 2017's
// libil2cpp.so link their own copy) finds a pc's unwind table through __gnu_Unwind_Find_exidx, not
// dl_unwind_find_exidx (#439). Both must answer with the table of the library that holds the pc.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <link.h>
#include <stdint.h>

_Unwind_Ptr __gnu_Unwind_Find_exidx(_Unwind_Ptr pc, int *pcount);

__attribute__((noinline)) int exidx_here(int x)
{
    return x * 3 + 1;
}

/* bit 0: dl_unwind_find_exidx finds this library's table; bit 1: __gnu_Unwind_Find_exidx gives the same table;
 * bit 2: that table lies inside this library */
int exidx_test(void)
{
    int r = 0, n1 = 0, n2 = 0;
    _Unwind_Ptr pc = (_Unwind_Ptr)(uintptr_t)&exidx_here;
    _Unwind_Ptr t1 = dl_unwind_find_exidx(pc, &n1);
    _Unwind_Ptr t2 = __gnu_Unwind_Find_exidx(pc, &n2);
    if (t1 && n1 > 0) r |= 1;
    if (t2 == t1 && n2 == n1) r |= 2;
    Dl_info a, b;
    if (t2 && dladdr((void *)pc, &a) && dladdr((void *)t2, &b) && a.dli_fbase == b.dli_fbase) r |= 4;
    return r;
}
