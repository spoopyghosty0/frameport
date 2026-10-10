// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c. Emulates Meta's scene / spatial-entity extensions (XR_FB_spatial_entity,
// _query, _storage, _container, XR_FB_scene, XR_FB_scene_capture) for mixed-reality apps on runtimes that
// lack them (Steam Frame). The "room" is a box built from the runtime's STAGE bounds (the guardian /
// chaperone rectangle): floor, ceiling and four walls, reported as one room anchor with a layout.
// Entity spaces are fake handles; every function that consumes a space is intercepted for them.

static float scene_min_size = 1.5f;  // smallest room edge if the guardian is tiny or unavailable
static int emulate_scene;            // app requested the scene extensions and the runtime lacks them

static const char *const scene_extensions[] = {
    "XR_FB_spatial_entity", "XR_FB_spatial_entity_query", "XR_FB_spatial_entity_storage",
    "XR_FB_spatial_entity_container", "XR_FB_scene", "XR_FB_scene_capture",
    "XR_META_spatial_entity_discovery", "XR_META_boundary_visibility",
};
#define SCENE_EXTENSION_COUNT (sizeof(scene_extensions) / sizeof(scene_extensions[0]))

static int is_scene_extension(const char *name) {
    for (size_t i = 0; i < SCENE_EXTENSION_COUNT; ++i)
        if (!strcmp(name, scene_extensions[i])) return 1;
    return 0;
}

// ---------------------------------------------------------------- pose math
static XrQuaternionf quat_mul(XrQuaternionf a, XrQuaternionf b) {
    return (XrQuaternionf){a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y, a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
                           a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w, a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z};
}
static XrVector3f quat_rotate(XrQuaternionf q, XrVector3f v) {
    XrQuaternionf p = {v.x, v.y, v.z, 0}, c = {-q.x, -q.y, -q.z, q.w};
    XrQuaternionf r = quat_mul(quat_mul(q, p), c);
    return (XrVector3f){r.x, r.y, r.z};
}
static XrPosef pose_mul(XrPosef a, XrPosef b) {  // a * b: b expressed in a's frame
    XrVector3f t = quat_rotate(a.orientation, b.position);
    return (XrPosef){quat_mul(a.orientation, b.orientation), {a.position.x + t.x, a.position.y + t.y, a.position.z + t.z}};
}
static XrPosef pose_inv(XrPosef a) {
    XrQuaternionf c = {-a.orientation.x, -a.orientation.y, -a.orientation.z, a.orientation.w};
    XrVector3f t = quat_rotate(c, a.position);
    return (XrPosef){c, {-t.x, -t.y, -t.z}};
}

// ---------------------------------------------------------------- entities
enum { ENT_ROOM, ENT_FLOOR, ENT_CEILING, ENT_WALL0, ENT_WALL1, ENT_WALL2, ENT_WALL3, ENT_FIXED_COUNT };
#define MAX_ENTITIES 64

typedef struct {
    XrSpace handle;       // fake handle given to the app
    XrUuidEXT uuid;
    XrPosef pose_in_stage;
    const char *label;    // semantic label, NULL if none
    XrRect2Df bounds;     // 2D bounds in the entity's local XY plane
    int has_bounds, is_room, is_anchor;
} scene_entity;

static pthread_mutex_t scene_lock = PTHREAD_MUTEX_INITIALIZER;
static scene_entity entities[MAX_ENTITIES];
static int entity_count;
static XrSpace scene_stage = XR_NULL_HANDLE;  // real STAGE space used to anchor everything
static XrSession scene_session = XR_NULL_HANDLE;
static uint64_t next_request = 0x5ce0000001ull;

#define FAKE_SPACE_TAG 0x5ce0000000000000ull
static int fake_space_index(XrSpace space) {
    uint64_t v = (uint64_t)(uintptr_t)space;
    if ((v & 0xfff0000000000000ull) != FAKE_SPACE_TAG) return -1;
    int i = (int)(v & 0xffff);
    return i < entity_count ? i : -1;
}
static XrSpace fake_space(int index) { return (XrSpace)(uintptr_t)(FAKE_SPACE_TAG | (uint64_t)index); }

static XrUuidEXT make_uuid(int index) {
    XrUuidEXT u;
    for (int i = 0; i < XR_UUID_SIZE_EXT; ++i) u.data[i] = (uint8_t)(0x5c ^ (i * 37) ^ (index * 101));
    u.data[6] = (uint8_t)((u.data[6] & 0x0f) | 0x40);  // version 4 style
    u.data[15] = (uint8_t)index;
    return u;
}
static int uuid_equal(const XrUuidEXT *a, const XrUuidEXT *b) { return !memcmp(a->data, b->data, XR_UUID_SIZE_EXT); }

static int add_entity(XrPosef pose, const char *label, float w, float h, int is_room, int is_anchor) {
    if (entity_count >= MAX_ENTITIES) return -1;
    int i = entity_count++;
    scene_entity *e = &entities[i];
    memset(e, 0, sizeof(*e));
    e->handle = fake_space(i);
    e->uuid = make_uuid(i);
    e->pose_in_stage = pose;
    e->label = label;
    e->has_bounds = w > 0 && h > 0;
    e->bounds = (XrRect2Df){{-w / 2, -h / 2}, {w, h}};
    e->is_room = is_room;
    e->is_anchor = is_anchor;
    return i;
}

// Build the room once the session exists: guardian rectangle -> floor/ceiling/walls.
static void build_room(void) {
    if (entity_count) return;
    float width = 0, depth = 0;
    PFN_xrGetReferenceSpaceBoundsRect bounds_fn =
        (PFN_xrGetReferenceSpaceBoundsRect)lookup(active_instance, "xrGetReferenceSpaceBoundsRect");
    XrExtent2Df bounds = {0, 0};
    if (bounds_fn && scene_session &&
        bounds_fn(scene_session, XR_REFERENCE_SPACE_TYPE_STAGE, &bounds) == XR_SUCCESS) {
        width = bounds.width;
        depth = bounds.height;
    }
    int from_guardian = width > 0 && depth > 0;
    LOG("scene: guardian STAGE bounds %.2f x %.2f m", width, depth);
    if (scene_width > 0) { width = scene_width; from_guardian = 0; }
    if (scene_depth > 0) { depth = scene_depth; from_guardian = 0; }
    if (width < scene_min_size) width = scene_min_size;
    if (depth < scene_min_size) depth = scene_min_size;
    float h = scene_height, s = 0.70710678f;
    LOG("scene: room %.2f x %.2f x %.2f m (%s)", width, depth, h, from_guardian ? "from guardian" : "configured/default size");
    add_entity((XrPosef){{0, 0, 0, 1}, {0, 0, 0}}, NULL, 0, 0, 1, 0);                         // ENT_ROOM
    add_entity((XrPosef){{-s, 0, 0, s}, {0, 0, 0}}, "FLOOR", width, depth, 0, 0);             // +Z (normal) -> +Y
    add_entity((XrPosef){{s, 0, 0, s}, {0, h, 0}}, "CEILING", width, depth, 0, 0);            // +Z -> -Y
    add_entity((XrPosef){{0, 0, 0, 1}, {0, h / 2, -depth / 2}}, "WALL_FACE", width, h, 0, 0); // faces +Z
    add_entity((XrPosef){{0, 1, 0, 0}, {0, h / 2, depth / 2}}, "WALL_FACE", width, h, 0, 0);  // faces -Z
    add_entity((XrPosef){{0, s, 0, s}, {-width / 2, h / 2, 0}}, "WALL_FACE", depth, h, 0, 0); // faces +X
    add_entity((XrPosef){{0, -s, 0, s}, {width / 2, h / 2, 0}}, "WALL_FACE", depth, h, 0, 0); // faces -X
}

// Supported components per entity.
static int entity_components(const scene_entity *e, XrSpaceComponentTypeFB *out) {
    int n = 0;
    out[n++] = XR_SPACE_COMPONENT_TYPE_LOCATABLE_FB;
    out[n++] = XR_SPACE_COMPONENT_TYPE_STORABLE_FB;
    if (e->is_room) {
        out[n++] = XR_SPACE_COMPONENT_TYPE_ROOM_LAYOUT_FB;
        out[n++] = XR_SPACE_COMPONENT_TYPE_SPACE_CONTAINER_FB;
    } else if (!e->is_anchor) {
        out[n++] = XR_SPACE_COMPONENT_TYPE_BOUNDED_2D_FB;
        out[n++] = XR_SPACE_COMPONENT_TYPE_SEMANTIC_LABELS_FB;
    }
    return n;
}
static int entity_has_component(const scene_entity *e, XrSpaceComponentTypeFB type) {
    XrSpaceComponentTypeFB list[8];
    int n = entity_components(e, list);
    for (int i = 0; i < n; ++i) if (list[i] == type) return 1;
    return 0;
}

// ---------------------------------------------------------------- queued events
#define MAX_EVENTS 64
static XrEventDataBuffer scene_events[MAX_EVENTS];
static int event_head, event_tail;
static void push_event(const void *event, size_t size) {
    if ((event_tail + 1) % MAX_EVENTS == event_head) return;
    memset(&scene_events[event_tail], 0, sizeof(XrEventDataBuffer));
    memcpy(&scene_events[event_tail], event, size);
    event_tail = (event_tail + 1) % MAX_EVENTS;
}
static int pop_event(XrEventDataBuffer *out) {  // caller holds scene_lock
    if (event_head == event_tail) return 0;
    memcpy(out, &scene_events[event_head], sizeof(XrEventDataBuffer));
    event_head = (event_head + 1) % MAX_EVENTS;
    return 1;
}

// ---------------------------------------------------------------- locating fake spaces
// Pose of a space in STAGE: fake spaces are stored in stage; real ones are located through the runtime.
static int stage_pose(XrSpace space, XrTime time, XrPosef *pose, XrSpaceLocationFlags *flags) {
    int i = fake_space_index(space);
    if (i >= 0) {
        *pose = entities[i].pose_in_stage;
        *flags = XR_SPACE_LOCATION_ORIENTATION_VALID_BIT | XR_SPACE_LOCATION_POSITION_VALID_BIT |
                 XR_SPACE_LOCATION_ORIENTATION_TRACKED_BIT | XR_SPACE_LOCATION_POSITION_TRACKED_BIT;
        return 1;
    }
    PFN_xrLocateSpace locate = (PFN_xrLocateSpace)lookup(active_instance, "xrLocateSpace");
    if (!locate || !scene_stage) return 0;
    XrSpaceLocation loc = {XR_TYPE_SPACE_LOCATION, NULL, 0, {{0, 0, 0, 1}, {0, 0, 0}}};
    if (XR_FAILED(locate(space, scene_stage, time, &loc))) return 0;
    *pose = loc.pose;
    *flags = loc.locationFlags;
    return 1;
}
// Locate `space` in `base` when either is fake. Returns 0 if neither is fake (caller forwards).
static int locate_fake(XrSpace space, XrSpace base, XrTime time, XrPosef *pose, XrSpaceLocationFlags *flags) {
    if (fake_space_index(space) < 0 && fake_space_index(base) < 0) return 0;
    XrPosef s, b;
    XrSpaceLocationFlags fs = 0, fb = 0;
    if (!stage_pose(space, time, &s, &fs) || !stage_pose(base, time, &b, &fb)) {
        *flags = 0;
        *pose = (XrPosef){{0, 0, 0, 1}, {0, 0, 0}};
        return 1;
    }
    *pose = pose_mul(pose_inv(b), s);
    *flags = fs & fb;
    return 1;
}

// ---------------------------------------------------------------- emulated entry points
static XRAPI_ATTR XrResult XRAPI_CALL emu_query_spaces(XrSession session, const XrSpaceQueryInfoBaseHeaderFB *info,
                                                       XrAsyncRequestIdFB *request) {
    if (!info || !request) return XR_ERROR_VALIDATION_FAILURE;
    pthread_mutex_lock(&scene_lock);
    scene_session = session;
    build_room();
    *request = next_request++;
    XrEventDataSpaceQueryResultsAvailableFB avail = {XR_TYPE_EVENT_DATA_SPACE_QUERY_RESULTS_AVAILABLE_FB, NULL, *request};
    XrEventDataSpaceQueryCompleteFB done = {XR_TYPE_EVENT_DATA_SPACE_QUERY_COMPLETE_FB, NULL, *request, XR_SUCCESS};
    push_event(&avail, sizeof(avail));
    push_event(&done, sizeof(done));
    pthread_mutex_unlock(&scene_lock);
    return XR_SUCCESS;
}

// Query filters are kept per request so retrieve can apply them.
typedef struct {
    XrAsyncRequestIdFB id;
    int has_component;
    XrSpaceComponentTypeFB component;
    uint32_t uuid_count;
    XrUuidEXT uuids[64];
} query_filter;
static query_filter filters[16];
static int filter_next;

static void store_filter(XrAsyncRequestIdFB id, const XrSpaceQueryInfoFB *q) {
    query_filter *f = &filters[filter_next++ % 16];
    memset(f, 0, sizeof(*f));
    f->id = id;
    const XrSpaceFilterInfoBaseHeaderFB *flt = q->filter;
    if (flt && flt->type == XR_TYPE_SPACE_COMPONENT_FILTER_INFO_FB) {
        f->has_component = 1;
        f->component = ((const XrSpaceComponentFilterInfoFB *)flt)->componentType;
    } else if (flt && flt->type == XR_TYPE_SPACE_UUID_FILTER_INFO_FB) {
        const XrSpaceUuidFilterInfoFB *u = (const XrSpaceUuidFilterInfoFB *)flt;
        f->uuid_count = u->uuidCount < 64 ? u->uuidCount : 64;
        for (uint32_t i = 0; i < f->uuid_count; ++i) f->uuids[i] = u->uuids[i];
    }
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_query_spaces_filtered(XrSession session, const XrSpaceQueryInfoBaseHeaderFB *info,
                                                                XrAsyncRequestIdFB *request) {
    XrResult r = emu_query_spaces(session, info, request);
    if (XR_SUCCEEDED(r) && info->type == XR_TYPE_SPACE_QUERY_INFO_FB) {
        pthread_mutex_lock(&scene_lock);
        store_filter(*request, (const XrSpaceQueryInfoFB *)info);
        const XrSpaceQueryInfoFB *q = (const XrSpaceQueryInfoFB *)info;
        LOG("scene: query request=%llu filter=%d", (unsigned long long)*request, q->filter ? q->filter->type : 0);
        pthread_mutex_unlock(&scene_lock);
    }
    return r;
}

static int filter_matches(const query_filter *f, const scene_entity *e) {
    if (!f) return 1;
    if (f->has_component) return entity_has_component(e, f->component);
    if (f->uuid_count) {
        for (uint32_t i = 0; i < f->uuid_count; ++i) if (uuid_equal(&f->uuids[i], &e->uuid)) return 1;
        return 0;
    }
    return 1;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_discover_spaces(XrSession session, const XrSpaceDiscoveryInfoMETA *info,
                                                          XrAsyncRequestIdFB *request) {
    if (!info || !request) return XR_ERROR_VALIDATION_FAILURE;
    pthread_mutex_lock(&scene_lock);
    scene_session = session;
    build_room();
    *request = next_request++;

    query_filter *f = &filters[filter_next++ % 16];
    memset(f, 0, sizeof(*f));
    f->id = *request;
    if (info->filters && info->filterCount) {
        for (uint32_t i = 0; i < info->filterCount; ++i) {
            const XrSpaceFilterBaseHeaderMETA *flt = info->filters[i];
            if (!flt) continue;
            if (flt->type == XR_TYPE_SPACE_FILTER_COMPONENT_META) {
                f->has_component = 1;
                f->component = ((const XrSpaceFilterComponentMETA *)flt)->componentType;
            } else if (flt->type == XR_TYPE_SPACE_FILTER_UUID_META) {
                const XrSpaceFilterUuidMETA *u = (const XrSpaceFilterUuidMETA *)flt;
                f->uuid_count = u->uuidCount < 64 ? u->uuidCount : 64;
                for (uint32_t k = 0; k < f->uuid_count; ++k) f->uuids[k] = u->uuids[k];
            }
        }
    }

    XrEventDataSpaceDiscoveryResultsAvailableMETA avail = {
        XR_TYPE_EVENT_DATA_SPACE_DISCOVERY_RESULTS_AVAILABLE_META, NULL, *request
    };
    XrEventDataSpaceDiscoveryCompleteMETA done = {
        XR_TYPE_EVENT_DATA_SPACE_DISCOVERY_COMPLETE_META, NULL, *request, XR_SUCCESS
    };
    push_event(&avail, sizeof(avail));
    push_event(&done, sizeof(done));
    LOG("scene: discover spaces request=%llu filterCount=%u", (unsigned long long)*request, info->filterCount);
    pthread_mutex_unlock(&scene_lock);
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_retrieve_discovery_results(XrSession session, XrAsyncRequestIdFB request,
                                                                     XrSpaceDiscoveryResultsMETA *results) {
    (void)session;
    if (!results) return XR_ERROR_VALIDATION_FAILURE;
    pthread_mutex_lock(&scene_lock);
    const query_filter *f = NULL;
    for (int i = 0; i < 16; ++i) if (filters[i].id == request) f = &filters[i];
    uint32_t n = 0;
    for (int i = 0; i < entity_count; ++i) {
        if (!filter_matches(f, &entities[i])) continue;
        if (results->resultCapacityInput && n < results->resultCapacityInput && results->results) {
            results->results[n].space = entities[i].handle;
            results->results[n].uuid = entities[i].uuid;
        }
        ++n;
    }
    results->resultCountOutput = n;
    LOG("scene: retrieve discovery results request=%llu found=%u returned=%u",
        (unsigned long long)request, n, results->resultCapacityInput ? (results->resultCapacityInput < n ? results->resultCapacityInput : n) : 0);
    pthread_mutex_unlock(&scene_lock);
    return (results->resultCapacityInput && results->resultCapacityInput < n) ? XR_ERROR_SIZE_INSUFFICIENT : XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_request_boundary_visibility(XrSession session, XrBoundaryVisibilityMETA boundaryVisibility) {
    (void)session; (void)boundaryVisibility;
    LOG("scene: boundary visibility requested: %d", (int)boundaryVisibility);
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_retrieve_results(XrSession session, XrAsyncRequestIdFB request,
                                                           XrSpaceQueryResultsFB *results) {
    (void)session;
    if (!results) return XR_ERROR_VALIDATION_FAILURE;
    pthread_mutex_lock(&scene_lock);
    const query_filter *f = NULL;
    for (int i = 0; i < 16; ++i) if (filters[i].id == request) f = &filters[i];
    uint32_t n = 0;
    for (int i = 0; i < entity_count; ++i) {
        if (!filter_matches(f, &entities[i])) continue;
        if (results->resultCapacityInput && n < results->resultCapacityInput && results->results) {
            results->results[n].space = entities[i].handle;
            results->results[n].uuid = entities[i].uuid;
        }
        ++n;
    }
    results->resultCountOutput = n;
    pthread_mutex_unlock(&scene_lock);
    return (results->resultCapacityInput && results->resultCapacityInput < n) ? XR_ERROR_SIZE_INSUFFICIENT : XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_get_uuid(XrSpace space, XrUuidEXT *uuid) {
    int i = fake_space_index(space);
    if (i < 0 || !uuid) return XR_ERROR_HANDLE_INVALID;
    *uuid = entities[i].uuid;
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_enumerate_components(XrSpace space, uint32_t capacity, uint32_t *count,
                                                               XrSpaceComponentTypeFB *types) {
    int i = fake_space_index(space);
    if (i < 0 || !count) return XR_ERROR_HANDLE_INVALID;
    XrSpaceComponentTypeFB list[8];
    int n = entity_components(&entities[i], list);
    *count = (uint32_t)n;
    if (!capacity) return XR_SUCCESS;
    if (capacity < (uint32_t)n) return XR_ERROR_SIZE_INSUFFICIENT;
    for (int k = 0; k < n; ++k) types[k] = list[k];
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_set_component_status(XrSpace space, const XrSpaceComponentStatusSetInfoFB *info,
                                                               XrAsyncRequestIdFB *request) {
    int i = fake_space_index(space);
    if (i < 0 || !info || !request) return XR_ERROR_HANDLE_INVALID;
    if (!entity_has_component(&entities[i], info->componentType)) return XR_ERROR_SPACE_COMPONENT_NOT_SUPPORTED_FB;
    pthread_mutex_lock(&scene_lock);
    *request = next_request++;
    XrEventDataSpaceSetStatusCompleteFB e = {XR_TYPE_EVENT_DATA_SPACE_SET_STATUS_COMPLETE_FB, NULL, *request, XR_SUCCESS,
                                             space, entities[i].uuid, info->componentType, info->enabled};
    push_event(&e, sizeof(e));
    pthread_mutex_unlock(&scene_lock);
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_get_component_status(XrSpace space, XrSpaceComponentTypeFB type,
                                                               XrSpaceComponentStatusFB *status) {
    int i = fake_space_index(space);
    if (i < 0 || !status) return XR_ERROR_HANDLE_INVALID;
    if (!entity_has_component(&entities[i], type)) return XR_ERROR_SPACE_COMPONENT_NOT_SUPPORTED_FB;
    status->enabled = XR_TRUE;
    status->changePending = XR_FALSE;
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_get_container(XrSession session, XrSpace space, XrSpaceContainerFB *container) {
    (void)session;
    int i = fake_space_index(space);
    if (i < 0 || !container) return XR_ERROR_HANDLE_INVALID;
    if (!entities[i].is_room) return XR_ERROR_SPACE_COMPONENT_NOT_ENABLED_FB;
    uint32_t n = ENT_FIXED_COUNT - 1;  // everything except the room itself
    container->uuidCountOutput = n;
    if (!container->uuidCapacityInput) return XR_SUCCESS;
    if (container->uuidCapacityInput < n) return XR_ERROR_SIZE_INSUFFICIENT;
    for (uint32_t k = 0; k < n; ++k) container->uuids[k] = entities[ENT_FLOOR + k].uuid;
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_get_room_layout(XrSession session, XrSpace space, XrRoomLayoutFB *layout) {
    (void)session;
    int i = fake_space_index(space);
    if (i < 0 || !layout) return XR_ERROR_HANDLE_INVALID;
    if (!entities[i].is_room) return XR_ERROR_SPACE_COMPONENT_NOT_ENABLED_FB;
    layout->floorUuid = entities[ENT_FLOOR].uuid;
    layout->ceilingUuid = entities[ENT_CEILING].uuid;
    layout->wallUuidCountOutput = 4;
    if (!layout->wallUuidCapacityInput) return XR_SUCCESS;
    if (layout->wallUuidCapacityInput < 4) return XR_ERROR_SIZE_INSUFFICIENT;
    for (int k = 0; k < 4; ++k) layout->wallUuids[k] = entities[ENT_WALL0 + k].uuid;
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_get_bounding_box_2d(XrSession session, XrSpace space, XrRect2Df *rect) {
    (void)session;
    int i = fake_space_index(space);
    if (i < 0 || !rect) return XR_ERROR_HANDLE_INVALID;
    if (!entities[i].has_bounds) return XR_ERROR_SPACE_COMPONENT_NOT_ENABLED_FB;
    *rect = entities[i].bounds;
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_get_bounding_box_3d(XrSession session, XrSpace space, XrRect3DfFB *box) {
    (void)session; (void)space; (void)box;
    return XR_ERROR_SPACE_COMPONENT_NOT_ENABLED_FB;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_get_boundary_2d(XrSession session, XrSpace space, XrBoundary2DFB *boundary) {
    (void)session;
    int i = fake_space_index(space);
    if (i < 0 || !boundary) return XR_ERROR_HANDLE_INVALID;
    if (!entities[i].has_bounds) return XR_ERROR_SPACE_COMPONENT_NOT_ENABLED_FB;
    boundary->vertexCountOutput = 4;
    if (!boundary->vertexCapacityInput) return XR_SUCCESS;
    if (boundary->vertexCapacityInput < 4) return XR_ERROR_SIZE_INSUFFICIENT;
    const XrRect2Df b = entities[i].bounds;
    boundary->vertices[0] = (XrVector2f){b.offset.x, b.offset.y};
    boundary->vertices[1] = (XrVector2f){b.offset.x + b.extent.width, b.offset.y};
    boundary->vertices[2] = (XrVector2f){b.offset.x + b.extent.width, b.offset.y + b.extent.height};
    boundary->vertices[3] = (XrVector2f){b.offset.x, b.offset.y + b.extent.height};
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_get_semantic_labels(XrSession session, XrSpace space, XrSemanticLabelsFB *labels) {
    (void)session;
    int i = fake_space_index(space);
    if (i < 0 || !labels) return XR_ERROR_HANDLE_INVALID;
    if (!entities[i].label) return XR_ERROR_SPACE_COMPONENT_NOT_ENABLED_FB;
    uint32_t n = (uint32_t)strlen(entities[i].label);
    labels->bufferCountOutput = n;
    LOG("scene: get semantic labels entity=%d label=%s cap=%u", i, entities[i].label, labels->bufferCapacityInput);
    if (!labels->bufferCapacityInput) return XR_SUCCESS;
    if (labels->bufferCapacityInput < n) return XR_ERROR_SIZE_INSUFFICIENT;
    memcpy(labels->buffer, entities[i].label, n);
    if (labels->bufferCapacityInput > n) labels->buffer[n] = '\0';
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_request_scene_capture(XrSession session, const XrSceneCaptureRequestInfoFB *info,
                                                                XrAsyncRequestIdFB *request) {
    (void)info;
    if (!request) return XR_ERROR_VALIDATION_FAILURE;
    pthread_mutex_lock(&scene_lock);
    scene_session = session;
    build_room();
    *request = next_request++;
    XrEventDataSceneCaptureCompleteFB e = {XR_TYPE_EVENT_DATA_SCENE_CAPTURE_COMPLETE_FB, NULL, *request, XR_SUCCESS};
    push_event(&e, sizeof(e));
    pthread_mutex_unlock(&scene_lock);
    LOG("scene: capture requested -> using guardian room");
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_create_anchor(XrSession session, const XrSpatialAnchorCreateInfoFB *info,
                                                        XrAsyncRequestIdFB *request) {
    if (!info || !request) return XR_ERROR_VALIDATION_FAILURE;
    pthread_mutex_lock(&scene_lock);
    scene_session = session;
    build_room();
    XrPosef base = {{0, 0, 0, 1}, {0, 0, 0}};
    XrSpaceLocationFlags flags = 0;
    stage_pose(info->space, info->time, &base, &flags);
    int i = add_entity(pose_mul(base, info->poseInSpace), NULL, 0, 0, 0, 1);
    *request = next_request++;
    XrEventDataSpatialAnchorCreateCompleteFB e = {XR_TYPE_EVENT_DATA_SPATIAL_ANCHOR_CREATE_COMPLETE_FB, NULL, *request,
        i >= 0 ? XR_SUCCESS : XR_ERROR_LIMIT_REACHED, i >= 0 ? entities[i].handle : XR_NULL_HANDLE,
        i >= 0 ? entities[i].uuid : (XrUuidEXT){{0}}};
    push_event(&e, sizeof(e));
    pthread_mutex_unlock(&scene_lock);
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_save_space(XrSession session, const XrSpaceSaveInfoFB *info, XrAsyncRequestIdFB *request) {
    (void)session;
    if (!info || !request) return XR_ERROR_VALIDATION_FAILURE;
    int i = fake_space_index(info->space);
    pthread_mutex_lock(&scene_lock);
    *request = next_request++;
    XrEventDataSpaceSaveCompleteFB e = {XR_TYPE_EVENT_DATA_SPACE_SAVE_COMPLETE_FB, NULL, *request, XR_SUCCESS, info->space,
                                        i >= 0 ? entities[i].uuid : (XrUuidEXT){{0}}, info->location};
    push_event(&e, sizeof(e));
    pthread_mutex_unlock(&scene_lock);
    return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_erase_space(XrSession session, const XrSpaceEraseInfoFB *info, XrAsyncRequestIdFB *request) {
    (void)session;
    if (!info || !request) return XR_ERROR_VALIDATION_FAILURE;
    int i = fake_space_index(info->space);
    pthread_mutex_lock(&scene_lock);
    *request = next_request++;
    XrEventDataSpaceEraseCompleteFB e = {XR_TYPE_EVENT_DATA_SPACE_ERASE_COMPLETE_FB, NULL, *request, XR_SUCCESS, info->space,
                                         i >= 0 ? entities[i].uuid : (XrUuidEXT){{0}}, info->location};
    push_event(&e, sizeof(e));
    pthread_mutex_unlock(&scene_lock);
    return XR_SUCCESS;
}

static PFN_xrVoidFunction scene_emulation(const char *name) {
    if (!emulate_scene) return NULL;
    static const struct { const char *name; PFN_xrVoidFunction fn; } table[] = {
        {"xrQuerySpacesFB", (PFN_xrVoidFunction)emu_query_spaces_filtered},
        {"xrRetrieveSpaceQueryResultsFB", (PFN_xrVoidFunction)emu_retrieve_results},
        {"xrGetSpaceUuidFB", (PFN_xrVoidFunction)emu_get_uuid},
        {"xrEnumerateSpaceSupportedComponentsFB", (PFN_xrVoidFunction)emu_enumerate_components},
        {"xrSetSpaceComponentStatusFB", (PFN_xrVoidFunction)emu_set_component_status},
        {"xrGetSpaceComponentStatusFB", (PFN_xrVoidFunction)emu_get_component_status},
        {"xrGetSpaceContainerFB", (PFN_xrVoidFunction)emu_get_container},
        {"xrGetSpaceRoomLayoutFB", (PFN_xrVoidFunction)emu_get_room_layout},
        {"xrGetSpaceBoundingBox2DFB", (PFN_xrVoidFunction)emu_get_bounding_box_2d},
        {"xrGetSpaceBoundingBox3DFB", (PFN_xrVoidFunction)emu_get_bounding_box_3d},
        {"xrGetSpaceBoundary2DFB", (PFN_xrVoidFunction)emu_get_boundary_2d},
        {"xrGetSpaceSemanticLabelsFB", (PFN_xrVoidFunction)emu_get_semantic_labels},
        {"xrRequestSceneCaptureFB", (PFN_xrVoidFunction)emu_request_scene_capture},
        {"xrCreateSpatialAnchorFB", (PFN_xrVoidFunction)emu_create_anchor},
        {"xrSaveSpaceFB", (PFN_xrVoidFunction)emu_save_space},
        {"xrEraseSpaceFB", (PFN_xrVoidFunction)emu_erase_space},
        {"xrDiscoverSpacesMETA", (PFN_xrVoidFunction)emu_discover_spaces},
        {"xrRetrieveSpaceDiscoveryResultsMETA", (PFN_xrVoidFunction)emu_retrieve_discovery_results},
        {"xrSetSpaceComponentStatusPrivilegedFB", (PFN_xrVoidFunction)emu_set_component_status},
        {"xrRequestBoundaryVisibilityMETA", (PFN_xrVoidFunction)emu_request_boundary_visibility},
    };
    for (size_t i = 0; i < sizeof(table) / sizeof(table[0]); ++i)
        if (!strcmp(name, table[i].name)) return table[i].fn;
    return NULL;
}

// Called from the xrCreateSession hook.
static void scene_on_create_session(XrSession session) {
    if (!emulate_scene) return;
    PFN_xrCreateReferenceSpace create = (PFN_xrCreateReferenceSpace)lookup(active_instance, "xrCreateReferenceSpace");
    XrReferenceSpaceCreateInfo ci = {XR_TYPE_REFERENCE_SPACE_CREATE_INFO, NULL, XR_REFERENCE_SPACE_TYPE_STAGE,
                                     {{0, 0, 0, 1}, {0, 0, 0}}};
    scene_session = session;
    if (create && XR_SUCCEEDED(create(session, &ci, &scene_stage))) LOG("scene: emulation active (anchored to STAGE)");
}
