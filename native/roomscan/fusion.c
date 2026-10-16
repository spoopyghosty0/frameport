/* SPDX-License-Identifier: GPL-3.0-only */
#include "scan.h"

#define MIN_TRIANGLE_TWICE_AREA 1e-8f
#define MIN_RANGE .1f
#define MAX_COORDINATE 10000.f
#define QUATERNION_NORM_TOLERANCE .1f
#define SAMPLE_SPACING_VOXELS .75f
#define MAX_SURFEL_WEIGHT 10.f
#define FEW_FRAMES 8
#define MAX_GRID_XZ 8.f
#define MAX_GRID_Y 4.f
#define GRID_PADDING_VOXELS 5.f

float root(float x)
{
    if (x <= 0) return 0;
    /* A float exponent seed plus Newton steps avoids a libm dependency on either host. */
    union { float f; u32 u; } a = {x};
    a.u = (a.u >> 1) + 0x1fc00000u;
    for (int i = 0; i < 5; i++) a.f = .5f * (a.f + x / a.f);
    return a.f;
}
Vec unit(Vec p) { float n = root(dot(p,p)); return n > 1e-12f ? mul(p,1/n) : vec(0,0,0); }
int flo(float x) { int i = (int)x; return x < (float)i ? i-1 : i; }
float sine(float x)
{
    while (x > PI) x -= 2*PI;
    while (x < -PI) x += 2*PI;
    if (x > PI/2) x = PI-x;
    if (x < -PI/2) x = -PI-x;
    float q = x*x;
    return x*(1-q/6+q*q/120-q*q*q/5040+q*q*q*q/362880);
}
float cosine(float x) { return sine(x+PI/2); }
float angle(float y, float x)
{
    /* atan approximation, maximum error < .012 degrees. */
    float a = mn(ab(x),ab(y)) / mx(mx(ab(x),ab(y)),1e-20f), q = a*a;
    float r = a*(.999866f+q*(-.3302995f+q*(.180141f+q*(-.085133f+q*.0208351f))));
    if (ab(y) > ab(x)) r = PI/2-r;
    if (x < 0) r = PI-r;
    return y < 0 ? -r : r;
}
int same(const char *a, const char *b)
{
    while (*a && *a == *b) a++,b++;
    return *a == *b;
}
u32 random_next(Scan *s) { s->rng ^= s->rng<<13; s->rng ^= s->rng>>17; s->rng ^= s->rng<<5; return s->rng; }
void warn(Scan *s, const char *m)
{
    for (int i = 0; i < s->nwarnings; i++) if (same(s->warnings[i],m)) return;
    if (s->nwarnings < 24) s->warnings[s->nwarnings++] = m;
}
static u32 u32le(const u8 *p) { return (u32)p[0]|(u32)p[1]<<8|(u32)p[2]<<16|(u32)p[3]<<24; }
static float f32le(const u8 *p) { union { u32 u; float f; } v = {u32le(p)}; return v.f; }
/* 1 = complete, 0 = clean EOF, -1 = I/O error or partial record. */
static int exact(int fd, void *p, usize n)
{
    usize done = 0;
    while (done < n) {
        long r = io_read(fd,(u8 *)p+done,n-done);
        if (r < 0 || (!r && done)) return -1;
        if (!r) return 0;
        done += (usize)r;
    }
    return 1;
}
Vec grid_point(Scan *s, int x, int y, int z)
{
    return add(s->origin,vec(x*s->opt.voxel,y*s->opt.voxel,z*s->opt.voxel));
}
int cell_index(Scan *s, Vec p)
{
    Vec q = mul(sub(p,s->origin),1/s->opt.voxel);
    int x = flo(q.x+.5f), y = flo(q.y+.5f), z = flo(q.z+.5f);
    if (x < 0 || y < 0 || z < 0 || x >= s->nx || y >= s->ny || z >= s->nz) return -1;
    return (y*s->nz+z)*s->nx+x;
}
static void sample(Scan *s, Vec p, Vec n, float w)
{
    int i = cell_index(s,p);
    if (i < 0) { s->clipped++; return; }
    Cell *c = &s->cells[i];
    /* Cap repeated views' influence without throwing away later measurements. */
    float scale = c->weight > MAX_SURFEL_WEIGHT ? MAX_SURFEL_WEIGHT/c->weight : 1;
    c->p = add(mul(c->p,scale),mul(p,w));
    c->n = add(mul(c->n,scale),mul(n,w));
    c->weight = c->weight*scale+w;
}
static void triangle(Scan *s, Vec a, Vec b, Vec c, Vec eye, int fuse)
{
    Vec abv = sub(b,a), ac = sub(c,a), bc = sub(c,b);
    float longest = root(mx(dot(abv,abv),mx(dot(ac,ac),dot(bc,bc))));
    Vec p = mul(add(a,add(b,c)),1.f/3), n = cross(abv,ac);
    float twice_area = root(dot(n,n));
    Vec view = sub(eye,p);
    float distance = root(dot(view,view));
    if (twice_area < MIN_TRIANGLE_TWICE_AREA || longest > s->opt.max_edge || distance > s->opt.range || distance < MIN_RANGE) {
        if (!fuse) s->rejected++;
        return;
    }
    n = mul(n,1/twice_area);
    float facing = dot(n,view)/distance;
    if (ab(facing) < s->opt.grazing) { if (!fuse) s->rejected++; return; }
    if (facing < 0) n = mul(n,-1);
    if (!fuse) {
        s->accepted++;
        s->seen++;
        int slot = s->seen <= MAX_SAMPLES ? (int)s->seen-1 : (int)(random_next(s)%s->seen);
        if (slot < MAX_SAMPLES) s->samples[slot] = p;
        s->nsamples = (int)mn((float)s->seen,MAX_SAMPLES);
    } else {
        int steps = (int)(longest/(s->opt.voxel*SAMPLE_SPACING_VOXELS))+1;
        float weight = twice_area*facing*facing / ((steps+1)*(steps+2)*.5f);
        for (int j = 0; j <= steps; j++) for (int k = 0; k <= steps-j; k++) {
            Vec q = add(a,add(mul(abv,(float)j/steps),mul(ac,(float)k/steps)));
            sample(s,q,n,weight);
        }
    }
}
const char *read_dump(Scan *s, int fuse)
{
    int fd = io_open(s->opt.input,0);
    if (fd < 0) return "cannot open scan dump";
    u8 header[44];
    const char *error = 0;
    Vec *vertices = 0;
    u8 *indices = 0;
    usize vs = 0, is = 0;
    if (exact(fd,header,8) != 1 || header[0]!='F' || header[1]!='P' || header[2]!='R' || header[3]!='S' ||
        u32le(header+4) != 1) { error = "invalid FPRS header"; goto done; }
    unsigned frames = 0;
    for (;;) {
        int r = exact(fd,header,44);
        if (r == 0) break;
        if (r < 0) { error = "truncated frame header"; break; }
        /* Timestamps must be finite too; they are opaque to reconstruction. */
        if ((u32le(header+4)&0x7ff00000u) == 0x7ff00000u) { error = "invalid frame timestamp"; break; }
        float pose[7];
        for (int i = 0; i < 7; i++) {
            pose[i] = f32le(header+8+i*4);
            if (!finite(pose[i]) || ab(pose[i]) > MAX_COORDINATE) error = "invalid frame pose";
        }
        float norm = 0;
        for (int i = 3; i < 7; i++) norm += pose[i]*pose[i];
        if (ab(norm-1) > QUATERNION_NORM_TOLERANCE) error = "invalid pose quaternion";
        if (error) break;
        for (int i = 3; i < 7; i++) pose[i] /= root(norm);
        u32 nv = u32le(header+36), ni = u32le(header+40);
        if (nv > MAX_FRAME_VERTICES || ni > MAX_FRAME_INDICES || ni%3 || (!nv && ni)) {
            error = "invalid frame counts"; break;
        }
        usize needv = (usize)nv*12, needi = (usize)ni*4;
        if (needv > vs) { io_free(vertices,vs); vs=needv; vertices=io_alloc(vs); }
        if (needi > is) { io_free(indices,is); is=needi; indices=io_alloc(is); }
        if ((nv && !vertices) || (ni && !indices)) { error = "frame allocation failed"; break; }
        if ((needv && exact(fd,vertices,needv)!=1) || (needi && exact(fd,indices,needi)!=1)) {
            error = "truncated frame data"; break;
        }
        Vec eye = vec(pose[0],pose[1],pose[2]), q = vec(pose[3],pose[4],pose[5]);
        for (u32 i = 0; i < nv; i++) {
            u8 *p = (u8 *)&vertices[i];
            Vec v = vec(f32le(p),f32le(p+4),f32le(p+8));
            if (!finite(v.x) || !finite(v.y) || !finite(v.z) ||
                ab(v.x)>MAX_COORDINATE || ab(v.y)>MAX_COORDINATE || ab(v.z)>MAX_COORDINATE) {
                error = "invalid vertex"; break;
            }
            Vec t = mul(cross(q,v),2);
            vertices[i] = add(eye,add(v,add(mul(t,pose[6]),cross(q,t))));
        }
        if (error) break;
        for (u32 i = 0; i < ni; i+=3) {
            u32 a=u32le(indices+4*i), b=u32le(indices+4*(i+1)), c=u32le(indices+4*(i+2));
            if (a>=nv || b>=nv || c>=nv) { error = "index outside vertex array"; break; }
            triangle(s,vertices[a],vertices[b],vertices[c],eye,fuse);
        }
        if (error) break;
        frames++;
    }
    if (!error && !frames) error = "scan contains no frames";
    if (!error && !fuse) s->frames = frames;
done:
    io_free(vertices,vs);
    io_free(indices,is);
    io_close(fd);
    return error;
}
static float median(Scan *s, int axis)
{
    /* In-place quickselect on the bounded reservoir; no dump-sized allocations. */
    int lo=0, hi=s->nsamples-1, mid=s->nsamples/2;
    while (lo < hi) {
        float pivot = ((float *)&s->samples[(lo+hi)/2])[axis];
        int i=lo,j=hi;
        while (i<=j) {
            while (((float *)&s->samples[i])[axis] < pivot) i++;
            while (((float *)&s->samples[j])[axis] > pivot) j--;
            if (i<=j) { Vec t=s->samples[i]; s->samples[i++]=s->samples[j]; s->samples[j--]=t; }
        }
        if (mid<=j) hi=j;
        else if (mid>=i) lo=i;
        else break;
    }
    return ((float *)&s->samples[mid])[axis];
}
const char *init_grid(Scan *s)
{
    if (!s->nsamples) return "scan contains no usable triangles";
    float x=median(s,0), y=median(s,1), z=median(s,2), v=s->opt.voxel;
    Vec low=s->samples[0],high=low;
    for(int i=1;i<s->nsamples;i++) {
        Vec p=s->samples[i];
        low=vec(mn(low.x,p.x),mn(low.y,p.y),mn(low.z,p.z));
        high=vec(mx(high.x,p.x),mx(high.y,p.y),mx(high.z,p.z));
    }
    float pad=v*mx(GRID_PADDING_VOXELS,s->opt.truncation+2);
    low=sub(low,vec(pad,pad,pad));high=add(high,vec(pad,pad,pad));
    s->origin=vec(mx(flo(low.x/v)*v,flo((x-MAX_GRID_XZ/2)/v)*v),
                  mx(flo(low.y/v)*v,flo((y-MAX_GRID_Y/2)/v)*v),
                  mx(flo(low.z/v)*v,flo((z-MAX_GRID_XZ/2)/v)*v));
    s->nx=(int)mn(-flo(-(high.x-s->origin.x)/v)+1,(int)(MAX_GRID_XZ/v)+1);
    s->ny=(int)mn(-flo(-(high.y-s->origin.y)/v)+1,(int)(MAX_GRID_Y/v)+1);
    s->nz=(int)mn(-flo(-(high.z-s->origin.z)/v)+1,(int)(MAX_GRID_XZ/v)+1);
    usize count = (usize)s->nx*s->ny*s->nz;
    if (count > MAX_CELLS) return "voxel grid exceeds memory budget; increase --voxel";
    s->count = (int)count;
    s->cells = io_alloc(count*sizeof(Cell));
    return s->cells ? 0 : "grid allocation failed";
}
void finish_fusion(Scan *s)
{
    for (int i=0; i<s->count; i++) {
        Cell *c=&s->cells[i];
        if (c->weight) { c->p=mul(c->p,1/c->weight); c->n=unit(c->n); }
    }
    if (s->clipped) warn(s,"scan clipped to 8 x 8 x 4 metre grid");
    if (s->frames < FEW_FRAMES) warn(s,"few frames; scan more viewpoints");
    if (s->accepted < s->rejected) warn(s,"many triangles rejected; check depth quality");
}
