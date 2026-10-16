/* SPDX-License-Identifier: GPL-3.0-only */
#include "scan.h"

typedef struct { char data[65536]; usize n; int ok; } Text;
static void text(Text *t, const char *p)
{
    while(*p) {
        if(t->n>=sizeof(t->data)) { t->ok=0;return; }
        t->data[t->n++]=*p++;
    }
}
static void integer(Text *t, u64 value)
{
    char rev[24];int n=0;
    do { rev[n++]=(char)('0'+value%10);value/=10; } while(value);
    while(n) { char a[2]={rev[--n],0};text(t,a); }
}
static void decimal(Text *t, float value)
{
    if(value<0) { text(t,"-");value=-value; }
    u64 scaled=(u64)(value*10000.f+.5f);
    integer(t,scaled/10000);text(t,".");
    u64 fraction=scaled%10000;
    if(fraction<1000) text(t,"0");
    if(fraction<100) text(t,"0");
    if(fraction<10) text(t,"0");
    integer(t,fraction);
}
static void space_number(Text *t, float n) { text(t," ");decimal(t,n); }
static int uuid(Text *t)
{
    u8 bytes[16];if(!io_random(bytes,sizeof(bytes))) return 0;
    bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
    const char *hex="0123456789abcdef";
    for(int i=0;i<16;i++) { char pair[3]={hex[bytes[i]>>4],hex[bytes[i]&15],0};text(t,pair); }
    return 1;
}
static int record(Text *t, const char *name)
{
    text(t,name);text(t," ");return uuid(t);
}
static int path(char *out, const char *dir, const char *name)
{
    usize n=0;
    while(*dir) { if(n>=3900) return 0;out[n++]=*dir++; }
    if(n && out[n-1]!='/') out[n++]='/';
    while(*name) { if(n>=4095) return 0;out[n++]=*name++; }
    out[n]=0;return 1;
}
static void le32(u8 *out,u32 v) { for(int i=0;i<4;i++) out[i]=(u8)(v>>(8*i)); }
static const char *write_files(Scan *s)
{
    Text t={.ok=1};text(&t,"frameport-room 1\nuniverse ");integer(&t,s->opt.universe);
    text(&t,"\nsource scan\ntime ");integer(&t,(u64)(io_time(0)/1000000000));text(&t,"\n");
    if(!record(&t,"room")) return "getrandom failed";
    text(&t,"\n");if(!record(&t,"floor")) return "getrandom failed";
    space_number(&t,s->floor);text(&t,"\n");if(!record(&t,"ceiling")) return "getrandom failed";
    space_number(&t,s->ceiling);text(&t,"\noutline ");integer(&t,(u64)s->noutline);
    for(int i=0;i<s->noutline;i++) { space_number(&t,s->outline[i].x);space_number(&t,s->outline[i].z); }
    text(&t,"\n");
    for(int i=0;i<s->noutline;i++) {
        Pt a=s->outline[i],b=s->outline[(i+1)%s->noutline];
        if(!record(&t,"wall")) return "getrandom failed";
        space_number(&t,a.x);space_number(&t,a.z);space_number(&t,b.x);space_number(&t,b.z);
        space_number(&t,s->floor);space_number(&t,s->ceiling);
        if(s->invisible[i]) text(&t," INVISIBLE_WALL_FACE");
        text(&t,"\n");
    }
    for(int i=0;i<s->nvolumes;i++) {
        Volume *v=&s->volumes[i];
        if(!record(&t,"volume")) return "getrandom failed";
        text(&t," ");text(&t,v->label);space_number(&t,v->p.x);space_number(&t,v->p.z);
        space_number(&t,v->p.y);space_number(&t,v->yaw);space_number(&t,v->w);space_number(&t,v->d);space_number(&t,v->h);
        text(&t,"\n");
    }
    if(!record(&t,"mesh")) return "getrandom failed";
    text(&t," room-mesh.bin ");integer(&t,(u64)s->nv);text(&t," ");integer(&t,(u64)s->ni);text(&t,"\n");
    if(!t.ok) return "room text exceeds output budget";
    Text rname={.ok=1},mname={.ok=1};
    text(&rname,"room.txt.");text(&mname,"room-mesh.bin.");
    if(!uuid(&rname) || !uuid(&mname)) return "getrandom failed";
    text(&rname,".tmp");text(&mname,".tmp");rname.data[rname.n]=0;mname.data[mname.n]=0;
    char room[4096],mesh[4096],rtmp[4096],mtmp[4096];
    if(!path(room,s->opt.out,"room.txt") || !path(mesh,s->opt.out,"room-mesh.bin") ||
       !path(rtmp,s->opt.out,rname.data) || !path(mtmp,s->opt.out,mname.data)) return "output path too long";
    if(!io_mkdir(s->opt.out)) return "cannot create output directory";
    int fd=io_open(rtmp,1),ok;
    if(fd<0) return "cannot open room temporary file";
    ok=io_write(fd,t.data,t.n);if(io_close(fd)) ok=0;
    if(!ok) { io_remove(rtmp);return "cannot write room temporary file"; }
    fd=io_open(mtmp,1);
    if(fd<0) { io_remove(rtmp);return "cannot open mesh temporary file"; }
    u8 header[16]={'F','P','R','M'};le32(header+4,1);le32(header+8,(u32)s->nv);le32(header+12,(u32)s->ni);
    ok=io_write(fd,header,16);
    /* Both supported targets are little-endian IEEE float32; parser always decodes explicitly. */
    ok=ok && io_write(fd,s->vertices,(usize)s->nv*sizeof(Vec));
    ok=ok && io_write(fd,s->indices,(usize)s->ni*sizeof(u32));
    if(io_close(fd)) ok=0;
    if(!ok) { io_remove(rtmp);io_remove(mtmp);return "cannot write mesh temporary file"; }
    if(io_rename(mtmp,mesh)) { io_remove(rtmp);io_remove(mtmp);return "cannot publish mesh"; }
    if(io_rename(rtmp,room)) { io_remove(rtmp);return "cannot publish room"; }
    return 0;
}
static int error_json(const char *error)
{
    Text t={.ok=1};text(&t,"{\"ok\":false,\"error\":\"");text(&t,error);text(&t,"\"}\n");
    io_write(1,t.data,t.n);return 2;
}
static int parse_float(const char *p,float *out)
{
    int digits=0;float n=0,scale=1;
    while(*p>='0' && *p<='9') { n=n*10+*p++-'0';digits++;if(n>10000) return 0; }
    if(*p=='.') { p++;while(*p>='0' && *p<='9') { scale*=.1f;n+=(*p++-'0')*scale;digits++; } }
    if(!digits || *p || !finite(n)) return 0;
    *out=n;return 1;
}
static int parse_universe(const char *p,u64 *out)
{
    u64 n=0;if(!*p) return 0;
    for(;*p;p++) {
        if(*p<'0' || *p>'9') return 0;
        unsigned digit=(unsigned)(*p-'0');
        if(n>(~(u64)0-digit)/10) return 0;
        n=n*10+digit;
    }
    *out=n;return 1;
}
static const char *options(Options *o,int argc,char **argv)
{
    *o=(Options){.voxel=.05f,.ceiling_default=2.5f,.truncation=3,.max_edge=.6f,.grazing=.12f,.range=4.f,
        .depth_jump=.25f,.depth_ratio=.12f,
        .plane_distance=.045f,.min_plane_area=.45f,.wall_height=1.5f,.snap_degrees=5,.floor_band=.35f,
        .ceiling_min=1.8f,.min_volume=.05f,.cluster_gap=.10f,.gap=.65f,.horizontal=.85f};
    for(int i=1;i<argc;i++) {
        const char *arg=argv[i];
        if(same(arg,"--json")) continue;
        if(i+1==argc) return "option requires a value";
        const char *value=argv[++i];
        if(same(arg,"--offline")) o->input=value;
        else if(same(arg,"--out")) o->out=value;
        else if(same(arg,"--hint")) o->hint=value;
        else if(same(arg,"--universe")) { if(!parse_universe(value,&o->universe)) return "invalid universe"; }
        else {
            float *dest=0,low=0,high=100;
            if(same(arg,"--voxel")) { dest=&o->voxel;low=.04f;high=.2f; }
            else if(same(arg,"--ceiling-default")) { dest=&o->ceiling_default;low=1.8f;high=3.8f; }
            else if(same(arg,"--truncation")) { dest=&o->truncation;low=1;high=5; }
            else if(same(arg,"--max-edge")) { dest=&o->max_edge;low=.05f;high=1.5f; }
            else if(same(arg,"--grazing")) { dest=&o->grazing;low=.01f;high=.9f; }
            else if(same(arg,"--range")) { dest=&o->range;low=1;high=10; }
            else if(same(arg,"--depth-jump")) { dest=&o->depth_jump;low=.02f;high=1; }
            else if(same(arg,"--depth-ratio")) { dest=&o->depth_ratio;high=.5f; }
            else if(same(arg,"--plane-distance")) { dest=&o->plane_distance;low=.01f;high=.15f; }
            else if(same(arg,"--min-plane-area")) { dest=&o->min_plane_area;low=.1f;high=4; }
            else if(same(arg,"--wall-height")) { dest=&o->wall_height;low=.5f;high=3; }
            else if(same(arg,"--snap-degrees")) { dest=&o->snap_degrees;high=15; }
            else if(same(arg,"--floor-band")) { dest=&o->floor_band;low=.05f;high=1; }
            else if(same(arg,"--ceiling-min")) { dest=&o->ceiling_min;low=1.5f;high=3; }
            else if(same(arg,"--min-volume")) { dest=&o->min_volume;low=.005f;high=1; }
            else if(same(arg,"--cluster-gap")) { dest=&o->cluster_gap;low=.04f;high=.2f; }
            else if(same(arg,"--gap")) { dest=&o->gap;low=.1f;high=1.5f; }
            else if(same(arg,"--horizontal")) { dest=&o->horizontal;low=.6f;high=.99f; }
            else return "unknown option";
            if(!parse_float(value,dest) || *dest<low || *dest>high) return "option outside supported range";
        }
    }
    return o->input && o->out ? 0 : "required: --offline dump.fprs --out directory";
}
static void duration(Text *t,i64 a,i64 b) { decimal(t,(float)(b-a)/1000000); }
static void summary(Scan *s)
{
    Text t={.ok=1};text(&t,"{\"ok\":true,\"frames\":");integer(&t,s->frames);
    text(&t,",\"skipped_frames\":");integer(&t,s->skipped_frames);
    text(&t,",\"far_vertices\":");integer(&t,s->far_vertices);
    text(&t,",\"discontinuity_triangles\":");integer(&t,s->discontinuities);
    text(&t,",\"walls\":");integer(&t,(u64)s->noutline);
    text(&t,",\"detected_walls\":");integer(&t,(u64)s->nplanes);
    text(&t,",\"volumes\":");integer(&t,(u64)s->nvolumes);
    text(&t,",\"floor_y\":");decimal(&t,s->floor);text(&t,",\"ceiling_y\":");decimal(&t,s->ceiling);
    text(&t,",\"coverage_percent\":");decimal(&t,s->coverage);
    text(&t,",\"hint_used\":");text(&t,s->hint_used ? "true" : "false");
    text(&t,",\"triangles\":");integer(&t,(u64)s->ni/3);
    text(&t,",\"accepted_triangles\":");integer(&t,s->accepted);
    text(&t,",\"rejected_triangles\":");integer(&t,s->rejected);
    text(&t,",\"warnings\":[");
    for(int i=0;i<s->nwarnings;i++) { if(i) text(&t,",");text(&t,"\"");text(&t,s->warnings[i]);text(&t,"\""); }
    text(&t,"],\"timings_ms\":{\"read\":");duration(&t,s->t0,s->tread);
    text(&t,",\"fusion\":");duration(&t,s->tread,s->tfuse);
    text(&t,",\"model\":");duration(&t,s->tfuse,s->tmodel);
    text(&t,",\"mesh\":");duration(&t,s->tmodel,s->tmesh);
    text(&t,",\"total\":");duration(&t,s->t0,io_time(1));text(&t,"}}\n");io_write(1,t.data,t.n);
}
int main(int argc,char **argv)
{
    if(argc==2 && same(argv[1],"--help")) {
        const char usage[]="fp_roomscan --offline dump.fprs --out directory [--universe N] [--voxel 0.05]\n"
            "  [--ceiling-default 2.5] [--hint outline.txt] [--json]\n"
            "Tuning: --truncation --max-edge --grazing --range --depth-jump --depth-ratio --plane-distance --min-plane-area\n"
            "  --wall-height --snap-degrees --floor-band --ceiling-min --min-volume --cluster-gap --gap --horizontal\n"
            "Always prints one JSON summary; errors exit 2. See docs/mr/scan.md for units and ranges.\n";
        io_write(1,usage,sizeof(usage)-1);return 0;
    }
    Scan *s=io_alloc(sizeof(Scan));
    if(!s) return error_json("scan allocation failed");
    s->rng=0x714ce3u;s->t0=io_time(1);
    const char *error=options(&s->opt,argc,argv);
    if(!error) error=read_dump(s,0);
    s->tread=io_time(1);
    if(!error) error=init_grid(s);
    if(!error) error=read_dump(s,1);
    if(!error) finish_fusion(s);
    s->tfuse=io_time(1);
    if(!error) error=make_model(s);
    s->tmodel=io_time(1);
    if(!error) error=make_mesh(s);
    s->tmesh=io_time(1);
    if(!error) error=write_files(s);
    int result=0;
    if(error) result=error_json(error);else summary(s);
    io_free(s->cells,(usize)s->count*sizeof(Cell));
    io_free(s->vertices,(usize)MAX_MESH_VERTICES*sizeof(Vec));
    io_free(s->indices,(usize)MAX_TRIANGLES*3*sizeof(u32));
    io_free(s,sizeof(Scan));return result;
}
