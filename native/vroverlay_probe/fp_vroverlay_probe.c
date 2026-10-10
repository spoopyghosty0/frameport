/* fp_vroverlay_probe.exe — can a Windows OpenVR overlay application show something on the Steam Frame (under Proton)?
 *
 * The Windows twin of vroverlay_probe.py: connects as VRApplication_Overlay through the given openvr_api.dll (any
 * app's copy; Proton's vrclient bridges it to the Frame's SteamVR), creates a head-locked overlay 1.5 m in front of
 * the headset (magenta/green checker), a world overlay (yellow/blue) and a dashboard overlay, logs every
 * EVROverlayError and the visible flags every 5 s, and stays alive for the given number of seconds.
 *
 *     fp_vroverlay_probe.exe <openvr_api.dll> [seconds]
 *
 * Exit code 0 when every overlay call succeeded, 1 when one failed, 2 on bad usage / OpenVR init failure.
 * Freestanding (no CRT, no Windows SDK headers): built by native/build.py (--only vroverlay) like fp_vrsettings.
 */
typedef void *HANDLE;
typedef unsigned long DWORD;
typedef int BOOL;
typedef unsigned long long u64;
typedef unsigned u32;

#define WINAPI __attribute__((ms_abi))
#define IMPORT __declspec(dllimport)
IMPORT HANDLE WINAPI LoadLibraryA(const char *);
IMPORT void *WINAPI GetProcAddress(HANDLE, const char *);
IMPORT char *WINAPI GetCommandLineA(void);
IMPORT HANDLE WINAPI GetStdHandle(DWORD);
IMPORT BOOL WINAPI WriteFile(HANDLE, const void *, DWORD, DWORD *, void *);
IMPORT void WINAPI Sleep(DWORD);
IMPORT __attribute__((noreturn)) void WINAPI ExitProcess(unsigned);

#define STD_OUTPUT_HANDLE ((DWORD)-11)
#define VRApplication_Overlay 2
int _fltused = 1;

typedef struct { float m[3][4]; } Mat34;
typedef int Err;
/* openvr_capi.h (OpenVR 2.5.1) struct VR_IVROverlay_FnTable, IVROverlay_027: only the slots used here are typed */
typedef struct {
    Err (*FindOverlay)(const char *, u64 *);                                       /* 0 */
    Err (*CreateOverlay)(const char *, const char *, u64 *);                       /* 1 */
    void *pad2_6[5];
    const char *(*GetOverlayErrorNameFromEnum)(Err);                               /* 7 */
    void *pad8_20[13];
    Err (*SetOverlayWidthInMeters)(u64, float);                                    /* 21 */
    void *pad22_31[10];
    Err (*SetOverlayTransformAbsolute)(u64, int, Mat34 *);                         /* 32 */
    void *pad33;
    Err (*SetOverlayTransformTrackedDeviceRelative)(u64, u32, Mat34 *);            /* 34 */
    void *pad35_40[6];
    Err (*ShowOverlay)(u64);                                                       /* 41 */
    Err (*HideOverlay)(u64);                                                       /* 42 */
    unsigned char (*IsOverlayVisible)(u64);                                        /* 43 */
    void *pad44_59[16];
    Err (*SetOverlayRaw)(u64, void *, u32, u32, u32);                              /* 60 */
    Err (*SetOverlayFromFile)(u64, const char *);                                  /* 61 */
    void *pad62_64[3];
    Err (*CreateDashboardOverlay)(const char *, const char *, u64 *, u64 *);       /* 65 */
    unsigned char (*IsDashboardVisible)(void);                                     /* 66 */
} OverlayFnTable;

typedef unsigned (*PFN_InitInternal2)(int *, int, const char *);
typedef void (*PFN_ShutdownInternal)(void);
typedef void *(*PFN_GetGenericInterface)(const char *, int *);

static void out(const char *s) {
    DWORD n = 0, len = 0;
    while (s[len]) len++;
    WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), s, len, &n, 0);
}

static void out_int(long long v) {
    char buf[24], *p = buf + sizeof buf;
    int neg = v < 0;
    unsigned long long u = neg ? (unsigned long long)-v : (unsigned long long)v;
    *--p = 0;
    do *--p = (char)('0' + u % 10); while (u /= 10);
    if (neg) *--p = '-';
    out(p);
}

static int split(char *p, char **argv, int max) {
    int n = 0;
    while (*p && n < max) {
        while (*p == ' ' || *p == '\t') p++;
        if (!*p) break;
        if (*p == '"') {
            argv[n++] = ++p;
            while (*p && *p != '"') p++;
        } else {
            argv[n++] = p;
            while (*p && *p != ' ' && *p != '\t') p++;
        }
        if (*p) *p++ = 0;
    }
    return n;
}

static OverlayFnTable *ov;
static int failed;

static Err report(const char *what, Err e) {
    out(what);
    out(" -> ");
    out_int(e);
    out(" ");
    const char *name = e ? ov->GetOverlayErrorNameFromEnum(e) : "None";
    out(name ? name : "?");
    out("\n");
    if (e) failed = 1;
    return e;
}

#define W 256
static unsigned char img_head[W * W * 4], img_world[W * W * 4];

static void checker(unsigned char *buf, const unsigned char *a, const unsigned char *b) {
    for (int y = 0; y < W; y++)
        for (int x = 0; x < W; x++) {
            const unsigned char white[4] = {255, 255, 255, 255};
            const unsigned char *c = ((x / 32 + y / 32) & 1) ? b : a;
            if (x < 4 || y < 4 || x >= W - 4 || y >= W - 4) c = white;
            for (int i = 0; i < 4; i++) buf[(y * W + x) * 4 + i] = c[i];
        }
}

static Mat34 translation(float x, float y, float z) {
    Mat34 m = {{{1, 0, 0, 0}, {0, 1, 0, 0}, {0, 0, 1, 0}}};
    m.m[0][3] = x;
    m.m[1][3] = y;
    m.m[2][3] = z;
    return m;
}

void start(void) {
    static char *argv[8];
    int argc = split(GetCommandLineA(), argv, 8);
    if (argc < 2) {
        out("usage: fp_vroverlay_probe.exe <openvr_api.dll> [seconds]\n");
        ExitProcess(2);
    }
    long long seconds = 60;
    if (argc > 2) {
        seconds = 0;
        for (const char *s = argv[2]; *s >= '0' && *s <= '9'; s++) seconds = seconds * 10 + (*s - '0');
    }
    HANDLE lib = LoadLibraryA(argv[1]);
    PFN_InitInternal2 init = lib ? (PFN_InitInternal2)GetProcAddress(lib, "VR_InitInternal2") : 0;
    PFN_ShutdownInternal shutdown = lib ? (PFN_ShutdownInternal)GetProcAddress(lib, "VR_ShutdownInternal") : 0;
    PFN_GetGenericInterface iface = lib ? (PFN_GetGenericInterface)GetProcAddress(lib, "VR_GetGenericInterface") : 0;
    if (!init || !shutdown || !iface) {
        out("error could not load openvr_api.dll\n");
        ExitProcess(2);
    }
    int err = 0;
    init(&err, VRApplication_Overlay, 0);
    out("VR_InitInternal2(Overlay) -> ");
    out_int(err);
    out("\n");
    if (err) ExitProcess(2);
    ov = (OverlayFnTable *)iface("FnTable:IVROverlay_027", &err);
    if (!ov || err) {
        out("error no FnTable:IVROverlay_027\n");
        shutdown();
        ExitProcess(2);
    }
    const unsigned char mag[4] = {255, 0, 255, 255}, grn[4] = {0, 200, 0, 255};
    const unsigned char yel[4] = {255, 220, 0, 255}, blu[4] = {0, 60, 255, 255};
    checker(img_head, mag, grn);
    checker(img_world, yel, blu);
    u64 head = 0, world = 0, dmain = 0, dthumb = 0;
    if (!report("CreateOverlay(head)", ov->CreateOverlay("frameport.probe.win.head", "FramePort probe (head, Proton)", &head))) {
        Mat34 m = translation(0, 0, -1.5f);
        report("SetOverlayRaw(head)", ov->SetOverlayRaw(head, img_head, W, W, 4));
        report("SetOverlayWidthInMeters(head)", ov->SetOverlayWidthInMeters(head, 0.6f));
        report("SetOverlayTransformTrackedDeviceRelative(head)", ov->SetOverlayTransformTrackedDeviceRelative(head, 0, &m));
        report("ShowOverlay(head)", ov->ShowOverlay(head));
    }
    if (!report("CreateOverlay(world)", ov->CreateOverlay("frameport.probe.win.world", "FramePort probe (world, Proton)", &world))) {
        Mat34 m = translation(0, 1.4f, -1.5f);
        report("SetOverlayRaw(world)", ov->SetOverlayRaw(world, img_world, W, W, 4));
        report("SetOverlayWidthInMeters(world)", ov->SetOverlayWidthInMeters(world, 1.0f));
        report("SetOverlayTransformAbsolute(world)", ov->SetOverlayTransformAbsolute(world, 1, &m));
        report("ShowOverlay(world)", ov->ShowOverlay(world));
    }
    if (!report("CreateDashboardOverlay(dash)", ov->CreateDashboardOverlay("frameport.probe.win.dash", "FramePort probe (Proton)", &dmain, &dthumb))) {
        report("SetOverlayRaw(dash main)", ov->SetOverlayRaw(dmain, img_head, W, W, 4));
        report("SetOverlayRaw(dash thumb)", ov->SetOverlayRaw(dthumb, img_world, W, W, 4));
    }
    for (long long t = 0;; t += 5) {
        out("state t=");
        out_int(t);
        out(" head visible=");
        out_int(ov->IsOverlayVisible(head));
        out(" world visible=");
        out_int(ov->IsOverlayVisible(world));
        out(" dashboard visible=");
        out_int(ov->IsDashboardVisible());
        out("\n");
        if (t >= seconds) break;
        Sleep(5000);
    }
    shutdown();
    out(failed ? "done, some calls failed\n" : "done, failed calls: none\n");
    ExitProcess(failed);
}
