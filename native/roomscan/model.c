/* SPDX-License-Identifier: GPL-3.0-only */
#include "scan.h"

#define RANSAC_TRIALS 96
#define NORMAL_AGREEMENT .90f
#define MIN_WALL_SPAN .60f
#define FURNITURE_MARGIN .12f
#define NEAR_WALL .35f
#define MIN_TOP_AREA .25f
#define BOX_TRIM .02f
#define VERTICAL_NORMAL_Y .35f
#define BOX_SIDE_NORMAL_Y .30f
#define BOX_EDGE_STRENGTH .40f
#define MIN_CLUSTER_SURFELS 12
#define MIN_BOX_EDGE_VOTES 12
#define MIN_OUTLINE_EXTENT 1.f
#define MIN_OUTLINE_TWICE_AREA .5f
#define FLOOR_INFER_DISTANCE .3f
#define TABLE_MIN_HEIGHT .55f
#define TABLE_MAX_HEIGHT 1.1f
#define COUCH_MIN_HEIGHT .35f
#define COUCH_MAX_HEIGHT .9f
#define COUCH_MIN_LENGTH 1.4f
#define BED_MIN_HEIGHT .3f
#define BED_MAX_HEIGHT .8f
#define BED_MIN_AREA 2.f
#define STORAGE_MIN_HEIGHT 1.1f
#define SCREEN_MAX_DEPTH .22f
#define SCREEN_MIN_WIDTH .4f
#define SCREEN_MAX_WIDTH 2.f
#define DEFAULT_CIRCLE_MIN_POINTS 8
#define DEFAULT_CIRCLE_RADIAL_CV .05f
#define LOW_COVERAGE_PERCENT 80.f
#define WALL_MERGE_DISTANCE .25f
#define WALL_MERGE_COSINE .97f
#define RELATIVE_WALL_AREA .20f
#define DOMINANT_WALL_TOLERANCE 12.f
#define ORTHOGONAL_SUPPORT_FRACTION .65f
#define CEILING_MIN_AREA .08f    /* m^2 of fused down-facing cells at one height (fused surfaces are sparse: the
                                  * owner's floor was 0.7 m^2 of cells, the ceiling 0.11 m^2 after a 2 s look up) */
#define CEILING_MIN_HEIGHT 2.0f  /* m above the floor: lower ones are shelves, door frames, table undersides */
#define CEILING_MAX_HEIGHT 4.0f
#define BOX_AXIS_COS .96f        /* walls within ~16 deg of the two room axes (smeared depth tilted a real
                                  * wall 12 deg in the owner's scan) */
#define BOX_SNAP .5f             /* m: a rectangle side snaps to a detected wall this close */
#define BOX_LOW_SHARE .02f       /* surfaces beyond the 2nd/98th percentile along an axis are outliers */
#define WALL_BAND_TOP 2.4f       /* m above the floor: walls are fitted below this (and below the ceiling) */
#define MIN_FURNITURE_TOP .25f
#define WALL_FURNITURE_CLEARANCE .15f
#define MIN_TOP_DENSITY .30f

static void consolidate_walls(Scan *s)
{
    if(!s->nplanes) return;
    Plane result[MAX_PLANES]={0};int count=0;
    float largest=0;
    for(int i=0;i<s->nplanes;i++) {
        Plane p=s->planes[i];int merge=-1;
        largest=mx(largest,p.area);
        for(int j=0;j<count;j++) if(dot(p.n,result[j].n)>WALL_MERGE_COSINE &&
            ab(p.d-result[j].d)<WALL_MERGE_DISTANCE) { merge=j;break; }
        if(merge<0) result[count++]=p;
        else {
            Plane *r=&result[merge];
            r->area+=p.area;r->bottom=mn(r->bottom,p.bottom);r->top=mx(r->top,p.top);
            r->lo=mn(r->lo,p.lo);r->hi=mx(r->hi,p.hi);
        }
    }
    float base=angle(result[0].n.z,result[0].n.x),aligned=0,total=0;
    for(int i=0;i<count;i++) {
        Plane *p=&result[i];float a=angle(p->n.z,p->n.x)-base;
        float delta=ab(a-flo(a/(PI/2)+.5f)*(PI/2));
        total+=p->area;
        if(delta<DOMINANT_WALL_TOLERANCE*PI/180) aligned+=p->area;
    }
    int orthogonal=aligned>total*ORTHOGONAL_SUPPORT_FRACTION && s->opt.snap_degrees>0;
    s->nplanes=0;
    for(int i=0;i<count;i++) {
        Plane p=result[i];float a=angle(p.n.z,p.n.x)-base;
        float delta=ab(a-flo(a/(PI/2)+.5f)*(PI/2));
        if(p.area<largest*RELATIVE_WALL_AREA) continue;
        if(orthogonal && delta>DOMINANT_WALL_TOLERANCE*PI/180) continue;
        s->planes[s->nplanes++]=p;
    }
}

static float plane_height(Scan *s, int ceiling, int *found)
{
    float hist[512]={0}, step=s->opt.voxel*.5f;
    int bins=(int)(4/step)+2;
    if (bins>512) bins=512;
    for (int i=0;i<s->count;i++) {
        Cell *c=&s->cells[i];
        if (!c->weight || (ceiling ? c->n.y > -s->opt.horizontal : c->n.y < s->opt.horizontal)) continue;
        if (ceiling ? c->p.y<s->opt.ceiling_min : ab(c->p.y)>s->opt.floor_band) continue;
        int b=flo((c->p.y-s->origin.y)/step);
        if (b>=0 && b<bins) hist[b]+=1;
    }
    int best=0;
    float score=0;
    for (int b=1;b<bins-1;b++) {
        float h=hist[b-1]+hist[b]+hist[b+1];
        if (h>score) { score=h; best=b; }
    }
    float y=s->origin.y+(best+.5f)*step, total=0, sum=0,upward=0;
    for (int i=0;i<s->count;i++) {
        Cell *c=&s->cells[i];
        if(c->weight && c->n.y>s->opt.horizontal) upward++;
        if (!c->weight || ab(c->p.y-y)>s->opt.plane_distance) continue;
        if (ceiling ? c->n.y > -s->opt.horizontal : c->n.y < s->opt.horizontal) continue;
        total++; sum+=c->p.y;
    }
    *found=(ceiling || score* s->opt.voxel*s->opt.voxel >= s->opt.min_plane_area) && total>0;
    /* A ceiling is judged on its own area and height, not against the floor: players glance up for a second and look
     * at the floor all the time, so a share-of-upward-surfaces rule rejected every real ceiling (owner's scans,
     * 2026-10-16: down-facing surfaces at ~2.9 m in both recordings, "ceiling not seen"). */
    (void)upward;
    if(ceiling && (total*s->opt.voxel*s->opt.voxel<CEILING_MIN_AREA || y-s->floor<CEILING_MIN_HEIGHT ||
                   y-s->floor>CEILING_MAX_HEIGHT)) *found=0;
    return *found ? sum/total : (ceiling ? s->floor+s->opt.ceiling_default : 0);
}
static int on_plane(Scan *s, Cell *c, Plane *p)
{
    return ab(dot(c->p,p->n)-p->d)<s->opt.plane_distance && dot(c->n,p->n)>NORMAL_AGREEMENT;
}
static void refine(Scan *s, int *ids, int n, Plane *p)
{
    Vec avg=vec(0,0,0), normal=vec(0,0,0);
    float count=0;
    for (int i=0;i<n;i++) {
        Cell *c=&s->cells[ids[i]];
        if (!c->mark && on_plane(s,c,p)) { avg=add(avg,c->p); normal=add(normal,c->n); count++; }
    }
    if (!count) return;
    avg=mul(avg,1/count);
    /* Fit positions, rather than averaging noisy per-triangle normals. */
    float xx=0,zz=0,xz=0;
    for (int i=0;i<n;i++) {
        Cell *c=&s->cells[ids[i]];
        if (!c->mark && on_plane(s,c,p)) {
            float x=c->p.x-avg.x,z=c->p.z-avg.z;
            xx+=x*x; zz+=z*z; xz+=x*z;
        }
    }
    float yaw=.5f*angle(2*xz,xx-zz);
    Vec nn=vec(-sine(yaw),0,cosine(yaw));
    if (dot(nn,normal)<0) nn=mul(nn,-1);
    p->n=nn; p->d=dot(nn,avg);
    p->lo=1e10f; p->hi=-1e10f; p->bottom=1e10f; p->top=-1e10f;
    count=0;
    for (int i=0;i<n;i++) {
        Cell *c=&s->cells[ids[i]];
        if (!c->mark && on_plane(s,c,p)) {
            float t=c->p.x*p->n.z-c->p.z*p->n.x;
            p->lo=mn(p->lo,t); p->hi=mx(p->hi,t);
            p->bottom=mn(p->bottom,c->p.y); p->top=mx(p->top,c->p.y); count++;
        }
    }
    p->area=count*s->opt.voxel*s->opt.voxel;
}
static void walls(Scan *s, int *ids, int n)
{
    /* Walls are fitted in a fixed band above the floor whatever the ceiling: Valve's smoothed depth bends where wall
     * meets ceiling, and with a detected 2.9 m ceiling that band confused the fit (owner's scan, 2026-10-16). */
    float top=mn(s->ceiling,s->floor+WALL_BAND_TOP)-FURNITURE_MARGIN;
    for (int i=0;i<n;i++) {
        Cell *c=&s->cells[ids[i]];
        c->mark=ab(c->n.y)>VERTICAL_NORMAL_Y || c->p.y<s->floor+FURNITURE_MARGIN || c->p.y>top;
    }
    for (int attempt=0;attempt<MAX_PLANES*2 && s->nplanes<MAX_PLANES;attempt++) {
        Plane best={0}; int best_count=0;
        for (int trial=0;trial<RANSAC_TRIALS;trial++) {
            Cell *seed=&s->cells[ids[random_next(s)%(unsigned)n]];
            if (seed->mark) continue;
            Plane p={0}; p.n=unit(vec(seed->n.x,0,seed->n.z)); p.d=dot(seed->p,p.n);
            int count=0;
            for (int j=0;j<n;j++) {
                Cell *c=&s->cells[ids[j]];
                if (!c->mark && on_plane(s,c,&p)) count++;
            }
            if (count>best_count) { best_count=count; best=p; }
        }
        if (best_count*s->opt.voxel*s->opt.voxel<s->opt.min_plane_area) break;
        refine(s,ids,n,&best);
        /* Height separates large furniture faces from room walls. */
        int wall=best.area>=s->opt.min_plane_area && best.top-best.bottom>=s->opt.wall_height &&
                 best.hi-best.lo>=MIN_WALL_SPAN;
        for (int j=0;j<n;j++) {
            Cell *c=&s->cells[ids[j]];
            if (!c->mark && on_plane(s,c,&best)) c->mark=wall ? 2 : 3;
        }
        if (wall) s->planes[s->nplanes++]=best;
        /* Rejected candidates are consumed too, ensuring bounded progress. */
    }
    if (s->nplanes==MAX_PLANES) warn(s,"wall limit reached");
    if (s->nplanes) {
        float base=angle(s->planes[0].n.z,s->planes[0].n.x);
        for (int k=0;k<s->nplanes;k++) {
            Plane *p=&s->planes[k];
            float a=angle(p->n.z,p->n.x), diff=a-base;
            int quarter=flo(diff/(PI/2)+.5f);
            float target=base+quarter*(PI/2);
            if (ab(a-target)<s->opt.snap_degrees*PI/180) {
                Vec nn=vec(cosine(target),0,sine(target));
                float sum=0,count=0,lo=1e10f,hi=-1e10f;
                for (int j=0;j<n;j++) {
                    Cell *c=&s->cells[ids[j]];
                    if (on_plane(s,c,p)) {
                        sum+=dot(c->p,nn); count++;
                        float t=c->p.x*nn.z-c->p.z*nn.x;
                        lo=mn(lo,t); hi=mx(hi,t);
                    }
                }
                if (count) { p->n=nn; p->d=sum/count; p->lo=lo; p->hi=hi; }
            }
        }
    }
    consolidate_walls(s);
}
static Pt endpoint(Plane *p, int end)
{
    float t=end ? p->hi : p->lo;
    return (Pt){p->n.x*p->d+p->n.z*t,p->n.z*p->d-p->n.x*t};
}
static float distance2(Pt a, Pt b) { float x=a.x-b.x,z=a.z-b.z; return x*x+z*z; }
static int intersect(Plane *a, Plane *b, Pt *p)
{
    float det=a->n.x*b->n.z-a->n.z*b->n.x;
    if (ab(det)<.1f) return 0;
    p->x=(a->d*b->n.z-a->n.z*b->d)/det;
    p->z=(a->n.x*b->d-a->d*b->n.x)/det;
    return 1;
}
static void push_outline(Scan *s, Pt p, int invisible)
{
    if (s->noutline && distance2(s->outline[s->noutline-1],p)<.0001f) return;
    if (s->noutline<MAX_OUTLINE) {
        s->outline[s->noutline]=p;
        s->invisible[s->noutline++]=invisible;
    }
}
static void rectangle(Scan *s, int *ids, int n)
{
    float x0=1e10f,x1=-1e10f,z0=1e10f,z1=-1e10f;
    for (int i=0;i<n;i++) {
        Vec p=s->cells[ids[i]].p;
        x0=mn(x0,p.x);x1=mx(x1,p.x);z0=mn(z0,p.z);z1=mx(z1,p.z);
    }
    if (x1-x0<MIN_OUTLINE_EXTENT) { float m=(x0+x1)*.5f; x0=m-MIN_OUTLINE_EXTENT/2; x1=m+MIN_OUTLINE_EXTENT/2; }
    if (z1-z0<MIN_OUTLINE_EXTENT) { float m=(z0+z1)*.5f; z0=m-MIN_OUTLINE_EXTENT/2; z1=m+MIN_OUTLINE_EXTENT/2; }
    push_outline(s,(Pt){x0,z0},1);push_outline(s,(Pt){x1,z0},1);
    push_outline(s,(Pt){x1,z1},1);push_outline(s,(Pt){x0,z1},1);
    warn(s,"incomplete walls; synthesized outline");
}
static int segment_cross(Pt a, Pt b, Pt c, Pt d)
{
    float p=(b.x-a.x)*(c.z-a.z)-(b.z-a.z)*(c.x-a.x);
    float q=(b.x-a.x)*(d.z-a.z)-(b.z-a.z)*(d.x-a.x);
    float r=(d.x-c.x)*(a.z-c.z)-(d.z-c.z)*(a.x-c.x);
    float t=(d.x-c.x)*(b.z-c.z)-(d.z-c.z)*(b.x-c.x);
    return p*q<-.000001f && r*t<-.000001f;
}
static int valid_polygon(Scan *s)
{
    float area=0;
    for (int i=0;i<s->noutline;i++) {
        Pt a=s->outline[i], b=s->outline[(i+1)%s->noutline];
        area+=a.x*b.z-b.x*a.z;
        for (int j=i+2;j<s->noutline;j++) {
            if ((j+1)%s->noutline==i) continue;
            if (segment_cross(a,b,s->outline[j],s->outline[(j+1)%s->noutline])) return 0;
        }
    }
    return s->noutline>=3 && area>MIN_OUTLINE_TWICE_AREA;
}
static int convex_fallback(Scan *s)
{
    if(s->nplanes<3 || s->nplanes>4) return 0;
    /* A concave multi-wall room must use directed segments. A small set of enclosure planes can instead be
     * clipped as inward half-planes when smeared endpoints prevent reliable corner ordering. */
    Pt a[MAX_OUTLINE],b[MAX_OUTLINE];float x0=1e10f,x1=-1e10f,z0=1e10f,z1=-1e10f;
    for(int i=0;i<s->nplanes;i++) for(int end=0;end<2;end++) {
        Pt p=endpoint(&s->planes[i],end);x0=mn(x0,p.x);x1=mx(x1,p.x);z0=mn(z0,p.z);z1=mx(z1,p.z);
    }
    int n=4;a[0]=(Pt){x0,z0};a[1]=(Pt){x1,z0};a[2]=(Pt){x1,z1};a[3]=(Pt){x0,z1};
    for(int i=0;i<s->nplanes;i++) {
        Plane *p=&s->planes[i];int count=0;
        for(int j=0;j<n;j++) {
            Pt u=a[j],v=a[(j+1)%n];float du=p->n.x*u.x+p->n.z*u.z-p->d,dv=p->n.x*v.x+p->n.z*v.z-p->d;
            if(du>=0) b[count++]=u;
            if((du>=0)!=(dv>=0)) {
                float t=du/(du-dv);b[count++]=(Pt){u.x+t*(v.x-u.x),u.z+t*(v.z-u.z)};
            }
        }
        if(count<3 || count>=MAX_OUTLINE) return 0;
        n=count;memcpy(a,b,(usize)n*sizeof(Pt));
    }
    s->noutline=n;
    for(int j=0;j<n;j++) {
        s->outline[j]=a[j];s->invisible[j]=1;Pt v=a[(j+1)%n];
        for(int i=0;i<s->nplanes;i++) {
            Plane *p=&s->planes[i];
            if(ab(p->n.x*a[j].x+p->n.z*a[j].z-p->d)<s->opt.plane_distance &&
               ab(p->n.x*v.x+p->n.z*v.z-p->d)<s->opt.plane_distance) s->invisible[j]=0;
        }
    }
    if(!valid_polygon(s)) { s->noutline=0;return 0; }
    warn(s,"wall endpoints uncertain; using plane intersections");return 1;
}
/* Value below which `share` of the projections fall (2 cm bins over +-8 m around the data). */
static float projection_percentile(Scan *s, int *ids, int n, Pt axis, float share)
{
    static u32 hist[800];
    memset(hist,0,sizeof(hist));
    for (int i=0;i<n;i++) {
        Vec p=s->cells[ids[i]].p;
        int b=(int)((p.x*axis.x+p.z*axis.z+8)/.02f);
        if (b>=0 && b<800) hist[b]++;
    }
    u64 total=0,run=0;
    for (int b=0;b<800;b++) total+=hist[b];
    for (int b=0;b<800;b++) { run+=hist[b]; if (run>=share*total) return b*.02f-8+.01f; }
    return 8;
}
/* Most rooms are rectangles. When every detected wall runs along one of two perpendicular axes (2-4 walls), the
 * outline is that rectangle: its sides at the 2nd/98th percentile of the surfaces along each axis, each snapped to a
 * detected wall when one lies within BOX_SNAP. Directed wall chaining made a skewed quadrilateral of the owner's
 * room from three smeared walls (2026-10-16). L-shaped rooms have more walls and keep the chaining below. */
static int orthogonal_box(Scan *s, int *ids, int n)
{
    if (s->nplanes<2 || s->nplanes>4) return 0;
    Vec u=s->planes[0].n; float len=root(u.x*u.x+u.z*u.z);
    if (len<.5f) return 0;
    Pt axis[2]={{u.x/len,u.z/len},{-u.z/len,u.x/len}};
    float side[2][2]; int wall[2][2]={{0,0},{0,0}}, snapped=0;
    for (int k=0;k<2;k++) {
        side[k][0]=projection_percentile(s,ids,n,axis[k],BOX_LOW_SHARE);
        side[k][1]=projection_percentile(s,ids,n,axis[k],1-BOX_LOW_SHARE);
    }
    for (int i=0;i<s->nplanes;i++) {
        Plane *p=&s->planes[i];
        float c0=p->n.x*axis[0].x+p->n.z*axis[0].z, c1=p->n.x*axis[1].x+p->n.z*axis[1].z;
        int k=ab(c0)>=BOX_AXIS_COS ? 0 : ab(c1)>=BOX_AXIS_COS ? 1 : -1;
        if (k<0) return 0;  // a wall off both axes: not a rectangle
        float offset=p->d*(k ? c1 : c0)/ab(k ? c1 : c0);
        int end=ab(offset-side[k][0])<ab(offset-side[k][1]) ? 0 : 1;
        if (ab(offset-side[k][end])<BOX_SNAP) { side[k][end]=offset; snapped+=!wall[k][end]; wall[k][end]=1; }
    }
    if (snapped<2 || side[0][1]-side[0][0]<MIN_OUTLINE_EXTENT || side[1][1]-side[1][0]<MIN_OUTLINE_EXTENT) return 0;
    /* corners (s,t) -> world x = a0.x s + a1.x t, z = a0.z s + a1.z t; edge flags = whether its side is a wall */
    int order[4][2]={{0,0},{1,0},{1,1},{0,1}}, edge[4][2]={{1,0},{0,1},{1,1},{0,0}};  // edge k lies on side[a][e]
    float twice=0;
    Pt c[4];
    for (int j=0;j<4;j++) {
        float a=side[0][order[j][0]], b=side[1][order[j][1]];
        c[j]=(Pt){axis[0].x*a+axis[1].x*b, axis[0].z*a+axis[1].z*b};
    }
    for (int j=0;j<4;j++) twice+=c[j].x*c[(j+1)%4].z-c[(j+1)%4].x*c[j].z;
    s->noutline=0;
    if (twice>0) for (int j=0;j<4;j++) push_outline(s,c[j],!wall[edge[j][0]][edge[j][1]]);
    else for (int j=0;j<4;j++) {  // reversed: corner 0,3,2,1; edge from corner k to k-1 is the old edge k-1
        int k=(4-j)%4, e=(k+3)%4;
        push_outline(s,c[k],!wall[edge[e][0]][edge[e][1]]);
    }
    if (!valid_polygon(s)) { s->noutline=0; return 0; }
    if (snapped<4) warn(s,"gaps closed with invisible walls");
    return 1;
}
static void outline(Scan *s, int *ids, int n)
{
    if (orthogonal_box(s,ids,n)) return;
    if (s->nplanes<3) { rectangle(s,ids,n); return; }
    int order[MAX_PLANES]={0};
    s->planes[0].used=1;
    for (int k=1;k<s->nplanes;k++) {
        Pt end=endpoint(&s->planes[order[k-1]],1);
        float best=1e10f; int which=-1;
        for (int j=0;j<s->nplanes;j++) if (!s->planes[j].used) {
            float d=distance2(end,endpoint(&s->planes[j],0));
            if (d<best) { best=d;which=j; }
        }
        order[k]=which;s->planes[which].used=1;
    }
    Pt starts[MAX_PLANES], ends[MAX_PLANES];
    for (int k=0;k<s->nplanes;k++) {
        Plane *p=&s->planes[order[k]], *next=&s->planes[order[(k+1)%s->nplanes]];
        ends[k]=endpoint(p,1); starts[(k+1)%s->nplanes]=endpoint(next,0);
        Pt corner;
        if (intersect(p,next,&corner) && distance2(corner,ends[k])<s->opt.gap*s->opt.gap &&
            distance2(corner,starts[(k+1)%s->nplanes])<s->opt.gap*s->opt.gap)
            ends[k]=starts[(k+1)%s->nplanes]=corner;
    }
    for (int k=0;k<s->nplanes;k++) {
        push_outline(s,starts[k],0);
        if (distance2(ends[k],starts[(k+1)%s->nplanes])>.0001f) {
            push_outline(s,ends[k],1);warn(s,"gaps closed with invisible walls");
        }
    }
    if (!valid_polygon(s)) {
        s->noutline=0;
        if(!convex_fallback(s)) { rectangle(s,ids,n);warn(s,"wall ordering ambiguous"); }
    }
}
static float wall_distance(Scan *s, Vec p)
{
    float best=1e10f;
    for (int i=0;i<s->nplanes;i++) best=mn(best,ab(dot(s->planes[i].n,p)-s->planes[i].d));
    return best;
}
static void trimmed_bounds(Scan *s, int *queue, int n, Vec axis, float *lo, float *hi)
{
    int hist[512]={0};float start=*lo,range=*hi-start;
    if(range<.001f) return;
    for(int i=0;i<n;i++) {
        float value=dot(s->cells[queue[i]].p,axis);
        int bin=(int)clamp((value-start)/range*511,0,511);hist[bin]++;
    }
    int count=0,target=(int)(n*BOX_TRIM);
    for(int b=0;b<512;b++) { count+=hist[b];if(count>target) { *lo=start+range*b/511;break; } }
    count=0;
    for(int b=511;b>=0;b--) { count+=hist[b];if(count>target) { *hi=start+range*(b+1)/511;break; } }
}
static float top_height(Scan *s, int *queue, int n, float low, float high)
{
    int histogram[512]={0},bins=(int)mn((high-low)/(s->opt.voxel*.25f)+2,512);
    float step=s->opt.voxel*.25f;
    for(int i=0;i<n;i++) {
        int bin=flo((s->cells[queue[i]].p.y-low)/step);
        if(bin>=0 && bin<bins) histogram[bin]++;
    }
    int count=0;
    for(int bin=0;bin<bins;bin++) {
        count+=histogram[bin];
        /* The median resists a softened lip without selecting one of several noisy sub-peaks as a new top. */
        if(count>=n/2) return low+(bin+.5f)*step;
    }
    return (low+high)*.5f;
}
static void furniture(Scan *s, int *ids, int n, int *queue)
{
    /* Tops remain separable when a softened sofa/table edge blends into its neighbouring wall. Vertical-only
     * components are considered separately for screens; they cannot turn a whole wall into an OTHER volume. */
    for(int mode=0;mode<2;mode++) {
    float histogram[256]={0},levels[32];int nlevels=0;
    if(!mode) {
        for(int j=0;j<n;j++) {
            Cell *c=&s->cells[ids[j]];
            if(c->n.y>s->opt.horizontal && c->p.y>s->floor+MIN_FURNITURE_TOP &&
               c->p.y<s->ceiling-FURNITURE_MARGIN && wall_distance(s,c->p)>WALL_FURNITURE_CLEARANCE) {
                int bin=flo((c->p.y-s->floor)/s->opt.voxel);
                if(bin>=0 && bin<256) histogram[bin]++;
            }
        }
        for(int k=0;k<32;k++) {
            int best=0;float score=0;
            for(int bin=1;bin<255;bin++) {
                float count=histogram[bin-1]+histogram[bin]+histogram[bin+1];
                if(count>score) { score=count;best=bin; }
            }
            if(score*s->opt.voxel*s->opt.voxel<MIN_TOP_AREA) break;
            levels[nlevels++]=s->floor+(best+.5f)*s->opt.voxel;
            int radius=(int)(s->opt.plane_distance*2/s->opt.voxel)+1;
            for(int bin=best-radius;bin<=best+radius;bin++) if(bin>=0 && bin<256) histogram[bin]=0;
        }
    }
    for (int j=0;j<n;j++) {
        Cell *c=&s->cells[ids[j]];
        int normal=mode ? ab(c->n.y)<BOX_SIDE_NORMAL_Y : c->n.y>s->opt.horizontal;
        c->mark=(normal && c->p.y>s->floor+MIN_FURNITURE_TOP && c->p.y<s->ceiling-FURNITURE_MARGIN &&
                 wall_distance(s,c->p)>WALL_FURNITURE_CLEARANCE) ? 1 : 0;
        if(c->mark && !mode) {
            c->mark=0;float best=s->opt.plane_distance*2;
            for(int k=0;k<nlevels;k++) if(ab(c->p.y-levels[k])<best) {
                best=ab(c->p.y-levels[k]);c->mark=k+1;
            }
        }
    }
    int radius=(int)(s->opt.cluster_gap/s->opt.voxel)+1;
    for (int j=0;j<n;j++) {
        int seed=ids[j];
        if (s->cells[seed].mark<=0) continue;
        int group=s->cells[seed].mark;
        int head=0,tail=1;queue[0]=seed;s->cells[seed].mark=-1;
        while (head<tail) {
            int id=queue[head++],x=id%s->nx,z=(id/s->nx)%s->nz,y=id/(s->nx*s->nz);
            for (int dy=-radius;dy<=radius;dy++) for (int dz=-radius;dz<=radius;dz++) for (int dx=-radius;dx<=radius;dx++) {
                if (dx*dx+dy*dy+dz*dz>radius*radius) continue;
                int xx=x+dx,yy=y+dy,zz=z+dz;
                if (xx<0 || yy<0 || zz<0 || xx>=s->nx || yy>=s->ny || zz>=s->nz) continue;
                int next=(yy*s->nz+zz)*s->nx+xx;
                Cell *c=&s->cells[next];
                if (c->mark==group) { c->mark=-1;queue[tail++]=next; }
            }
        }
        if (tail<MIN_CLUSTER_SURFELS) continue;
        Vec avg=vec(0,0,0);float top=-1e10f,bottom=1e10f;
        for (int k=0;k<tail;k++) {
            Vec p=s->cells[queue[k]].p;avg=add(avg,p);top=mx(top,p.y);bottom=mn(bottom,p.y);
        }
        avg=mul(avg,1.f/tail);
        float xx=0,zz=0,xz=0,edge_x=0,edge_y=0,edge_count=0;
        for (int k=0;k<tail;k++) {
            Cell *c=&s->cells[queue[k]];
            Vec p=sub(c->p,avg);xx+=p.x*p.x;zz+=p.z*p.z;xz+=p.x*p.z;
            if(ab(c->n.y)<BOX_SIDE_NORMAL_Y) {
                Vec norm=unit(vec(c->n.x,0,c->n.z));
                float co2=norm.x*norm.x-norm.z*norm.z,si2=2*norm.x*norm.z;
                edge_x+=co2*co2-si2*si2;edge_y+=2*co2*si2;edge_count++;
            }
        }
        float yaw=.5f*angle(2*xz,xx-zz);
        /* Opposing faces and perpendicular edges vote for the same fourth harmonic. Unlike PCA, this is not
         * biased by one side being more visible. Non-box objects fall back to position covariance. */
        if(edge_count>MIN_BOX_EDGE_VOTES && root(edge_x*edge_x+edge_y*edge_y)>edge_count*BOX_EDGE_STRENGTH)
            yaw=.25f*angle(edge_y,edge_x);
        if(!mode) {
            float smallest=1e10f;
            for(int degree=0;degree<90;degree++) {
                float a=degree*PI/180,co=cosine(a),si=sine(a),u0=1e10f,u1=-1e10f,v0=1e10f,v1=-1e10f;
                for(int k=0;k<tail;k++) {
                    Vec p=s->cells[queue[k]].p;float u=p.x*co+p.z*si,v=-p.x*si+p.z*co;
                    u0=mn(u0,u);u1=mx(u1,u);v0=mn(v0,v);v1=mx(v1,v);
                }
                float area=(u1-u0)*(v1-v0);
                if(area<smallest) { smallest=area;yaw=a; }
            }
        }
        float co=cosine(yaw),si=sine(yaw);
        float u0=1e10f,u1=-1e10f,v0=1e10f,v1=-1e10f,top_count=0;
        for (int k=0;k<tail;k++) {
            Cell *c=&s->cells[queue[k]];Vec p=sub(c->p,avg);
            float u=p.x*co+p.z*si,v=-p.x*si+p.z*co;
            u0=mn(u0,u);u1=mx(u1,u);v0=mn(v0,v);v1=mx(v1,v);
            if (c->n.y>s->opt.horizontal && c->p.y>top-s->opt.plane_distance*2) top_count++;
        }
        /* Stereo discontinuities leave a low-density halo even after long-edge rejection. Trim the outer
         * two percent before measuring boxes; otherwise a table can become wide enough to look like a couch. */
        float up=dot(avg,vec(co,0,si)),vp=dot(avg,vec(-si,0,co));
        u0+=up;u1+=up;v0+=vp;v1+=vp;
        trimmed_bounds(s,queue,tail,vec(co,0,si),&u0,&u1);
        trimmed_bounds(s,queue,tail,vec(-si,0,co),&v0,&v1);
        u0-=up;u1-=up;v0-=vp;v1-=vp;
        trimmed_bounds(s,queue,tail,vec(0,1,0),&bottom,&top);
        if(!mode) top=top_height(s,queue,tail,bottom,top);
        float w=u1-u0,d=v1-v0,h=top-(bottom<s->floor+FLOOR_INFER_DISTANCE ? s->floor : mn(bottom,s->floor+FURNITURE_MARGIN));
        if(d>w) { float t=w;w=d;d=t;yaw+=PI/2; }
        while(yaw>PI/2) yaw-=PI;
        if(!mode) h=top-s->floor;
        if (w*d*h<s->opt.min_volume) continue;
        float height=top-s->floor,area=w*d,top_area=(mode ? top_count : tail)*s->opt.voxel*s->opt.voxel;
        if(!mode && (top_area<MIN_TOP_AREA || top_area<area*MIN_TOP_DENSITY)) continue;
        const char *label="OTHER";
        if (height>=BED_MIN_HEIGHT && height<=BED_MAX_HEIGHT && area>=BED_MIN_AREA) label="BED";
        else if (height>=COUCH_MIN_HEIGHT && height<=COUCH_MAX_HEIGHT && mx(w,d)+2*s->opt.voxel>=COUCH_MIN_LENGTH) label="COUCH";
        else if (height>=TABLE_MIN_HEIGHT && height<=TABLE_MAX_HEIGHT && top_area>=MIN_TOP_AREA) label="TABLE";
        else if (mn(w,d)<SCREEN_MAX_DEPTH && mx(w,d)>=SCREEN_MIN_WIDTH && mx(w,d)<=SCREEN_MAX_WIDTH &&
                 wall_distance(s,avg)<NEAR_WALL) label="SCREEN";
        else if (h>=STORAGE_MIN_HEIGHT && wall_distance(s,avg)<NEAR_WALL) label="STORAGE";
        if(mode && !same(label,"SCREEN")) continue;
        if(!mode && same(label,"OTHER") && height>TABLE_MAX_HEIGHT) continue;
        if (s->nvolumes>=MAX_VOLUMES) { warn(s,"furniture limit reached");break; }
        Vec centre=vec(avg.x+co*(u0+u1)*.5f-si*(v0+v1)*.5f,top,
                       avg.z+si*(u0+u1)*.5f+co*(v0+v1)*.5f);
        /* ROOM_FORMAT yaw is an active right-handed rotation about +Y. */
        s->volumes[s->nvolumes++]=(Volume){centre,-yaw*180/PI,w,d,h,label};
    }
    }
}

/* A hint is a fallback for missing walls, never a constraint on observed geometry. */
static const char *hint(Scan *s);
const char *make_model(Scan *s)
{
    int found;
    s->floor=plane_height(s,0,&found);
    if (!found) warn(s,"floor not seen; using STAGE floor");
    s->ceiling=plane_height(s,1,&found);
    if (!found) warn(s,"ceiling not seen");
    usize bytes=(usize)s->count*sizeof(int);
    int *ids=io_alloc(bytes),*queue=io_alloc(bytes);
    if (!ids || !queue) { io_free(ids,bytes);io_free(queue,bytes);return "model allocation failed"; }
    int n=0;
    for (int i=0;i<s->count;i++) if (s->cells[i].weight) ids[n++]=i;
    if (!n) { io_free(ids,bytes);io_free(queue,bytes);return "no surfaces in reconstruction bounds"; }
    walls(s,ids,n);outline(s,ids,n);
    const char *error=hint(s);
    if (!error) furniture(s,ids,n,queue);
    float perimeter=0,observed=0;
    for (int i=0;i<s->noutline;i++) {
        float d=root(distance2(s->outline[i],s->outline[(i+1)%s->noutline]));perimeter+=d;
        if (!s->invisible[i]) observed+=d;
    }
    s->coverage=perimeter ? 100*observed/perimeter : 0;
    if (s->coverage<LOW_COVERAGE_PERCENT) warn(s,"low wall coverage; scan all walls");
    io_free(ids,bytes);io_free(queue,bytes);
    return error;
}

static int number(char **cursor, float *value)
{
    char *p=*cursor;while (*p==' ' || *p=='\t' || *p=='\r') p++;
    int sign=1,digits=0;if (*p=='-' || *p=='+') { if (*p=='-') sign=-1;p++; }
    float v=0,scale=1;
    while (*p>='0' && *p<='9') { v=v*10+*p++-'0';digits++;if(v>10000) return 0; }
    if (*p=='.') { p++;while (*p>='0' && *p<='9') { scale*=.1f;v+=(*p++-'0')*scale;digits++; } }
    if(*p=='e' || *p=='E') {
        p++;int exponent=0,negative=0,edigits=0;
        if(*p=='-' || *p=='+') { negative=*p=='-';p++; }
        while(*p>='0' && *p<='9') { exponent=exponent*10+*p++-'0';edigits++;if(exponent>38) return 0; }
        if(!edigits) return 0;
        while(exponent--) v=negative ? v*.1f : v*10;
    }
    if (!digits || !finite(v) || v>10000) return 0;
    *value=sign*v;*cursor=p;return 1;
}
static const char *hint(Scan *s)
{
    if (!s->opt.hint) return 0;
    int fd=io_open(s->opt.hint,0);
    if (fd<0) return "cannot open outline hint";
    char data[8192];usize size=0;
    for (;;) {
        long r=io_read(fd,data+size,sizeof(data)-1-size);
        if (r<0 || (r>0 && size+(usize)r==sizeof(data)-1)) { io_close(fd);return "outline hint too large or unreadable"; }
        if (!r) break;
        size+=(usize)r;
    }
    io_close(fd);data[size]=0;
    Pt pts[MAX_OUTLINE];int n=0;char *p=data;
    while (*p) {
        while (*p==' ' || *p=='\t' || *p=='\r' || *p=='\n') p++;
        if (!*p) break;
        if (*p=='#') { while (*p && *p!='\n') p++;continue; }
        if (n==MAX_OUTLINE || !number(&p,&pts[n].x) || !number(&p,&pts[n].z)) return "invalid outline hint";
        n++;while (*p==' ' || *p=='\t' || *p=='\r') p++;
        if (*p && *p!='\n' && *p!='#') return "invalid outline hint";
        if (*p=='#') while (*p && *p!='\n') p++;
    }
    if (n<3) return "outline hint needs at least three points";
    Pt centre={0};for (int i=0;i<n;i++) { centre.x+=pts[i].x/n;centre.z+=pts[i].z/n; }
    float mean=0,variance=0;
    for (int i=0;i<n;i++) mean+=root(distance2(pts[i],centre))/n;
    for (int i=0;i<n;i++) { float r=root(distance2(pts[i],centre))-mean;variance+=r*r/n; }
    if (n>=DEFAULT_CIRCLE_MIN_POINTS && root(variance)<mean*DEFAULT_CIRCLE_RADIAL_CV) {
        warn(s,"default circle hint ignored");return 0;
    }
    int missing=0;for(int i=0;i<s->noutline;i++) missing+=s->invisible[i];
    if (!missing) return 0;
    Pt saved[MAX_OUTLINE];int inv[MAX_OUTLINE],old=s->noutline;
    memcpy(saved,s->outline,sizeof(saved));memcpy(inv,s->invisible,sizeof(inv));
    float area=0;for(int i=0;i<n;i++) area+=pts[i].x*pts[(i+1)%n].z-pts[(i+1)%n].x*pts[i].z;
    s->noutline=n;
    for(int i=0;i<n;i++) { s->outline[i]=pts[area>0 ? i : n-1-i];s->invisible[i]=1; }
    if (!valid_polygon(s)) {
        memcpy(s->outline,saved,sizeof(saved));memcpy(s->invisible,inv,sizeof(inv));s->noutline=old;
        return "invalid outline hint polygon";
    }
    /* Only use a hint compatible with every observed wall. Chaperone can be an inset play area. */
    for(int i=0;i<s->nplanes;i++) {
        Plane *w=&s->planes[i];int match=0;
        for(int k=0;k<n;k++) {
            Pt a=s->outline[k],b=s->outline[(k+1)%n];
            if(ab(a.x*w->n.x+a.z*w->n.z-w->d)<s->opt.gap &&
               ab(b.x*w->n.x+b.z*w->n.z-w->d)<s->opt.gap) { match=1;s->invisible[k]=0; }
        }
        if (!match) {
            memcpy(s->outline,saved,sizeof(saved));memcpy(s->invisible,inv,sizeof(inv));s->noutline=old;
            warn(s,"hint disagrees with observed walls; ignored");return 0;
        }
    }
    s->hint_used=1;warn(s,"incomplete walls closed using outline hint");return 0;
}
