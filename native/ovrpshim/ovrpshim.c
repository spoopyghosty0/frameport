// SPDX-License-Identifier: GPL-3.0-only
// FramePort OVRPlugin shim: the frame loop for Unity 2017-2018 built-in Oculus support, and Unity's user presence.
//
// That Unity drives OVRPlugin with the legacy frame loop: ovrp_Update2(render step, frame index, ...) on the main
// thread, then ovrp_BeginFrame / ovrp_EndFrame on the render thread. It never calls ovrp_WaitToBeginFrame, which
// newer OVRPlugin builds (OVRPort's OpenXR OVRPlugin) need to call xrWaitFrame: no frame ever begins, OVRPlugin logs
// "CompositorOpenXR::Update called for frame N outside of frame bounds" thousands of times a second and the Frame's
// dashboard freezes (Accounting+, Unity 2017.4).
//
// FramePort renames the "ovrp_Update2" lookup in libunity.so to "fpov_Update2" (same length) and adds this library to
// libOVRPlugin.so's DT_NEEDED, so Unity's dlsym on the plugin finds this function: it waits for the frame first (once
// per frame index, render step only), then calls the real ovrp_Update2 (render step; the physics step: see below).
#include <android/log.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define TAG "FrameBridge"
#define EXPORT __attribute__((visibility("default")))
#define LOG(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)

typedef int (*PFN_Update2)(int step, int frame_index, double prediction_seconds);
typedef int (*PFN_WaitToBeginFrame)(int frame_index);

static PFN_Update2 real_update2;
static PFN_WaitToBeginFrame real_wait;
static pthread_once_t once = PTHREAD_ONCE_INIT;

// Per-game settings (the same sources as FrameBridge and the GL shim, later ones winning): the frame-begin gate and
// the held-back physics update below were needed by Sniper Elite VR; in Unity 2019 games (BattleSisters) holding the
// physics update back made the hands lag and the gate made loading screens stutter, so both are off unless the recipe
// turns them on (ovrp_begin_gate=1, ovrp_hold_physics=1).
static int begin_gate, hold_physics;

static void read_conf_file(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        if (!strncmp(line, "ovrp_begin_gate=", 16)) begin_gate = atoi(line + 16);
        if (!strncmp(line, "ovrp_hold_physics=", 18)) hold_physics = atoi(line + 18);
    }
    fclose(f);
}

static void read_conf(void) {
    Dl_info info;
    char path[600];
    if (dladdr((void *)read_conf, &info) && info.dli_fname) {
        const char *slash = strrchr(info.dli_fname, '/');
        if (slash && slash - info.dli_fname < 500) {
            snprintf(path, sizeof(path), "%.*s/libframe_settings.so", (int)(slash - info.dli_fname), info.dli_fname);
            read_conf_file(path);
        }
    }
    char pkg[256] = {0};
    FILE *f = fopen("/proc/self/cmdline", "r");
    if (f) {
        size_t n = fread(pkg, 1, sizeof(pkg) - 1, f);
        fclose(f);
        pkg[n] = 0;
    }
    char *colon = strchr(pkg, ':');
    if (colon) *colon = 0;
    if (*pkg && !strchr(pkg, '/')) {
        snprintf(path, sizeof(path), "/sdcard/Android/data/%s/files/framebridge.conf", pkg);
        read_conf_file(path);
    }
    const char *env = getenv("FRAMEBRIDGE_CONFIG");
    if (env && *env) read_conf_file(env);
}

static void init(void) {
    read_conf();
    void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
    if (!ovrp) ovrp = dlopen("libOVRPlugin.so", RTLD_NOW);
    if (ovrp) {
        real_update2 = (PFN_Update2)dlsym(ovrp, "ovrp_Update2");
        real_wait = (PFN_WaitToBeginFrame)dlsym(ovrp, "ovrp_WaitToBeginFrame");
    }
    LOG("ovrp frame loop shim: ovrp_Update2 %s, ovrp_WaitToBeginFrame %s, begin gate %d, hold physics %d",
        real_update2 ? "OK" : "MISSING", real_wait ? "OK" : "MISSING", begin_gate, hold_physics);
}

#define STEP_RENDER (-1)  // ovrpStep_Render

static void mouse_click_frame(void);  // below: controller presses as Unity mouse clicks

// xrWaitFrame blocks until the frame waited for before it has begun (xrBeginFrame). Unity begins frames on its render
// thread, which sometimes skips one (scene activation) and then waits for the main thread: our next wait never
// returned (Sniper Elite VR hung at its Init scene, main thread in SteamVR's CSxrSession::StartNextFrame, render
// thread idle). FramePort also renames libunity.so's "ovrp_BeginFrame"/"ovrp_EndFrame" lookups to the functions below.
// Once a begin has come through, the main thread first waits (≤ BEGIN_TIMEOUT_MS) for the last waited frame to begin
// and skips this frame's wait if it doesn't (normally the render thread begins it within a frame: pacing unchanged).
// OVRPlugin only begins the frame index it waited for (else "outside of frame bounds" and no xrBeginFrame, after which
// the next wait blocked for good), so a begin of another index while a wait is outstanding becomes a begin of the
// waited index, and the matching end follows it.
#define BEGIN_TIMEOUT_MS 50
static pthread_mutex_t begin_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t begin_cond = PTHREAD_COND_INITIALIZER;
static int begin_hooked;     // fpov_BeginFrame has been called: the gate is armed
static int outstanding = -1; // frame index waited for and not begun yet, else -1 (under begin_lock)
static int remap_from = -1, remap_to = -1;  // the last begin that was given the waited index (for its end)

static int last_wait_begun(int frame_index) {
    if (!begin_gate) return 1;
    pthread_mutex_lock(&begin_lock);
    struct timespec until;
    clock_gettime(CLOCK_REALTIME, &until);
    until.tv_nsec += BEGIN_TIMEOUT_MS * 1000000L;
    if (until.tv_nsec >= 1000000000L) until.tv_sec++, until.tv_nsec -= 1000000000L;
    while (begin_hooked && outstanding >= 0)
        if (pthread_cond_timedwait(&begin_cond, &begin_lock, &until)) break;
    int ok = !begin_hooked || outstanding < 0;
    if (ok) outstanding = frame_index;
    pthread_mutex_unlock(&begin_lock);
    return ok;
}

// Unity's physics step updates OVRPlugin's poses with prediction 0 ("now"). OVRPlugin takes "now" from the monotonic
// clock, which is the XrTime base on a Quest but not on the Frame (its XrTime ran 0.05-0.9 s ahead), so it located
// every node that much in the past (the runtime extrapolates backwards), and the render-step reads after it got those
// poses too: the game's hands trailed the controllers by several centimetres whenever they moved (Sniper Elite VR). A
// larger prediction doesn't reach (OVRPlugin caps it at ~0.07 s), so the physics update isn't passed on: OVRPlugin
// keeps the render update's display-time poses for the whole frame (physics and render reads agree).
#define STEP_PHYSICS 0

EXPORT int fpov_Update2(int step, int frame_index, double prediction_seconds) {
    pthread_once(&once, init);
    static int last_waited = -1, rendered, logged, skipped, last_result;
    if (step == STEP_RENDER && real_wait && frame_index != last_waited) {
        last_waited = frame_index;
        if (last_wait_begun(frame_index)) {
            int r = real_wait(frame_index);
            if (logged++ < 3) LOG("ovrp frame loop shim: waited for frame %d: %d", frame_index, r);
        } else if (skipped++ < 20 || skipped % 1000 == 0) {
            LOG("ovrp frame loop shim: frame %d: the last waited frame wasn't begun within %d ms, not waiting "
                "(%d times)", frame_index, BEGIN_TIMEOUT_MS, skipped);
        }
        mouse_click_frame();
    }
    if (hold_physics && step == STEP_PHYSICS && rendered) return last_result;  // see above: keeps the display-time poses
    int r = real_update2 ? real_update2(step, frame_index, prediction_seconds) : -1000;  // ovrpFailure
    if (step == STEP_RENDER) rendered = 1, last_result = r;
    return r;
}

// Unity's legacy ovrp_BeginFrame(frameIndex) / ovrp_EndFrame(frameIndex) on the render thread; further arguments
// (older plugins) are passed on as they are
typedef int (*PFN_FrameCall)(long, long, long, long);

static PFN_FrameCall ovrp_fn(const char *name) {
    void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
    PFN_FrameCall fn = ovrp ? (PFN_FrameCall)dlsym(ovrp, name) : 0;
    LOG("ovrp frame loop shim: %s %s", name, fn ? "OK" : "MISSING");
    return fn;
}

EXPORT int fpov_BeginFrame(long frame, long b, long c, long d) {
    static PFN_FrameCall real;
    static int logged;
    pthread_once(&once, init);  // the settings (begin_gate) before the first begin
    if (!real) real = ovrp_fn("ovrp_BeginFrame");
    pthread_mutex_lock(&begin_lock);
    long index = frame;
    if (begin_gate && outstanding >= 0 && outstanding != (int)frame) {
        index = outstanding;
        remap_from = (int)frame, remap_to = outstanding;
    } else {
        remap_from = remap_to = -1;
    }
    pthread_mutex_unlock(&begin_lock);
    int r = real ? real(index, b, c, d) : -1000;
    if (index != frame && logged++ < 20)
        LOG("ovrp frame loop shim: begin frame %ld as waited frame %ld: %d", frame, index, r);
    pthread_mutex_lock(&begin_lock);
    begin_hooked = 1;
    outstanding = -1;  // the waited frame (if any) is begun now
    pthread_cond_broadcast(&begin_cond);
    pthread_mutex_unlock(&begin_lock);
    return r;
}

EXPORT int fpov_EndFrame(long frame, long b, long c, long d) {
    static PFN_FrameCall real;
    if (!real) real = ovrp_fn("ovrp_EndFrame");
    pthread_mutex_lock(&begin_lock);
    if ((int)frame == remap_from) frame = remap_to;
    pthread_mutex_unlock(&begin_lock);
    return real ? real(frame, b, c, d) : -1000;
}

// ---------------------------------------------------------------- input diagnostics (pass-through)
// The game's C# input (OVRInput) asks OVRPlugin which controllers are connected and for their button state. These
// wrappers return exactly what OVRPlugin returns and log the connected mask and every change of the button bits, to
// see whether presses reach the game (Accounting+ stuck at "press any button" while the runtime reports presses).
typedef struct { unsigned int words[3]; } State;  // the common start: ConnectedControllers, Buttons, Touches
// ovrpControllerState2 is returned by value (through x8): the type must have its exact size, 64 bytes (4 uint32,
// IndexTrigger[2], HandTrigger[2], Thumbstick[2] and Touchpad[2] as float pairs)
typedef struct { unsigned int words[16]; } State2;
_Static_assert(sizeof(State2) == 64, "ovrpControllerState2 is 64 bytes");
typedef unsigned int (*PFN_GetConnected)(void);
typedef int (*PFN_GetState4)(unsigned int mask, State *state);  // (written by OVRPlugin; only the start is read)
typedef State2 (*PFN_GetState2)(unsigned int mask);
static PFN_GetConnected real_connected;
static PFN_GetState4 real_state4;
static PFN_GetState2 real_state2;
static pthread_once_t input_once = PTHREAD_ONCE_INIT;

static void input_init(void) {
    void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
    if (!ovrp) return;
    real_connected = (PFN_GetConnected)dlsym(ovrp, "ovrp_GetConnectedControllers");
    real_state4 = (PFN_GetState4)dlsym(ovrp, "ovrp_GetControllerState4");
    real_state2 = (PFN_GetState2)dlsym(ovrp, "ovrp_GetControllerState2");
}

// per requested controller mask (the game may ask for Touch, Remote, Gamepad... separately)
static struct { unsigned int mask, connected, buttons; int seen; } masks[8];
static int changes;

// Face buttons on the Frame's controllers can read as clicked while only touched (Accounting+: buttons == touches ==
// A|B|X|Y held for seconds). A button held for more than STUCK_NS is reported released until it really lets go, so
// real presses (short) still register as a change.
#include <time.h>
#define STUCK_NS 2000000000ll
static long long held_since[32];
static unsigned int suppressed;

static long long now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000000000ll + t.tv_nsec;
}

static unsigned int unstick(unsigned int buttons) {
    long long now = now_ns();
    for (int b = 0; b < 32; b++) {
        unsigned int bit = 1u << b;
        if (!(buttons & bit)) {
            held_since[b] = 0;
            suppressed &= ~bit;
            continue;
        }
        if (!held_since[b]) held_since[b] = now;
        if (now - held_since[b] > STUCK_NS && !(suppressed & bit)) {
            suppressed |= bit;
            static int logged;
            if (logged++ < 20) LOG("ovrp input: button 0x%x held for > 2 s: reported released until let go", bit);
        }
    }
    return buttons & ~suppressed;
}

static void note_state(const char *which, unsigned int mask, const State *s) {
    int i = 0;
    while (i < 8 && masks[i].seen && masks[i].mask != mask) i++;
    if (i == 8) return;
    if (!masks[i].seen || masks[i].buttons != s->words[1] || masks[i].connected != s->words[0]) {
        if (changes++ < 400)
            LOG("ovrp input: %s(mask 0x%x): connected 0x%x buttons 0x%x touches 0x%x%s", which, mask, s->words[0],
                s->words[1], s->words[2], masks[i].seen ? "" : " (first call with this mask)");
        masks[i].seen = 1;
        masks[i].mask = mask;
        masks[i].connected = s->words[0];
        masks[i].buttons = s->words[1];
    }
}

EXPORT unsigned int fpov_GetConnectedControllers(void) {
    pthread_once(&input_once, input_init);
    unsigned int c = real_connected ? real_connected() : 0;
    static unsigned int last = 0xffffffffu;
    static int logged;
    if (c != last && logged++ < 20) LOG("ovrp input: connected controllers 0x%x", c);
    last = c;
    return c;
}

EXPORT int fpov_GetControllerState4(unsigned int mask, State *state) {
    pthread_once(&input_once, input_init);
    int r = real_state4 ? real_state4(mask, state) : -1000;
    if (state) {
        // ovrpControllerState4 continues with 4 floats after the 4 masks: L/R IndexTrigger, L/R HandTrigger. OVRInput
        // turns them into the trigger/grip buttons at 0.5; log each time one crosses it (per requested mask)
        const float *axis = (const float *)((const char *)state + 16);
        static struct { unsigned int mask, bits; } seen[8];
        static int axis_logs;
        unsigned int bits = 0;
        for (int k = 0; k < 4; ++k) bits |= (axis[k] >= 0.5f) << k;
        int i = 0;
        while (i < 8 && seen[i].mask && seen[i].mask != mask) i++;
        if (i < 8 && (!seen[i].mask || seen[i].bits != bits)) {
            if (seen[i].mask && axis_logs++ < 200)
                LOG("ovrp input: State4(mask 0x%x) triggers L %.2f R %.2f grips L %.2f R %.2f", mask, axis[0], axis[1],
                    axis[2], axis[3]);
            seen[i].mask = mask;
            seen[i].bits = bits;
        }
        note_state("State4", mask, state);
        state->words[1] = unstick(state->words[1]);
    }
    return r;
}

EXPORT State2 fpov_GetControllerState2(unsigned int mask) {
    pthread_once(&input_once, input_init);
    State2 s = {{0}};
    if (real_state2) s = real_state2(mask);
    note_state("State2", mask, (const State *)&s);
    s.words[1] = unstick(s.words[1]);
    return s;
}

// Unity's own input (libunity.so: Input.GetKey/anyKey on the Oculus device) reads ovrp_GetControllerState (v1,
// returned by value, 48 bytes: 4 uint32, IndexTrigger[2], HandTrigger[2], Thumbstick[2]) and ovrp_GetControllerState2
typedef struct { unsigned int words[12]; } State1;
_Static_assert(sizeof(State1) == 48, "ovrpControllerState is 48 bytes");
typedef State1 (*PFN_GetState1)(unsigned int mask);
static PFN_GetState1 real_state1;

EXPORT State1 fpov_GetControllerState(unsigned int mask) {
    pthread_once(&input_once, input_init);
    if (!real_state1) {
        void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
        if (ovrp) real_state1 = (PFN_GetState1)dlsym(ovrp, "ovrp_GetControllerState");
    }
    State1 s = {{0}};
    if (real_state1) s = real_state1(mask);
    note_state("State1(unity)", mask, (const State *)&s);
    return s;
}

// Oculus Utilities (1.3x) ignores every button unless OVRPlugin reports input focus; with Unity 2017's legacy frame
// loop on OVRPort's OVRPlugin that may never be true. Report it as true (the session's own focus still pauses the game)
// and log what OVRPlugin said.
typedef int (*PFN_GetInputFocus)(int *has_focus);
static PFN_GetInputFocus real_input_focus;

EXPORT int fpov_GetAppHasInputFocus(int *has_focus) {
    pthread_once(&input_once, input_init);
    if (!real_input_focus) {
        void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
        if (ovrp) real_input_focus = (PFN_GetInputFocus)dlsym(ovrp, "ovrp_GetAppHasInputFocus");
    }
    int value = 0, r = real_input_focus ? real_input_focus(&value) : -1000;
    static int last = -2, logged;
    if ((r < 0 ? -1 : value) != last && logged++ < 20) LOG("ovrp input: OVRPlugin input focus %d (result %d)", value, r);
    last = r < 0 ? -1 : value;
    if (has_focus) *has_focus = 1;
    return 0;  // ovrpSuccess
}

// ---------------------------------------------------------------- controller presses as mouse clicks
// Unity 2017-2019 games made for Go/Gear VR-era input wait for Input.GetMouseButtonDown(0/1), which a Quest delivers
// for the controller's primary press (Accounting+'s "press any button" motion warning checks only that). Lepton runs
// VR apps without a focused Android window, so no touch/mouse event ever arrives. IL2CPP looks engine functions up by
// name (il2cpp_resolve_icall) on first use: this registers a replacement for UnityEngine.Input::GetMouseButtonDown
// that returns Unity's answer, or true in the frame a Touch trigger (>= 0.5) or A/B/X/Y is newly pressed.
#define MOUSE_ICALL "UnityEngine.Input::GetMouseButtonDown(System.Int32)"
typedef void (*PFN_AddICall)(const char *name, const void *fn);
typedef const void *(*PFN_ResolveICall)(const char *name);
typedef int (*PFN_MouseDown)(int button);
static PFN_MouseDown real_mouse_down;
static volatile int click_now;  // set for the frame in which a press began

static int fp_mouse_down(int button) {
    int real = real_mouse_down ? real_mouse_down(button) : 0;
    return real || ((button == 0 || button == 1) && click_now);
}

static void mouse_install(void) {
    static int tries;
    if (real_mouse_down || tries > 600) return;  // ~10 s of frames
    tries++;
    PFN_AddICall add = (PFN_AddICall)dlsym(RTLD_DEFAULT, "il2cpp_add_internal_call");
    PFN_ResolveICall resolve = (PFN_ResolveICall)dlsym(RTLD_DEFAULT, "il2cpp_resolve_icall");
    if (!add || !resolve) {
        void *il2cpp = dlopen("libil2cpp.so", RTLD_NOW | RTLD_NOLOAD);
        if (il2cpp) {
            add = (PFN_AddICall)dlsym(il2cpp, "il2cpp_add_internal_call");
            resolve = (PFN_ResolveICall)dlsym(il2cpp, "il2cpp_resolve_icall");
        }
    }
    if (!add || !resolve) return;
    const void *current = resolve(MOUSE_ICALL);
    if (!current) return;  // Unity hasn't registered its own yet: try again next frame
    real_mouse_down = (PFN_MouseDown)current;
    add(MOUSE_ICALL, (const void *)fp_mouse_down);
    LOG("controller presses count as mouse clicks (%s %s)", MOUSE_ICALL,
        resolve(MOUSE_ICALL) == (const void *)fp_mouse_down ? "replaced" : "NOT replaced");
}

static void mouse_click_frame(void) {
    mouse_install();
    pthread_once(&input_once, input_init);
    static int was_pressed;
    unsigned char raw[512] __attribute__((aligned(16))) = {0};  // ovrpControllerState4 (+ room for newer layouts)
    int pressed = 0;
    if (real_state4 && real_state4(0x3, (State *)raw) >= 0) {
        const unsigned int *w = (const unsigned int *)raw;
        const float *axis = (const float *)(raw + 16);  // L/R IndexTrigger, L/R HandTrigger
        pressed = (w[1] & 0x303u) != 0 || axis[0] >= 0.5f || axis[1] >= 0.5f;  // A, B, X, Y or a trigger
    }
    click_now = pressed && !was_pressed;
    was_pressed = pressed;
}

// ---------------------------------------------------------------- hand nodes
// Unity's built-in Oculus XR input creates its left/right controller devices (InputDevices.GetDeviceAtXRNode, the
// Touch buttons and triggers games read through CommonUsages) for the hand nodes OVRPlugin reports as present
// (ovrp_GetNodePresent). Logged per node; a hand node whose Touch controller is connected counts as present.
typedef int (*PFN_NodePresent)(int node);
#define NODE_HAND_LEFT 3
#define NODE_HAND_RIGHT 4

EXPORT int fpov_GetNodePresent(int node) {
    static PFN_NodePresent real;
    static int logged[16];
    pthread_once(&input_once, input_init);
    if (!real) {
        void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
        if (ovrp) real = (PFN_NodePresent)dlsym(ovrp, "ovrp_GetNodePresent");
    }
    int present = real ? real(node) : 0, answer = present;
    if (!present && (node == NODE_HAND_LEFT || node == NODE_HAND_RIGHT) && real_connected)
        answer = (real_connected() & (node == NODE_HAND_LEFT ? 0x1u : 0x2u)) != 0;  // LTouch / RTouch
    if (node >= 0 && node < 16 && logged[node] != (answer ? 2 : 1)) {
        logged[node] = answer ? 2 : 1;
        LOG("ovrp node %d present: OVRPlugin %d -> Unity %d", node, present, answer);
    }
    return answer;
}

// ---------------------------------------------------------------- user presence (patch frame.unity_user_presence)
// Unity's Oculus XR Plugin gives its HMD input device a UserPresence feature from ovrp_GetUserPresent2. On the Frame
// OVRPort's OVRPlugin answers "not worn" a few seconds after start while the headset is worn, and games that drive
// their rig only while the user is present stay frozen (BONELAB's Marrow rig: XRHMD.IsUserPresent). The plugin's
// lookup is renamed to this function, which reports the user as present and logs OVRPlugin's own answer on change.
typedef int (*PFN_UserPresent2)(int *present);

EXPORT int fpov_GetUserPresent2(int *present) {
    static PFN_UserPresent2 real;
    if (!real) {
        void *ovrp = dlopen("libOVRPlugin.so", RTLD_NOW | RTLD_NOLOAD);
        if (ovrp) real = (PFN_UserPresent2)dlsym(ovrp, "ovrp_GetUserPresent2");
    }
    int value = 0, r = real ? real(&value) : -1000;
    static int last = -2, logged;
    if ((r < 0 ? -1 : value) != last && logged++ < 20) LOG("user presence: OVRPlugin %d (result %d) -> 1", value, r);
    last = r < 0 ? -1 : value;
    if (present) *present = 1;
    return 0;  // ovrpSuccess
}
