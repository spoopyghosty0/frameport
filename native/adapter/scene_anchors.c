// SPDX-License-Identifier: GPL-3.0-only
// Included by scene_emu.c. Persist only application anchors; scanned entities remain owned by the room file.
#include <sys/random.h>
#include <sys/stat.h>
#include <unistd.h>
#ifndef SCENE_EXTERNAL_ROOT
#define SCENE_EXTERNAL_ROOT "/sdcard/Android/data"
#endif

static int anchor_path(char *path, size_t size) {
    char process[256] = {0};
    FILE *f = fopen("/proc/self/cmdline", "r");
    if (!f) return 0;
    size_t n = fread(process, 1, sizeof(process)-1, f);
    fclose(f);
    if (!n) return 0;
    char *colon = strchr(process, ':');
    if (colon) *colon = 0;
    // Only package-like process names: never let a command line become a path traversal.
    if (!strchr(process, '.') || !strcmp(process, ".") || !strcmp(process, "..")) return 0;
    for (const char *p = process; *p; ++p)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') || *p == '.' || *p == '_')) return 0;
    int len = snprintf(path, size, "%s/%s/files/framebridge-anchors.txt", SCENE_EXTERNAL_ROOT, process);
    return len > 0 && (size_t)len < size;
}
static int anchor_uuid(XrUuidEXT *u) {
    size_t have = 0;
    while (have < sizeof(u->data)) {
        ssize_t n = getrandom(u->data + have, sizeof(u->data) - have, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        have += (size_t)n;
    }
    if (have < sizeof(u->data)) {
        FILE *f = fopen("/dev/urandom", "rb");
        if (!f) return 0;
        have = fread(u->data, 1, sizeof(u->data), f);
        fclose(f);
        if (have != sizeof(u->data)) return 0;
    }
    u->data[6] = (uint8_t)((u->data[6] & 15) | 0x40);
    u->data[8] = (uint8_t)((u->data[8] & 63) | 0x80);
    for (int i = 0; i < entity_count; ++i) if (uuid_equal(u, &entities[i].uuid)) return 0;
    return 1;
}
static int write_anchors(void) {
    char path[4096], tmp[4128];
    if (!anchor_path(path, sizeof(path))) return 0;
    snprintf(tmp, sizeof(tmp), "%s.tmp-XXXXXX", path);
    int fd = mkstemp(tmp);
    if (fd < 0) return 0;
    FILE *f = fdopen(fd, "w");
    if (!f) { close(fd); unlink(tmp); return 0; }
    int ok = 1;
    for (int i = 0; i < entity_count; ++i) {
        const scene_entity *e = &entities[i];
        if (!e->is_anchor || !e->saved || e->erased) continue;
        char uuid[33];
        for (int k = 0; k < 16; ++k) snprintf(uuid + k*2, 3, "%02x", e->uuid.data[k]);
        const XrPosef p = e->pose_in_stage;
        if (fprintf(f, "anchor %s %.9g %.9g %.9g %.9g %.9g %.9g %.9g\n", uuid,
                    p.position.x, p.position.y, p.position.z, p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w) < 0) ok = 0;
    }
    if (fflush(f) || fsync(fd)) ok = 0;
    if (fclose(f)) ok = 0;
    if (ok && rename(tmp, path)) ok = 0;
    if (!ok) { LOG("scene: anchor write failed (%d)", errno); unlink(tmp); }
    return ok;
}
static void load_anchors(void) {
    char path[4096];
    if (!anchor_path(path, sizeof(path))) return;
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[1024], *t[10];
    unsigned loaded = 0;
    while (fgets(line, sizeof(line), f)) {
        if (!strchr(line, '\n') && !feof(f)) { int c; while ((c=fgetc(f)) != EOF && c != '\n') {} continue; }
        XrUuidEXT uuid; float v[7];
        int n = room_tokens(line, t, 10);
        if (n != 9 || strcmp(t[0], "anchor") || !room_uuid(t[1], &uuid) || !room_values(t, 2, 7, v)) continue;
        float norm = sqrtf(v[3]*v[3]+v[4]*v[4]+v[5]*v[5]+v[6]*v[6]);
        if (norm < 0.0001f) continue;
        int duplicate = 0;
        for (int i = 0; i < entity_count; ++i) duplicate |= uuid_equal(&uuid, &entities[i].uuid);
        if (duplicate) continue;
        int i = add_entity((XrPosef){{v[3]/norm,v[4]/norm,v[5]/norm,v[6]/norm},{v[0],v[1],v[2]}}, NULL, 0, 0, 0, 1);
        if (i < 0) break;
        entities[i].uuid = uuid; entities[i].saved = 1; ++loaded;
    }
    fclose(f);
    LOG("scene: loaded %u saved anchors", loaded);
}
// Batch writes are atomic both on disk and in memory. Failure never silently destroys the old anchor set.
static XrResult persist_spaces(uint32_t count, const XrSpace *spaces, uint32_t uuid_count, const XrUuidEXT *uuids, int erase) {
    unsigned char selected[MAX_ENTITIES] = {0}, old_saved[MAX_ENTITIES], old_erased[MAX_ENTITIES];
    for (uint32_t k = 0; k < count; ++k) {
        int i = fake_space_index(spaces[k]);
        if (i < 0) return XR_ERROR_HANDLE_INVALID;
        if (entities[i].is_anchor) selected[i] = 1;
    }
    if (erase) for (int i = 0; i < entity_count; ++i)
        if (entities[i].is_anchor && !entities[i].erased && uuid_matches(uuid_count, uuids, &entities[i])) selected[i] = 1;
    int changed = 0;
    for (int i = 0; i < entity_count; ++i) {
        old_saved[i] = (unsigned char)entities[i].saved; old_erased[i] = (unsigned char)entities[i].erased;
        if (selected[i]) { entities[i].saved = !erase; entities[i].erased = erase; changed = 1; }
    }
    if (changed && !write_anchors()) {
        for (int i = 0; i < entity_count; ++i) { entities[i].saved = old_saved[i]; entities[i].erased = old_erased[i]; }
        return XR_ERROR_RUNTIME_FAILURE;
    }
    if (changed) LOG("scene: anchors %s", erase ? "erased" : "saved");
    return XR_SUCCESS;
}
