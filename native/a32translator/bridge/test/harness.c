/* Bridge test harness: plays ART for the guest test libraries (libbtest*.a32.so) with the fake JVM.
 *
 *   harness <dir with liba32rt.so and the test libraries>
 *
 * Prints one line per check and "ALL OK" at the end (exit code 0), else the number of failed checks. */
#define _GNU_SOURCE
#include <android/native_activity.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include "a32bridge.h"
#include "fakejni.h"
#include "fakemedia.h"
#include <media/NdkMediaExtractor.h>

static int failures;

#define CHECK(cond, ...)                                                                                          \
    do {                                                                                                          \
        int ok_ = (cond);                                                                                         \
        printf("%s ", ok_ ? "ok  " : "FAIL");                                                                     \
        printf(__VA_ARGS__);                                                                                      \
        printf("\n");                                                                                             \
        fflush(stdout);                                                                                           \
        if (!ok_) failures++;                                                                                     \
    } while (0)

/* ---------------------------------------------------------------- the "Java" class */

static fclass *bridge_cls;
static jclass bridge_jcls; /* what ART passes static natives */

static jvalue j_add3(JNIEnv *env, jobject self, const jvalue *a)
{
    jvalue r;
    r.i = a[0].i + a[1].i + a[2].i;
    return r;
}

static jvalue j_mix(JNIEnv *env, jobject self, const jvalue *a)
{
    jvalue r;
    r.d = a[0].i + (double)a[1].j + a[2].f + a[3].d + (double)strlen(fake_utf((jstring)a[4].l));
    return r;
}

/* Java callBack(n) = nativeTwice(n) */
static jvalue j_callback(JNIEnv *env, jobject self, const jvalue *a)
{
    jvalue r;
    fmethod *m = fake_method(bridge_cls, "nativeTwice", "(I)I");
    r.i = ((jint (*)(JNIEnv *, jclass, jint))m->native_fn)(env, bridge_jcls, a[0].i);
    return r;
}

static jvalue j_ctor(JNIEnv *env, jobject self, const jvalue *a)
{
    (*env)->SetIntField(env, self, (*env)->GetFieldID(env, bridge_jcls, "value", "I"), a[0].i);
    jvalue r;
    r.j = 0;
    return r;
}

static jvalue j_greet(JNIEnv *env, jobject self, const jvalue *a)
{
    char buf[128];
    snprintf(buf, sizeof buf, "hello %s/%d", fake_utf((jstring)a[0].l), fake_get_int_field(self));
    jvalue r;
    r.l = fake_string(buf);
    return r;
}

#define NATIVE(name, sig) {name, sig, 1, 1, NULL, NULL, NULL}
static const fmethod bridge_methods[] = {
    {"add3", "(III)I", 1, 0, j_add3, NULL, NULL},
    {"mix", "(IJFDLjava/lang/String;)D", 1, 0, j_mix, NULL, NULL},
    {"callBack", "(I)I", 1, 0, j_callback, NULL, NULL},
    {"<init>", "(I)V", 0, 0, j_ctor, NULL, NULL},
    {"greet", "(Ljava/lang/String;)Ljava/lang/String;", 0, 0, j_greet, NULL, NULL},
    NATIVE("nativeAdd", "(II)I"),
    NATIVE("nativeMany", "(JDFZBCSIJDFI)J"),
    NATIVE("nativeFloat", "(FF)F"),
    NATIVE("nativeDouble", "(DD)D"),
    NATIVE("nativeByte", "(B)B"),
    NATIVE("nativeConcat", "(Ljava/lang/String;)Ljava/lang/String;"),
    NATIVE("nativeSum", "([I)I"),
    NATIVE("nativeCallJava", "()I"),
    NATIVE("nativeNested", "(I)I"),
    NATIVE("nativeTwice", "(I)I"),
    NATIVE("nativeThread", "()I"),
    NATIVE("nativeThrow", "()I"),
    NATIVE("exported", "(I)I"),
    NATIVE("over", "(I)J"),
    NATIVE("over", "(J)J"),
    NATIVE("under_score", "()I"),
    NATIVE("unregistered", "()V"),
    {NULL, NULL, 0, 0, NULL, NULL, NULL},
};

jvalue fake_invoke(JNIEnv *env, fmethod *m, jobject self, const jvalue *a)
{
    if (m->impl) return m->impl(env, self, a);
    fprintf(stderr, "harness: Java calls native %s%s, not supported by the fake\n", m->name, m->sig);
    abort();
}

static void *nat(const char *name, const char *sig)
{
    fmethod *m = fake_method(bridge_cls, name, sig);
    return m ? m->native_fn : NULL;
}

/* ---------------------------------------------------------------- tests */

static void *other_thread(void *arg)
{
    fake_attach(); /* an ART thread */
    JNIEnv *env = fake_env();
    jint (*add)(JNIEnv *, jclass, jint, jint) = nat("nativeAdd", "(II)I");
    intptr_t r = 0;
    for (int k = 0; k < 100; k++) r += add(env, bridge_jcls, k, 1);
    jint (*nested)(JNIEnv *, jclass, jint) = nat("nativeNested", "(I)I");
    r += nested(env, bridge_jcls, 1000); /* 2001 */
    return (void *)r;
}

static void test_jni(const char *dir)
{
    JavaVM *vm = fake_vm();
    fake_attach();
    JNIEnv *env = fake_env();
    bridge_cls = fake_class("com/example/Bridge", bridge_methods);
    bridge_jcls = (*env)->FindClass(env, "com/example/Bridge");
    char stub[512];
    snprintf(stub, sizeof stub, "%s/libbtest.so", dir);
    jint ver = a32b_jni_onload(vm, stub);
    CHECK(ver == JNI_VERSION_1_6, "JNI_OnLoad -> 0x%x", ver);

    jint (*add)(JNIEnv *, jclass, jint, jint) = nat("nativeAdd", "(II)I");
    CHECK(add && add(env, bridge_jcls, 40, 2) == 42, "RegisterNatives: nativeAdd(40, 2)");
    CHECK(add && add(env, bridge_jcls, -5, 3) == -2, "nativeAdd(-5, 3)");

    jlong (*many)(JNIEnv *, jclass, jlong, jdouble, jfloat, jboolean, jbyte, jchar, jshort, jint, jlong, jdouble,
                  jfloat, jint) = nat("nativeMany", "(JDFZBCSIJDFI)J");
    jlong exp = 10000000000LL + 1500 + 25 + 1 + (-3) + 60000 + (-7) + 123456 + (-9000000000LL) + 75 + 25 + 77;
    jlong got = many ? many(env, bridge_jcls, 10000000000LL, 1.5, 0.25f, 1, -3, 60000, -7, 123456, -9000000000LL, 7.5, 2.5f, 77)
                     : 0;
    CHECK(got == exp, "12 mixed arguments (stack on both sides): %lld, expected %lld", (long long)got,
          (long long)exp);

    jfloat (*mulf)(JNIEnv *, jclass, jfloat, jfloat) = nat("nativeFloat", "(FF)F");
    CHECK(mulf && mulf(env, bridge_jcls, 1.5f, -4.0f) == -6.0f, "float arguments and result");
    jdouble (*divd)(JNIEnv *, jclass, jdouble, jdouble) = nat("nativeDouble", "(DD)D");
    CHECK(divd && divd(env, bridge_jcls, 1.0, 8.0) == 0.125, "double arguments and result");
    jbyte (*byte1)(JNIEnv *, jclass, jbyte) = nat("nativeByte", "(B)B");
    CHECK(byte1 && byte1(env, bridge_jcls, -128) == 127 && byte1(env, bridge_jcls, 5) == 4, "byte argument and result");

    jstring (*concat)(JNIEnv *, jclass, jstring) = nat("nativeConcat", "(Ljava/lang/String;)Ljava/lang/String;");
    jstring s = concat ? concat(env, bridge_jcls, fake_string("frame")) : NULL;
    CHECK(s && !strcmp(fake_utf(s), "frame-5"), "strings both ways: %s", s ? fake_utf(s) : "(null)");

    jint (*sum)(JNIEnv *, jclass, jintArray) = nat("nativeSum", "([I)I");
    for (int low = 1; low >= 0; low--) {
        jintArray arr = fake_array('I', 5, low);
        jint *d = fake_array_data(arr);
        for (int k = 0; k < 5; k++) d[k] = k + 1;
        jint r = sum ? sum(env, bridge_jcls, arr) : -1;
        CHECK(r == 15 && d[0] == 99, "int[] elements %s 4 GiB (sum %d, written back %d)", low ? "below" : "above",
              r, d[0]);
    }

    jint (*calljava)(JNIEnv *, jclass) = nat("nativeCallJava", "()I");
    jint bits = calljava ? calljava(env, bridge_jcls) : 0;
    CHECK(bits == 127, "Call*Method: ... / va_list / jvalue[], NewObject, fields, arrays (0x%x of 0x7f)", bits);

    jint (*nested)(JNIEnv *, jclass, jint) = nat("nativeNested", "(I)I");
    CHECK(nested && nested(env, bridge_jcls, 21) == 43, "nested calls: native -> Java -> native");

    jint (*thr)(JNIEnv *, jclass) = nat("nativeThread", "()I");
    CHECK(thr && thr(env, bridge_jcls) == 123, "guest thread: AttachCurrentThread + call Java");

    pthread_t t[3];
    for (int k = 0; k < 3; k++) pthread_create(&t[k], NULL, other_thread, NULL);
    for (int k = 0; k < 3; k++) {
        void *r = NULL;
        pthread_join(t[k], &r);
        CHECK((intptr_t)r == 4950 + 100 + 2001, "host thread %d gets a guest thread (%ld)", k, (long)(intptr_t)r);
    }

    jint (*throw_)(JNIEnv *, jclass) = nat("nativeThrow", "()I");
    jint tr = throw_ ? throw_(env, bridge_jcls) : 0;
    const char *e = fake_take_exception();
    CHECK(tr == 7 && e && strstr(e, "boom"), "ThrowNew: %s", e ? e : "(no exception)");

    jint (*exported)(JNIEnv *, jclass, jint) = nat("exported", "(I)I");
    CHECK(exported && exported(env, bridge_jcls, 14) == 42, "Java_ export, short name");
    jlong (*over_i)(JNIEnv *, jclass, jint) = nat("over", "(I)J");
    jlong (*over_j)(JNIEnv *, jclass, jlong) = nat("over", "(J)J");
    CHECK(over_i && over_j && over_i(env, bridge_jcls, 1) == 2 && over_j(env, bridge_jcls, 1LL << 40) == (1LL << 40) + 2,
          "Java_ exports, long names (overloads)");
    jint (*under)(JNIEnv *, jclass) = nat("under_score", "()I");
    CHECK(under && under(env, bridge_jcls) == 5, "Java_ export with an escaped underscore");
}

/* ---------------------------------------------------------------- NativeActivity */

static int fake_window_marker;
#define FAKE_WINDOW ((ANativeWindow *)&fake_window_marker)

/* libandroid, as far as the imports need it */
void ANativeActivity_finish(ANativeActivity *a) {}
void ANativeActivity_setWindowFormat(ANativeActivity *a, int32_t f) {}
void ANativeActivity_setWindowFlags(ANativeActivity *a, uint32_t add, uint32_t rm) {}
void ANativeActivity_showSoftInput(ANativeActivity *a, uint32_t f) {}
void ANativeActivity_hideSoftInput(ANativeActivity *a, uint32_t f) {}
void ANativeWindow_acquire(ANativeWindow *w) {}
void ANativeWindow_release(ANativeWindow *w) {}
int32_t ANativeWindow_getWidth(ANativeWindow *w) { return w == FAKE_WINDOW ? 1234 : -1; }
int32_t ANativeWindow_getHeight(ANativeWindow *w) { return w == FAKE_WINDOW ? 567 : -1; }
int32_t ANativeWindow_getFormat(ANativeWindow *w) { return 1; }
int32_t ANativeWindow_setBuffersGeometry(ANativeWindow *w, int32_t x, int32_t y, int32_t f) { return 0; }

static int result2(uint32_t h, int which)
{
    uint32_t fn = a32b_sym(h, "btest2_result");
    uint32_t w = (uint32_t)which;
    return fn ? (int)a32b_call(fn, &w, 1) : -999;
}

static void test_native_activity(const char *dir)
{
    char stub[512];
    snprintf(stub, sizeof stub, "%s/libbtest2.so", dir);
    static ANativeActivityCallbacks cb;
    static ANativeActivity act;
    act.callbacks = &cb;
    act.vm = fake_vm();
    act.env = fake_env();
    act.clazz = fake_string("activity");
    act.internalDataPath = "/data/test/files";
    act.externalDataPath = "/sdcard/test";
    act.sdkVersion = 30;
    a32b_native_activity_oncreate(&act, "abc", 3, stub);
    uint32_t h = a32b_load(stub);
    CHECK(h != 0, "NativeActivity library loaded (with its DT_NEEDED libbtest.so)");
    cb.onStart(&act);
    cb.onResume(&act);
    cb.onWindowFocusChanged(&act, 1);
    cb.onNativeWindowCreated(&act, FAKE_WINDOW);
    size_t n = 0;
    char *state = cb.onSaveInstanceState(&act, &n);
    CHECK(result2(h, 0) == 1 && result2(h, 1) == 1, "onStart, onResume forwarded");
    CHECK(result2(h, 3) == 1, "onWindowFocusChanged(1)");
    CHECK(result2(h, 2) == 1234, "onNativeWindowCreated -> ANativeWindow_getWidth import (%d)", result2(h, 2));
    CHECK(n == 6 && state && !strcmp(state, "state"), "onSaveInstanceState result copied out");
    CHECK(result2(h, 4) == 1, "saved state passed in");
    CHECK(result2(h, 5) == 30, "sdkVersion");
    CHECK(result2(h, 6) == 1, "activity vm/env/paths");
    CHECK(result2(h, 7) == (1 ^ 0x55), "symbol from the DT_NEEDED library");
    cb.onDestroy(&act);
    CHECK(result2(h, 8) == 1, "onDestroy forwarded");
    free(state);
}

static void test_cxx(const char *dir, const char *name)
{
    char stub[512];
    snprintf(stub, sizeof stub, "%s/%s.so", dir, name);
    uint32_t h = a32b_load(stub);
    uint32_t fn = h ? a32b_sym(h, "cxx_test") : 0;
    uint32_t w = 6;
    int r = fn ? (int)a32b_call(fn, &w, 1) : -1;
    CHECK(r == 303, "%s: C++ exceptions and static constructors in a guest library (%d)", name, r);
}

static void test_exidx(const char *dir)
{
    char stub[512];
    snprintf(stub, sizeof stub, "%s/libbtest5.so", dir);
    uint32_t h = a32b_load(stub);
    uint32_t fn = h ? a32b_sym(h, "exidx_test") : 0;
    int r = fn ? (int)a32b_call(fn, NULL, 0) : -1;
    CHECK(r >= 0 && (r & 1), "dl_unwind_find_exidx finds a guest library's unwind table (0x%x)", r);
    CHECK(r >= 0 && (r & 6) == 6, "__gnu_Unwind_Find_exidx (libgcc's unwinder, #439) gives the same table, not the "
                                  "runtime's (0x%x)", r);
}

static void test_media(const char *dir)
{
    char stub[512];
    snprintf(stub, sizeof stub, "%s/libbtest4.so", dir);
    uint32_t h = a32b_load(stub);
    uint32_t fn = h ? a32b_sym(h, "media_test") : 0;
    int r = fn ? (int)a32b_call(fn, NULL, 0) : -1;
    CHECK(r >= 0 && (r & 1), "libmediandk: every function Unity's video player looks up resolves (0x%x)", r);
    CHECK(r >= 0 && (r & 64), "libmediandk data symbols: every AMEDIAFORMAT_KEY_* Unity looks up, with AOSP's value");
    CHECK(r >= 0 && (r & 2), "AMediaFormat: values both ways, host-owned string, buffer and toString copied in");
    CHECK(fake_media.buffers_high, "the fake codec's buffers lie above 4 GiB");
    CHECK(r >= 0 && (r & 4) && fake_media.configured && fake_media.queued_guest_data == 1,
          "AMediaCodec input buffer: the guest's writes reach the codec at queueInputBuffer");
    CHECK(r >= 0 && (r & 8), "AMediaCodec output buffer: buffer info and the codec's bytes reach the guest");
    CHECK(r >= 0 && (r & 16) && fake_media.queued_extractor_data == 1 && fake_media.last_timeout == -1,
          "AMediaExtractor_readSampleData into a mirrored input buffer, queued again (same buffer reused)");
    CHECK(fake_media.fd == 5 && fake_media.fd_offset == 1ll << 33 && fake_media.fd_length == 77 &&
              fake_media.seek_pos == 5000000000ll && fake_media.seek_mode == AMEDIAEXTRACTOR_SEEK_CLOSEST_SYNC,
          "64-bit arguments: setDataSourceFd, seekTo");
    CHECK(r >= 0 && (r & 32) && fake_media.formats_deleted == 2 && fake_media.codecs_deleted == 1,
          "getOutputFormat, AMediaFormat_delete, AMediaCodec_delete");
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: harness <dir>\n");
        return 2;
    }
    /* the host process owns memory in the low 4 GiB, as ART's heap and boot image do: the guest must avoid it */
    void *taken = mmap((void *)0x400000, 4 << 20, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    if (taken == MAP_FAILED) perror("mmap 0x400000");
    extern int a32_vrapi_selftest(void);
    CHECK(a32_vrapi_selftest() == 0, "VrApi ABI, render poses, tracking consistency and floor transforms");
    test_jni(argv[1]);
    test_native_activity(argv[1]);
    test_cxx(argv[1], "libbtest3");
    test_cxx(argv[1], "libbtest3_android");
    test_cxx(argv[1], "libbtest3_standard");
    test_exidx(argv[1]);
    test_media(argv[1]);
    CHECK(mprotect(taken, 4 << 20, PROT_NONE) == 0, "the host's low mapping is still there");
    if (failures) {
        printf("%d FAILED\n", failures);
        return 1;
    }
    printf("ALL OK\n");
    return 0;
}
