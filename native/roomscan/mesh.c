/* SPDX-License-Identifier: GPL-3.0-only */
#include "scan.h"

/* Marching cubes without a 256-case table: trace the zero contour on the six faces, then fan each closed loop.
 * Ambiguous faces choose their diagonal from the face centre. Adjacent cubes therefore make the same choice. */
static const int corners[8][3]={{0,0,0},{1,0,0},{1,0,1},{0,0,1},{0,1,0},{1,1,0},{1,1,1},{0,1,1}};
static const int edges[12][2]={{0,1},{1,2},{2,3},{3,0},{4,5},{5,6},{6,7},{7,4},{0,4},{1,5},{2,6},{3,7}};
static const int faces[6][4]={{0,1,2,3},{4,5,6,7},{0,9,4,8},{1,10,5,9},{2,11,6,10},{3,8,7,11}};

static void tsdf(Scan *s)
{
    float v=s->opt.voxel,trunc=v*s->opt.truncation;
    int radius=(int)s->opt.truncation+2;
    for (int i=0;i<s->count;i++) {
        Cell *c=&s->cells[i];
        if (!c->weight) continue;
        int x=i%s->nx,z=(i/s->nx)%s->nz,y=i/(s->nx*s->nz);
        for (int dy=-radius;dy<=radius;dy++) for (int dz=-radius;dz<=radius;dz++) for (int dx=-radius;dx<=radius;dx++) {
            int xx=x+dx,yy=y+dy,zz=z+dz;
            if (xx<0 || yy<0 || zz<0 || xx>=s->nx || yy>=s->ny || zz>=s->nz) continue;
            Vec delta=sub(grid_point(s,xx,yy,zz),c->p);
            float d=dot(delta,c->n),tangent=dot(delta,delta)-d*d;
            if (ab(d)>trunc || tangent>v*v*2.25f) continue;
            Cell *dest=&s->cells[(yy*s->nz+zz)*s->nx+xx];
            float w=(1-tangent/(v*v*2.5f))*mn(c->weight/(v*v),1.f);
            dest->sdf+=d*w;dest->sw+=w;
        }
    }
    for (int i=0;i<s->count;i++) if(s->cells[i].sw) s->cells[i].sdf/=s->cells[i].sw;
}
static void connect(int links[12][2], int a, int b)
{
    if(links[a][0]<0) links[a][0]=b;else links[a][1]=b;
    if(links[b][0]<0) links[b][0]=a;else links[b][1]=a;
}
static int cube(Scan *s, int x, int y, int z, int stride)
{
    float f[8];Vec p[8],normal=vec(0,0,0);
    for(int k=0;k<8;k++) {
        int xx=x+corners[k][0]*stride,yy=y+corners[k][1]*stride,zz=z+corners[k][2]*stride;
        Cell *c=&s->cells[(yy*s->nz+zz)*s->nx+xx];
        if (c->sw<=0) return 1; /* Unknown space never creates an invented surface. */
        f[k]=c->sdf;p[k]=grid_point(s,xx,yy,zz);
    }
    Vec cuts[12];int crossing[12],links[12][2],visited[12]={0};
    for(int e=0;e<12;e++) {
        int a=edges[e][0],b=edges[e][1];
        crossing[e]=(f[a]<0)!=(f[b]<0);links[e][0]=links[e][1]=-1;
        if(crossing[e]) cuts[e]=add(p[a],mul(sub(p[b],p[a]),f[a]/(f[a]-f[b])));
    }
    /* Gradient points to positive TSDF: toward the observed free room, including furniture's exterior. */
    normal.x=f[1]+f[2]+f[5]+f[6]-f[0]-f[3]-f[4]-f[7];
    normal.y=f[4]+f[5]+f[6]+f[7]-f[0]-f[1]-f[2]-f[3];
    normal.z=f[2]+f[3]+f[6]+f[7]-f[0]-f[1]-f[4]-f[5];
    for(int face=0;face<6;face++) {
        int hit[4],n=0;float centre=0;
        for(int j=0;j<4;j++) {
            int e=faces[face][j];
            if(crossing[e]) hit[n++]=e;
            centre+=f[edges[e][0]]+f[edges[e][1]];
        }
        if(n==2) connect(links,hit[0],hit[1]);
        else if(n==4) {
            /* The shared first edge's first corner fixes the phase on this face. */
            int first=edges[hit[0]][0],second=edges[hit[0]][1];
            int common=(edges[hit[1]][0]==second || edges[hit[1]][1]==second) ? second : first;
            if((centre<0)==(f[common]<0)) {
                connect(links,hit[0],hit[3]);connect(links,hit[1],hit[2]);
            } else { connect(links,hit[0],hit[1]);connect(links,hit[2],hit[3]); }
        }
    }
    for(int e=0;e<12;e++) if(crossing[e] && !visited[e]) {
        int loop[12],n=0,prev=-1,cur=e;
        while(cur>=0 && !visited[cur] && n<12) {
            visited[cur]=1;loop[n++]=cur;
            int next=links[cur][0]==prev ? links[cur][1] : links[cur][0];prev=cur;cur=next;
        }
        if(cur!=e) continue;
        for(int k=1;k+1<n;k++) {
            Vec a=cuts[loop[0]],b=cuts[loop[k]],c=cuts[loop[k+1]];
            Vec norm=cross(sub(b,a),sub(c,a));
            if(dot(norm,norm)<1e-14f) continue;
            if(s->ni/3>=MAX_TRIANGLES) return 0;
            if(dot(norm,normal)<0) { Vec t=b;b=c;c=t; }
            s->vertices[s->nv]=a;s->vertices[s->nv+1]=b;s->vertices[s->nv+2]=c;
            for(int j=0;j<3;j++) s->indices[s->ni++]=(u32)s->nv++;
        }
    }
    return 1;
}
const char *make_mesh(Scan *s)
{
    s->vertices=io_alloc((usize)MAX_MESH_VERTICES*sizeof(Vec));
    s->indices=io_alloc((usize)MAX_TRIANGLES*3*sizeof(u32));
    if(!s->vertices || !s->indices) return "mesh allocation failed";
    tsdf(s);
    for(int stride=1;stride<=4;stride++) {
        s->nv=s->ni=0;int ok=1;
        for(int y=0;y+stride<s->ny && ok;y+=stride)
            for(int z=0;z+stride<s->nz && ok;z+=stride)
                for(int x=0;x+stride<s->nx && ok;x+=stride) ok=cube(s,x,y,z,stride);
        if(ok) {
            if(stride>1) warn(s,"mesh decimated by coarsening extraction grid");
            if(!s->ni) warn(s,"no mesh surface; scan denser views");
            return 0;
        }
    }
    return "mesh exceeds triangle budget";
}
