/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef FP_ROOMSCAN_H
#define FP_ROOMSCAN_H
#include "platform.h"

/* Resource limits are independent of dump length. Two passes avoid retaining frames. */
#define MAX_FRAME_VERTICES 1000000u
#define MAX_FRAME_INDICES 3000000u
#define MAX_CELLS 4200000u
#define MAX_SAMPLES 32768
#define MAX_PLANES 32
#define MAX_OUTLINE 96
#define MAX_VOLUMES 128
#define MAX_TRIANGLES 200000
#define MAX_MESH_VERTICES 600000
#define PI 3.14159265358979323846f

typedef struct { float x, y, z; } Vec;
typedef struct { float x, z; } Pt;
typedef struct {
    Vec p, n;
    float weight, sdf, sw;
    int mark;
} Cell;
typedef struct {
    Vec n;
    float d, lo, hi, bottom, top, area;
    int used;
} Plane;
typedef struct { Vec p; float yaw, w, d, h; const char *label; } Volume;
typedef struct {
    const char *input, *out, *hint;
    u64 universe;
    float voxel, ceiling_default, truncation, max_edge, grazing, range;
    float plane_distance, min_plane_area, wall_height, snap_degrees;
    float floor_band, ceiling_min, min_volume, cluster_gap, gap, horizontal;
} Options;
typedef struct {
    Options opt;
    Cell *cells;
    int nx, ny, nz, count;
    Vec origin;
    Vec samples[MAX_SAMPLES];
    int nsamples;
    u64 seen, accepted, rejected, clipped;
    unsigned frames;
    Plane planes[MAX_PLANES];
    int nplanes;
    Pt outline[MAX_OUTLINE];
    int invisible[MAX_OUTLINE], noutline;
    Volume volumes[MAX_VOLUMES];
    int nvolumes;
    float floor, ceiling, coverage;
    const char *warnings[24];
    int nwarnings, hint_used;
    u32 rng;
    Vec *vertices;
    u32 *indices;
    int nv, ni;
    i64 t0, tread, tfuse, tmodel, tmesh;
} Scan;

static inline float ab(float x) { return x < 0 ? -x : x; }
static inline float mn(float a, float b) { return a < b ? a : b; }
static inline float mx(float a, float b) { return a > b ? a : b; }
static inline float clamp(float x, float a, float b) { return mn(mx(x, a), b); }
static inline Vec vec(float x, float y, float z) { return (Vec){x,y,z}; }
static inline Vec add(Vec a, Vec b) { return vec(a.x+b.x,a.y+b.y,a.z+b.z); }
static inline Vec sub(Vec a, Vec b) { return vec(a.x-b.x,a.y-b.y,a.z-b.z); }
static inline Vec mul(Vec a, float b) { return vec(a.x*b,a.y*b,a.z*b); }
static inline float dot(Vec a, Vec b) { return a.x*b.x+a.y*b.y+a.z*b.z; }
static inline Vec cross(Vec a, Vec b) { return vec(a.y*b.z-a.z*b.y,a.z*b.x-a.x*b.z,a.x*b.y-a.y*b.x); }
static inline int finite(float x) { return x == x && ab(x) < 1e20f; }
float root(float x);
float sine(float x);
float cosine(float x);
float angle(float y, float x);
Vec unit(Vec p);
int flo(float x);
u32 random_next(Scan *s);
void warn(Scan *s, const char *message);
int cell_index(Scan *s, Vec p);
Vec grid_point(Scan *s, int x, int y, int z);
const char *read_dump(Scan *s, int fuse);
const char *init_grid(Scan *s);
void finish_fusion(Scan *s);
const char *make_model(Scan *s);
const char *make_mesh(Scan *s);
int same(const char *a, const char *b);

#endif
