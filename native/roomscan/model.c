/* SPDX-License-Identifier: GPL-3.0-only */
#include "scan.h"

#define RANSAC_TRIALS 96
#define NORMAL_AGREEMENT .90f
#define MIN_WALL_SPAN .60f
#define FURNITURE_MARGIN .12f
#define NEAR_WALL .35f
#define MIN_TOP_AREA .25f

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
    float y=s->origin.y+(best+.5f)*step, total=0, sum=0;
    for (int i=0;i<s->count;i++) {
        Cell *c=&s->cells[i];
        if (!c->weight || ab(c->p.y-y)>s->opt.plane_distance) continue;
        if (ceiling ? c->n.y > -s->opt.horizontal : c->n.y < s->opt.horizontal) continue;
        total++; sum+=c->p.y;
    }
    *found=score* s->opt.voxel*s->opt.voxel >= s->opt.min_plane_area && total>0;
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
    for (int i=0;i<n;i++) {
        Cell *c=&s->cells[ids[i]];
        c->mark=ab(c->n.y)>.35f || c->p.y<s->floor+FURNITURE_MARGIN || c->p.y>s->ceiling-FURNITURE_MARGIN;
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
    if (x1-x0<1) { float m=(x0+x1)*.5f; x0=m-.5f; x1=m+.5f; }
    if (z1-z0<1) { float m=(z0+z1)*.5f; z0=m-.5f; z1=m+.5f; }
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
    return s->noutline>=3 && area>.5f;
}
static void outline(Scan *s, int *ids, int n)
{
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
    if (!valid_polygon(s)) { s->noutline=0;rectangle(s,ids,n);warn(s,"wall ordering ambiguous"); }
}
static float wall_distance(Scan *s, Vec p)
{
    float best=1e10f;
    for (int i=0;i<s->nplanes;i++) best=mn(best,ab(dot(s->planes[i].n,p)-s->planes[i].d));
    return best;
}
static void furniture(Scan *s, int *ids, int n, int *queue)
{
    for (int j=0;j<n;j++) {
        Cell *c=&s->cells[ids[j]];
        c->mark=(c->p.y>s->floor+FURNITURE_MARGIN && c->p.y<s->ceiling-FURNITURE_MARGIN &&
                 wall_distance(s,c->p)>s->opt.plane_distance*2) ? 1 : 0;
    }
    int radius=(int)(s->opt.cluster_gap/s->opt.voxel)+1;
    for (int j=0;j<n;j++) {
        int seed=ids[j];
        if (s->cells[seed].mark!=1) continue;
        int head=0,tail=1;queue[0]=seed;s->cells[seed].mark=2;
        while (head<tail) {
            int id=queue[head++],x=id%s->nx,z=(id/s->nx)%s->nz,y=id/(s->nx*s->nz);
            for (int dy=-radius;dy<=radius;dy++) for (int dz=-radius;dz<=radius;dz++) for (int dx=-radius;dx<=radius;dx++) {
                if (dx*dx+dy*dy+dz*dz>radius*radius) continue;
                int xx=x+dx,yy=y+dy,zz=z+dz;
                if (xx<0 || yy<0 || zz<0 || xx>=s->nx || yy>=s->ny || zz>=s->nz) continue;
                int next=(yy*s->nz+zz)*s->nx+xx;
                Cell *c=&s->cells[next];
                if (c->mark==1) { c->mark=2;queue[tail++]=next; }
            }
        }
        if (tail<12) continue;
        Vec avg=vec(0,0,0);float top=-1e10f,bottom=1e10f;
        for (int k=0;k<tail;k++) {
            Vec p=s->cells[queue[k]].p;avg=add(avg,p);top=mx(top,p.y);bottom=mn(bottom,p.y);
        }
        avg=mul(avg,1.f/tail);
        float xx=0,zz=0,xz=0,edge_x=0,edge_y=0,edge_count=0;
        for (int k=0;k<tail;k++) {
            Cell *c=&s->cells[queue[k]];
            Vec p=sub(c->p,avg);xx+=p.x*p.x;zz+=p.z*p.z;xz+=p.x*p.z;
            if(ab(c->n.y)<.3f) {
                Vec norm=unit(vec(c->n.x,0,c->n.z));
                float co2=norm.x*norm.x-norm.z*norm.z,si2=2*norm.x*norm.z;
                edge_x+=co2*co2-si2*si2;edge_y+=2*co2*si2;edge_count++;
            }
        }
        float yaw=.5f*angle(2*xz,xx-zz);
        /* Opposing faces and perpendicular edges vote for the same fourth harmonic. Unlike PCA, this is not
         * biased by one side being more visible. Non-box objects fall back to position covariance. */
        if(edge_count>12 && root(edge_x*edge_x+edge_y*edge_y)>edge_count*.4f)
            yaw=.25f*angle(edge_y,edge_x);
        float co=cosine(yaw),si=sine(yaw);
        float u0=1e10f,u1=-1e10f,v0=1e10f,v1=-1e10f,top_count=0;
        for (int k=0;k<tail;k++) {
            Cell *c=&s->cells[queue[k]];Vec p=sub(c->p,avg);
            float u=p.x*co+p.z*si,v=-p.x*si+p.z*co;
            u0=mn(u0,u);u1=mx(u1,u);v0=mn(v0,v);v1=mx(v1,v);
            if (c->n.y>s->opt.horizontal && c->p.y>top-s->opt.plane_distance*2) top_count++;
        }
        float w=u1-u0,d=v1-v0,h=top-(bottom<s->floor+.3f ? s->floor : mn(bottom,s->floor+FURNITURE_MARGIN));
        if (w*d*h<s->opt.min_volume) continue;
        float height=top-s->floor,area=w*d,top_area=top_count*s->opt.voxel*s->opt.voxel;
        const char *label="OTHER";
        if (height>=.3f && height<=.8f && area>=2) label="BED";
        else if (height>=.35f && height<=.9f && mx(w,d)>=1.4f) label="COUCH";
        else if (height>=.55f && height<=1.1f && top_area>=MIN_TOP_AREA) label="TABLE";
        else if (mn(w,d)<.22f && mx(w,d)>=.4f && mx(w,d)<=2 && wall_distance(s,avg)<NEAR_WALL) label="SCREEN";
        else if (h>=1.1f && wall_distance(s,avg)<NEAR_WALL) label="STORAGE";
        if (s->nvolumes>=MAX_VOLUMES) { warn(s,"furniture limit reached");break; }
        Vec centre=vec(avg.x+co*(u0+u1)*.5f-si*(v0+v1)*.5f,top,
                       avg.z+si*(u0+u1)*.5f+co*(v0+v1)*.5f);
        /* ROOM_FORMAT yaw is an active right-handed rotation about +Y. */
        s->volumes[s->nvolumes++]=(Volume){centre,-yaw*180/PI,w,d,h,label};
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
    if (s->coverage<80) warn(s,"low wall coverage; scan all walls");
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
    if (!digits || !finite(v)) return 0;
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
    if (n>=8 && root(variance)<mean*.05f) { warn(s,"default circle hint ignored");return 0; }
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
