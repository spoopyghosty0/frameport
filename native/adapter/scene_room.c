// SPDX-License-Identifier: GPL-3.0-only
// Included by scene_emu.c. Bounded parsing keeps corrupt scan files from exhausting an Android game.
#include <errno.h>
#include <stdint.h>

static int room_uuid(const char *s, XrUuidEXT *u) {
    if (strlen(s) != 32) return 0;
    for (int i = 0; i < 16; ++i) {
        unsigned n = 0;
        for (int k = 0; k < 2; ++k) {
            char c = s[i * 2 + k];
            if (c >= '0' && c <= '9') n = n * 16 + (unsigned)(c - '0');
            else if (c >= 'a' && c <= 'f') n = n * 16 + (unsigned)(c - 'a' + 10);
            else return 0;
        }
        u->data[i] = (uint8_t)n;
    }
    return 1;
}
static int room_float(const char *s, float *v) {
    char *end;
    errno = 0;
    *v = strtof(s, &end);
    return *s && !*end && !errno && isfinite(*v) && fabsf(*v) <= 100000;
}
static int room_uint(const char *s, uint64_t *v) {
    char *end;
    if (*s < '0' || *s > '9') return 0;
    errno = 0;
    *v = strtoull(s, &end, 10);
    return !*end && !errno;
}
static int room_tokens(char *s, char **t, int cap) {
    char *comment = strchr(s, '#');
    if (comment) *comment = 0;
    int n = 0;
    char *save;
    for (char *p = strtok_r(s, " \t\r\n", &save); p; p = strtok_r(NULL, " \t\r\n", &save)) {
        if (n == cap) return -1;
        t[n++] = p;
    }
    return n;
}
static int room_values(char **t, int start, int count, float *v) {
    for (int i = 0; i < count; ++i) if (!room_float(t[start + i], &v[i])) return 0;
    return 1;
}
static int room_path(char *out, size_t size, const char *name) {
    const char *config = getenv("FRAMEBRIDGE_CONFIG");
    if (!config || !*config || strchr(name, '/') || strchr(name, '\\') || !strcmp(name, "..")) return 0;
    const char *slash = strrchr(config, '/');
    int n = slash ? snprintf(out, size, "%.*s/%s", (int)(slash - config), config, name)
                  : snprintf(out, size, "%s", name);
    return n >= 0 && (size_t)n < size;
}
static uint32_t room_u32(const unsigned char *p) {
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static int room_mesh(scene_entity *e, const char *name, uint64_t nv, uint64_t ni) {
    // At most 200k triangles and 16 MiB of payload; validate every index before publishing the mesh.
    if (!nv || nv > 600000 || !ni || ni > 600000 || ni % 3 || nv * 12 + ni * 4 > 16 * 1024 * 1024) return 0;
    char path[4096];
    if (!room_path(path, sizeof(path), name)) return 0;
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    unsigned char header[16], word[4];
    int ok = fread(header, 1, 16, f) == 16 && !memcmp(header, "FPRM", 4) && room_u32(header + 4) == 1 &&
             room_u32(header + 8) == nv && room_u32(header + 12) == ni;
    XrVector3f *verts = ok ? malloc((size_t)nv * sizeof(*verts)) : NULL;
    uint32_t *indices = ok ? malloc((size_t)ni * sizeof(*indices)) : NULL;
    ok = ok && verts && indices;
    for (uint64_t i = 0; ok && i < nv; ++i) {
        float xyz[3];
        for (int k = 0; ok && k < 3; ++k) {
            ok = fread(word, 1, 4, f) == 4;
            uint32_t bits = ok ? room_u32(word) : 0;
            memcpy(&xyz[k], &bits, 4);
            ok = ok && isfinite(xyz[k]) && fabsf(xyz[k]) <= 100000;
        }
        if (ok) verts[i] = (XrVector3f){xyz[0], xyz[1], xyz[2]};
    }
    for (uint64_t i = 0; ok && i < ni; ++i) {
        ok = fread(word, 1, 4, f) == 4;
        indices[i] = ok ? room_u32(word) : 0;
        ok = ok && indices[i] < nv;
    }
    ok = ok && fgetc(f) == EOF && !ferror(f);
    fclose(f);
    if (!ok) { free(verts); free(indices); return 0; }
    e->vertices = verts; e->indices = indices;
    e->vertex_count = (uint32_t)nv; e->index_count = (uint32_t)ni;
    return 1;
}
static int room_set_uuid(int i, XrUuidEXT uuid) {
    for (int k = 0; k < entity_count; ++k)
        if (k != i && uuid_equal(&entities[k].uuid, &uuid)) return 0;
    entities[i].uuid = uuid;
    return 1;
}
static int room_wall(XrUuidEXT uuid, const float *v) {
    float dx = v[2] - v[0], dz = v[3] - v[1], length = hypotf(dx, dz);
    if (length < 0.001f || v[5] <= v[4]) return 0;
    float angle = atan2f(-dz, dx) / 2;
    int i = add_entity((XrPosef){{0, sinf(angle), 0, cosf(angle)},
                       {(v[0] + v[2]) / 2, (v[4] + v[5]) / 2, (v[1] + v[3]) / 2}},
                       "WALL_FACE", length, v[5] - v[4], 0, 0);
    if (i < 0) return 0;
    if (!room_set_uuid(i, uuid)) { --entity_count; return 0; }
    entities[i].is_wall = 1;
    return 1;
}
static int room_outline(scene_entity *e, const XrVector2f *points, int n, float cx, float cz, int ceiling) {
    e->boundary = malloc((size_t)n * sizeof(*e->boundary));
    if (!e->boundary) return 0;
    e->boundary_count = (uint32_t)n;
    float xmin = INFINITY, ymin = INFINITY, xmax = -INFINITY, ymax = -INFINITY;
    for (int i = 0; i < n; ++i) {
        XrVector2f p = {points[i].x - cx, (points[i].y - cz) * (ceiling ? 1 : -1)};
        e->boundary[i] = p;
        xmin = fminf(xmin, p.x); xmax = fmaxf(xmax, p.x);
        ymin = fminf(ymin, p.y); ymax = fmaxf(ymax, p.y);
    }
    e->has_bounds = 1;
    e->bounds = (XrRect2Df){{xmin, ymin}, {xmax - xmin, ymax - ymin}};
    return 1;
}
static int load_room_file(void) {
    char path[4096];
    if (!room_path(path, sizeof(path), "room.txt")) return 0;
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    // Long enough for a 1024-point outline. Reject whole overlong lines, never interpret their tails.
    char *line = malloc(65536);
    char **t = malloc((2 * MAX_OUTLINE + 4) * sizeof(*t));
    if (!line || !t) { free(line); free(t); fclose(f); return 0; }
    int ok = fgets(line, 65536, f) != NULL;
    int n = ok ? room_tokens(line, t, 2 * MAX_OUTLINE + 4) : 0;
    if (n != 2 || strcmp(t[0], "frameport-room") || strcmp(t[1], "1")) {
        LOG("scene: bad room magic/version -> guardian box");
        free(line); free(t); fclose(f); return 0;
    }
    const float s = 0.70710678f;
    add_entity((XrPosef){{0, 0, 0, 1}, {0, 0, 0}}, NULL, 0, 0, 1, 0);
    add_entity((XrPosef){{-s, 0, 0, s}, {0, 0, 0}}, "FLOOR", 0, 0, 0, 0);
    add_entity((XrPosef){{s, 0, 0, s}, {0, 2.5f, 0}}, "CEILING", 0, 0, 0, 0);
    XrVector2f outline[MAX_OUTLINE];
    int outline_n = 0, ceiling_set = 0, volumes = 0, planes = 0, triangles = 0;
    unsigned lineno = 1;
    snprintf(room_source, sizeof(room_source), "manual");
    while (fgets(line, 65536, f)) {
        ++lineno;
        if (!strchr(line, '\n') && !feof(f)) {
            int c; while ((c = fgetc(f)) != EOF && c != '\n') {}
            LOG("scene: bad room line %u (too long)", lineno); continue;
        }
        n = room_tokens(line, t, 2 * MAX_OUTLINE + 4);
        if (!n) continue;
        ok = n > 0;
        float v[10]; XrUuidEXT uuid; uint64_t a, b;
        if (n < 0) ok = 0;
        else if (!strcmp(t[0], "source")) {
            ok = n == 2 && (!strcmp(t[1], "scan") || !strcmp(t[1], "boundary") || !strcmp(t[1], "box") || !strcmp(t[1], "manual"));
            if (ok) snprintf(room_source, sizeof(room_source), "%s", t[1]);
        } else if (!strcmp(t[0], "universe")) {
            ok = n == 2 && room_uint(t[1], &a);
            if (ok) room_universe = a;
        } else if (!strcmp(t[0], "room") || !strcmp(t[0], "floor") || !strcmp(t[0], "ceiling")) {
            int i = !strcmp(t[0], "room") ? ENT_ROOM : !strcmp(t[0], "floor") ? ENT_FLOOR : ENT_CEILING;
            ok = n == (i == ENT_ROOM ? 2 : 3) && room_uuid(t[1], &uuid) &&
                 (i == ENT_ROOM || room_float(t[2], v)) && room_set_uuid(i, uuid);
            if (ok && i != ENT_ROOM) entities[i].pose_in_stage.position.y = v[0];
            if (ok && i == ENT_CEILING) ceiling_set = 1;
        } else if (!strcmp(t[0], "outline")) {
            ok = n >= 2 && room_uint(t[1], &a) && a >= 3 && a <= MAX_OUTLINE && n == 2 + 2 * (int)a;
            for (uint64_t k = 0; ok && k < a; ++k) ok = room_float(t[2 + k * 2], &outline[k].x) && room_float(t[3 + k * 2], &outline[k].y);
            if (ok) outline_n = (int)a;
        } else if (!strcmp(t[0], "wall")) {
            ok = n == 8 && room_uuid(t[1], &uuid) && room_values(t, 2, 6, v) && room_wall(uuid, v);
        } else if (!strcmp(t[0], "volume") || !strcmp(t[0], "plane")) {
            int volume = !strcmp(t[0], "volume");
            ok = n == (volume ? 10 : 12) && room_uuid(t[1], &uuid) && strlen(t[2]) < sizeof(entities[0].label) && room_values(t, 3, volume ? 7 : 9, v);
            XrPosef pose = {{0, 0, 0, 1}, {0, 0, 0}};
            float w = 0, h = 0;
            if (ok && volume) {
                float yaw = v[3] * 0.00872664626f;
                pose = (XrPosef){quat_mul((XrQuaternionf){0, sinf(yaw), 0, cosf(yaw)}, (XrQuaternionf){-s, 0, 0, s}), {v[0], v[2], v[1]}};
                w = v[4]; h = v[5]; ok = v[6] > 0;
            } else if (ok) {
                float norm = sqrtf(v[3]*v[3]+v[4]*v[4]+v[5]*v[5]+v[6]*v[6]);
                ok = norm > 0.0001f;
                if (ok) pose = (XrPosef){{v[3]/norm,v[4]/norm,v[5]/norm,v[6]/norm}, {v[0],v[1],v[2]}};
                w = v[7]; h = v[8];
            }
            int i = ok && w > 0 && h > 0 ? add_entity(pose, t[2], w, h, 0, 0) : -1;
            ok = i >= 0;
            if (ok && !room_set_uuid(i, uuid)) { --entity_count; ok = 0; }
            if (ok && volume) {
                entities[i].has_bounds3 = 1;
                entities[i].bounds3 = (XrRect3DfFB){{-w/2, -h/2, -v[6]}, {w, h, v[6]}};
                ++volumes;
            } else if (ok) ++planes;
        } else if (!strcmp(t[0], "mesh")) {
            ok = n == 5 && room_uuid(t[1], &uuid) && room_uint(t[3], &a) && room_uint(t[4], &b) && !triangles;
            int i = ok ? add_entity((XrPosef){{0,0,0,1},{0,0,0}}, "GLOBAL_MESH", 0, 0, 0, 0) : -1;
            ok = i >= 0 && room_set_uuid(i, uuid) && room_mesh(&entities[i], t[2], a, b);
            if (ok) triangles = (int)b / 3;
            else if (i >= 0) --entity_count;
        }
        if (!ok) LOG("scene: bad room line %u (skipped)", lineno);
    }
    ok = !ferror(f);
    free(line); free(t); fclose(f);
    // Layout walls must be contiguous and in file order, regardless of where other records occurred.
    for (int i = ENT_WALL0; i < entity_count; ++i) if (entities[i].is_wall) {
        scene_entity wall = entities[i];
        memmove(&entities[ENT_WALL0 + wall_count + 1], &entities[ENT_WALL0 + wall_count],
                (size_t)(i - ENT_WALL0 - wall_count) * sizeof(wall));
        entities[ENT_WALL0 + wall_count++] = wall;
    }
    if (!outline_n && wall_count) {
        float xmin = INFINITY, zmin = INFINITY, xmax = -INFINITY, zmax = -INFINITY;
        for (int i = ENT_WALL0; i < ENT_WALL0 + wall_count; ++i) {
            scene_entity *e = &entities[i];
            XrVector3f d = quat_rotate(e->pose_in_stage.orientation, (XrVector3f){e->bounds.extent.width/2,0,0});
            xmin = fminf(xmin, e->pose_in_stage.position.x - fabsf(d.x)); xmax = fmaxf(xmax, e->pose_in_stage.position.x + fabsf(d.x));
            zmin = fminf(zmin, e->pose_in_stage.position.z - fabsf(d.z)); zmax = fmaxf(zmax, e->pose_in_stage.position.z + fabsf(d.z));
        }
        outline[0]=(XrVector2f){xmin,zmin}; outline[1]=(XrVector2f){xmax,zmin};
        outline[2]=(XrVector2f){xmax,zmax}; outline[3]=(XrVector2f){xmin,zmax}; outline_n=4;
    }
    double area = 0, cx = 0, cz = 0;
    for (int i = 0; i < outline_n; ++i) {
        XrVector2f a = outline[i], b = outline[(i+1)%outline_n];
        double cross = (double)a.x*b.y-(double)b.x*a.y;
        area += cross; cx += (a.x+b.x)*cross; cz += (a.y+b.y)*cross;
    }
    if (!ok || outline_n < 3 || fabs(area) < 0.00001) { reset_room(); return 0; }
    cx /= 3*area; cz /= 3*area;
    if (!ceiling_set) entities[ENT_CEILING].pose_in_stage.position.y = entities[ENT_FLOOR].pose_in_stage.position.y + 2.5f;
    for (int i = ENT_FLOOR; i <= ENT_CEILING; ++i) {
        entities[i].pose_in_stage.position.x = (float)cx; entities[i].pose_in_stage.position.z = (float)cz;
        if (!room_outline(&entities[i], outline, outline_n, (float)cx, (float)cz, i == ENT_CEILING)) { reset_room(); return 0; }
    }
    if (!wall_count) for (int i = 0; i < outline_n; ++i) {
        XrVector2f a = outline[i], b = outline[(i+1)%outline_n];
        float v[] = {a.x,a.y,b.x,b.y,entities[ENT_FLOOR].pose_in_stage.position.y,entities[ENT_CEILING].pose_in_stage.position.y};
        if (!room_wall(make_uuid(entity_count), v)) { reset_room(); return 0; }
        ++wall_count;
    }
    // Move generated walls ahead of furniture as well.
    if (wall_count && !entities[ENT_WALL0].is_wall) {
        scene_entity *walls = malloc((size_t)wall_count * sizeof(*walls));
        if (!walls) { reset_room(); return 0; }
        memcpy(walls, &entities[entity_count-wall_count], (size_t)wall_count*sizeof(*walls));
        memmove(&entities[ENT_WALL0+wall_count], &entities[ENT_WALL0], (size_t)(entity_count-wall_count-ENT_WALL0)*sizeof(*walls));
        memcpy(&entities[ENT_WALL0], walls, (size_t)wall_count*sizeof(*walls)); free(walls);
    }
    for (int i = 0; i < entity_count; ++i) entities[i].handle = fake_space(i);
    room_entity_count = entity_count; room_from_file = 1;
    LOG("scene: room from %s file (%d walls, %d volumes, %d planes, %d mesh triangles, universe %llu)",
        room_source, wall_count, volumes, planes, triangles, (unsigned long long)room_universe);
    return 1;
}
