// SPDX-License-Identifier: GPL-3.0-only
// Compile the actual emulation with a fake runtime. No Android, SteamVR or device is involved.
#define _GNU_SOURCE
#include <openxr/openxr.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#define LOG(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } while (0)
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAIL %d: %s\n", __LINE__, #c); return 1; } } while (0)
#define NEAR(a,b) (fabsf((a)-(b)) < 0.0001f)
static XrInstance active_instance = (XrInstance)(uintptr_t)1;
static float scene_width, scene_depth, scene_height = 2.5f;
#define STAGE ((XrSpace)(uintptr_t)42)
#define LOCAL ((XrSpace)(uintptr_t)43)
#define SESSION ((XrSession)(uintptr_t)1)
static XrResult XRAPI_CALL fake_create(XrSession s, const XrReferenceSpaceCreateInfo *info, XrSpace *out) {
    (void)s; (void)info; *out = STAGE; return XR_SUCCESS;
}
static XrResult XRAPI_CALL fake_bounds(XrSession s, XrReferenceSpaceType type, XrExtent2Df *out) {
    (void)s; (void)type; *out = (XrExtent2Df){1,1}; return XR_SUCCESS;
}
static XrResult XRAPI_CALL fake_locate(XrSpace s, XrSpace base, XrTime time, XrSpaceLocation *out) {
    (void)base; (void)time;
    if (s != STAGE && s != LOCAL) return XR_ERROR_HANDLE_INVALID;
    out->pose = s == STAGE ? (XrPosef){{0,0,0,1},{0,0,0}} : (XrPosef){{0,0.70710678f,0,0.70710678f},{10,0,5}};
    out->locationFlags = XR_SPACE_LOCATION_ORIENTATION_VALID_BIT | XR_SPACE_LOCATION_POSITION_VALID_BIT |
                        XR_SPACE_LOCATION_ORIENTATION_TRACKED_BIT | XR_SPACE_LOCATION_POSITION_TRACKED_BIT;
    return XR_SUCCESS;
}
static PFN_xrVoidFunction lookup(XrInstance i, const char *name) {
    (void)i;
    if (!strcmp(name,"xrCreateReferenceSpace")) return (PFN_xrVoidFunction)fake_create;
    if (!strcmp(name,"xrGetReferenceSpaceBoundsRect")) return (PFN_xrVoidFunction)fake_bounds;
    if (!strcmp(name,"xrLocateSpace")) return (PFN_xrVoidFunction)fake_locate;
    return NULL;
}
#include "scene_emu.c"
static int check_boundary(int i, unsigned count) {
    XrBoundary2DFB b = {.type=XR_TYPE_BOUNDARY_2D_FB};
    CHECK(emu_get_boundary_2d(SESSION,entities[i].handle,&b) == XR_SUCCESS && b.vertexCountOutput == count);
    XrVector2f vertices[MAX_OUTLINE];
    b.vertexCapacityInput=count-1; b.vertices=vertices;
    CHECK(emu_get_boundary_2d(SESSION,entities[i].handle,&b) == XR_ERROR_SIZE_INSUFFICIENT);
    b.vertexCapacityInput=count;
    CHECK(emu_get_boundary_2d(SESSION,entities[i].handle,&b) == XR_SUCCESS);
    for (unsigned k=0;k<count;++k) {
        XrVector3f v = quat_rotate(entities[i].pose_in_stage.orientation,(XrVector3f){vertices[k].x,vertices[k].y,0});
        if (i == ENT_FLOOR || i == ENT_CEILING) {
            CHECK(NEAR(v.y,0));
            XrVector3f n=quat_rotate(entities[i].pose_in_stage.orientation,(XrVector3f){0,0,1});
            CHECK(NEAR(n.y,i==ENT_FLOOR?1:-1));
        }
    }
    return 0;
}
static int check_mesh(int mesh_ok) {
    int mesh=-1;
    for(int i=0;i<entity_count;++i) if(entities[i].vertex_count) mesh=i;
    CHECK((mesh >= 0) == mesh_ok);
    if(mesh<0) return 0;
    XrSpaceTriangleMeshMETA m={.type=XR_TYPE_SPACE_TRIANGLE_MESH_META};
    CHECK(emu_get_triangle_mesh(entities[mesh].handle,NULL,&m)==XR_SUCCESS && m.vertexCountOutput==4 && m.indexCountOutput==6);
    XrVector3f v[4]; uint32_t indices[6];
    m.vertexCapacityInput=3; m.vertices=v;
    CHECK(emu_get_triangle_mesh(entities[mesh].handle,NULL,&m)==XR_ERROR_SIZE_INSUFFICIENT);
    m.vertexCapacityInput=4; m.indexCapacityInput=5; m.indices=indices;
    CHECK(emu_get_triangle_mesh(entities[mesh].handle,NULL,&m)==XR_ERROR_SIZE_INSUFFICIENT);
    m.indexCapacityInput=6;
    CHECK(emu_get_triangle_mesh(entities[mesh].handle,NULL,&m)==XR_SUCCESS && indices[5]==3 && v[2].x==4);
    m.vertexCapacityInput=0; m.vertices=NULL;
    CHECK(emu_get_triangle_mesh(entities[mesh].handle,NULL,&m)==XR_SUCCESS);
    return 0;
}
static int check_discovery(void) {
    XrSpaceFilterComponentMETA component={XR_TYPE_SPACE_FILTER_COMPONENT_META,NULL,XR_SPACE_COMPONENT_TYPE_ROOM_LAYOUT_FB};
    const XrSpaceFilterBaseHeaderMETA *fs[]={(const XrSpaceFilterBaseHeaderMETA *)&component};
    XrSpaceDiscoveryInfoMETA info={XR_TYPE_SPACE_DISCOVERY_INFO_META,NULL,1,fs};
    XrAsyncRequestIdFB req;
    CHECK(emu_discover_spaces(SESSION,&info,&req)==XR_SUCCESS);
    XrEventDataBuffer ev;
    CHECK(pop_event(&ev) && ev.type==XR_TYPE_EVENT_DATA_SPACE_DISCOVERY_RESULTS_AVAILABLE_META);
    CHECK(((XrEventDataSpaceDiscoveryResultsAvailableMETA *)&ev)->requestId==req);
    CHECK(pop_event(&ev) && ev.type==XR_TYPE_EVENT_DATA_SPACE_DISCOVERY_COMPLETE_META);
    CHECK(((XrEventDataSpaceDiscoveryCompleteMETA *)&ev)->requestId==req);
    CHECK(((XrEventDataSpaceDiscoveryCompleteMETA *)&ev)->result==XR_SUCCESS && !pop_event(&ev));
    XrSpaceDiscoveryResultsMETA results={.type=XR_TYPE_SPACE_DISCOVERY_RESULTS_META};
    CHECK(emu_retrieve_discovery(SESSION,req,&results)==XR_SUCCESS && results.resultCountOutput==1);
    XrSpaceDiscoveryResultMETA values[MAX_ENTITIES];
    results.resultCapacityInput=MAX_ENTITIES; results.results=values;
    CHECK(emu_retrieve_discovery(SESSION,req,&results)==XR_SUCCESS && values[0].space==entities[0].handle);
    // UUID and component filters intersect; unknown filters do not restrict discovery.
    XrUuidEXT uuid=entities[ENT_FLOOR].uuid;
    XrSpaceFilterUuidMETA u={XR_TYPE_SPACE_FILTER_UUID_META,NULL,1,&uuid};
    XrSpaceFilterBaseHeaderMETA unknown={(XrStructureType)123,NULL};
    const XrSpaceFilterBaseHeaderMETA *both[]={(const XrSpaceFilterBaseHeaderMETA *)&u,(const XrSpaceFilterBaseHeaderMETA *)&component,&unknown};
    info.filterCount=3; info.filters=both;
    CHECK(emu_discover_spaces(SESSION,&info,&req)==XR_SUCCESS);
    results.resultCapacityInput=0;
    CHECK(emu_retrieve_discovery(SESSION,req,&results)==XR_SUCCESS && results.resultCountOutput==0);
    component.componentType=XR_SPACE_COMPONENT_TYPE_BOUNDED_2D_FB;
    CHECK(emu_discover_spaces(SESSION,&info,&req)==XR_SUCCESS);
    CHECK(emu_retrieve_discovery(SESSION,req,&results)==XR_SUCCESS && results.resultCountOutput==1);
    info.filterCount=0; info.filters=NULL;
    CHECK(emu_discover_spaces(SESSION,&info,&req)==XR_SUCCESS);
    results.resultCapacityInput=1;
    CHECK(emu_retrieve_discovery(SESSION,req,&results)==XR_ERROR_SIZE_INSUFFICIENT);
    CHECK(emu_retrieve_discovery(SESSION,req+1000,&results)==XR_ERROR_VALIDATION_FAILURE);
    // FB query uses the same UUIDs, two-call semantics and max-result limit.
    XrSpaceUuidFilterInfoFB fb_u={XR_TYPE_SPACE_UUID_FILTER_INFO_FB,NULL,1,&uuid};
    XrSpaceQueryInfoFB q={.type=XR_TYPE_SPACE_QUERY_INFO_FB,.maxResultCount=MAX_ENTITIES,.filter=(const XrSpaceFilterInfoBaseHeaderFB *)&fb_u};
    CHECK(emu_query_spaces_filtered(SESSION,(const XrSpaceQueryInfoBaseHeaderFB *)&q,&req)==XR_SUCCESS);
    XrSpaceQueryResultsFB fb_results={.type=XR_TYPE_SPACE_QUERY_RESULTS_FB};
    CHECK(emu_retrieve_results(SESSION,req,&fb_results)==XR_SUCCESS && fb_results.resultCountOutput==1);
    XrSpaceQueryResultFB fb_values[MAX_ENTITIES]; fb_results.results=fb_values; fb_results.resultCapacityInput=MAX_ENTITIES;
    CHECK(emu_retrieve_results(SESSION,req,&fb_results)==XR_SUCCESS && uuid_equal(&fb_values[0].uuid,&uuid));
    q.filter=NULL; q.maxResultCount=2;
    CHECK(emu_query_spaces_filtered(SESSION,(const XrSpaceQueryInfoBaseHeaderFB *)&q,&req)==XR_SUCCESS);
    fb_results.resultCapacityInput=1;
    CHECK(emu_retrieve_results(SESSION,req,&fb_results)==XR_ERROR_SIZE_INSUFFICIENT && fb_results.resultCountOutput==2);
    while(pop_event(&ev)) {}
    // Queue exhaustion must return an error before accepting a request whose events would be lost.
    for(int i=0;i<31;++i) CHECK(emu_discover_spaces(SESSION,&info,&req)==XR_SUCCESS);
    CHECK(emu_discover_spaces(SESSION,&info,&req)==XR_ERROR_LIMIT_REACHED);
    while(pop_event(&ev)) {}
    return 0;
}
static int check_room(int expect_walls, int mesh_ok) {
    CHECK(room_from_file && wall_count==expect_walls && room_universe==1234);
    CHECK(!check_boundary(ENT_FLOOR,5) && !check_boundary(ENT_CEILING,5));
    // Area centroid of this asymmetric five-point polygon, rather than its bounding rectangle centre.
    CHECK(NEAR(entities[ENT_FLOOR].pose_in_stage.position.x,2));
    CHECK(NEAR(entities[ENT_FLOOR].pose_in_stage.position.z,37.0f/21.0f));
    XrRoomLayoutFB layout={.type=XR_TYPE_ROOM_LAYOUT_FB};
    CHECK(emu_get_room_layout(SESSION,entities[0].handle,&layout)==XR_SUCCESS && layout.wallUuidCountOutput==(unsigned)expect_walls);
    XrUuidEXT walls[MAX_ENTITIES]; layout.wallUuids=walls; layout.wallUuidCapacityInput=1;
    CHECK(emu_get_room_layout(SESSION,entities[0].handle,&layout)==XR_ERROR_SIZE_INSUFFICIENT);
    layout.wallUuidCapacityInput=MAX_ENTITIES;
    CHECK(emu_get_room_layout(SESSION,entities[0].handle,&layout)==XR_SUCCESS);
    XrVector3f normal=quat_rotate(entities[ENT_WALL0].pose_in_stage.orientation,(XrVector3f){0,0,1});
    CHECK(NEAR(normal.x,0) && NEAR(normal.z,1));
    CHECK(!check_boundary(ENT_WALL0,4));
    for(int i=ENT_WALL0;i<ENT_WALL0+wall_count;++i) {
        scene_entity *e=&entities[i];
        XrVector3f n=quat_rotate(e->pose_in_stage.orientation,(XrVector3f){0,0,1});
        XrVector3f tangent=quat_rotate(e->pose_in_stage.orientation,(XrVector3f){1,0,0});
        CHECK(NEAR(n.x,-tangent.z) && NEAR(n.z,tangent.x));
        CHECK(uuid_equal(&walls[i-ENT_WALL0],&e->uuid));
    }
    int volume=-1,plane=-1;
    for(int i=0;i<entity_count;++i) {
        if(entities[i].has_bounds3) volume=i;
        if(!strcmp(entities[i].label,"WINDOW_FRAME")) plane=i;
    }
    CHECK(volume>=0 && plane>=0);
    XrRect3DfFB box;
    CHECK(emu_get_bounding_box_3d(SESSION,entities[volume].handle,&box)==XR_SUCCESS);
    CHECK(NEAR(box.offset.x,-1) && NEAR(box.offset.y,-0.5f) && NEAR(box.offset.z,-0.75f));
    CHECK(NEAR(box.extent.width,2) && NEAR(box.extent.height,1) && NEAR(box.extent.depth,0.75f));
    normal=quat_rotate(entities[volume].pose_in_stage.orientation,(XrVector3f){0,0,1});
    CHECK(NEAR(normal.y,1));
    XrVector3f bottom=quat_rotate(entities[volume].pose_in_stage.orientation,(XrVector3f){0,0,-0.75f});
    CHECK(NEAR(entities[volume].pose_in_stage.position.y+bottom.y,0));
    XrVector3f axis=quat_rotate(entities[volume].pose_in_stage.orientation,(XrVector3f){1,0,0});
    CHECK(NEAR(axis.x,0) && NEAR(axis.z,-1));
    CHECK(NEAR(entities[plane].pose_in_stage.position.x,1) && NEAR(entities[plane].bounds.extent.height,1.2f));
    // a current SDK names what it knows and accepts TABLE / invisible walls: labels as stored
    XrSemanticLabelsSupportInfoFB support={XR_TYPE_SEMANTIC_LABELS_SUPPORT_INFO_FB,NULL,
        XR_SEMANTIC_LABELS_SUPPORT_ACCEPT_DESK_TO_TABLE_MIGRATION_BIT_FB|
        XR_SEMANTIC_LABELS_SUPPORT_ACCEPT_INVISIBLE_WALL_FACE_BIT_FB,
        "TABLE,OTHER,FLOOR,CEILING,WALL_FACE,INVISIBLE_WALL_FACE,WINDOW_FRAME"};
    XrSemanticLabelsFB labels={.type=XR_TYPE_SEMANTIC_LABELS_FB,.next=&support};
    CHECK(emu_get_semantic_labels(SESSION,entities[volume].handle,&labels)==XR_SUCCESS && labels.bufferCountOutput==12);
    char text[256]; labels.buffer=text; labels.bufferCapacityInput=2;
    CHECK(emu_get_semantic_labels(SESSION,entities[volume].handle,&labels)==XR_ERROR_SIZE_INSUFFICIENT);
    labels.bufferCapacityInput=sizeof(text);
    CHECK(emu_get_semantic_labels(SESSION,entities[volume].handle,&labels)==XR_SUCCESS && !strcmp(text,"TABLE,OTHER"));
    // an SDK without the migration bit gets DESK; one that doesn't know a label gets OTHER (deduplicated)
    support.flags=0;
    CHECK(emu_get_semantic_labels(SESSION,entities[volume].handle,&labels)==XR_SUCCESS && !strcmp(text,"OTHER"));
    support.recognizedLabels="DESK,OTHER";
    CHECK(emu_get_semantic_labels(SESSION,entities[volume].handle,&labels)==XR_SUCCESS && !strcmp(text,"DESK,OTHER"));
    // no support info at all (first XR_FB_scene apps): the first label set only
    labels.next=NULL;
    CHECK(emu_get_semantic_labels(SESSION,entities[volume].handle,&labels)==XR_SUCCESS && !strcmp(text,"DESK,OTHER"));
    labels.next=&support;
    uint32_t n; XrSpaceComponentTypeFB types[8];
    CHECK(emu_enumerate_components(entities[volume].handle,0,&n,NULL)==XR_SUCCESS && n==6);  // incl. SHARABLE
    CHECK(emu_enumerate_components(entities[volume].handle,1,&n,types)==XR_ERROR_SIZE_INSUFFICIENT);
    CHECK(emu_enumerate_components(entities[volume].handle,8,&n,types)==XR_SUCCESS);
    XrSpaceComponentStatusFB status={.type=XR_TYPE_SPACE_COMPONENT_STATUS_FB};
    CHECK(emu_get_component_status(entities[volume].handle,XR_SPACE_COMPONENT_TYPE_BOUNDED_3D_FB,&status)==XR_SUCCESS && status.enabled);
    // scene loaders of older Meta SDKs switch SHARABLE on for every anchor (Meta's runtime lists it everywhere)
    XrSpaceComponentStatusSetInfoFB share={XR_TYPE_SPACE_COMPONENT_STATUS_SET_INFO_FB,NULL,XR_SPACE_COMPONENT_TYPE_SHARABLE_FB,XR_TRUE,0};
    XrAsyncRequestIdFB share_req;
    CHECK(emu_set_component_status(entities[ENT_WALL0].handle,&share,&share_req)==XR_SUCCESS);
    XrEventDataBuffer share_ev;
    CHECK(pop_event(&share_ev) && share_ev.type==XR_TYPE_EVENT_DATA_SPACE_SET_STATUS_COMPLETE_FB);
    XrSpaceContainerFB container={.type=XR_TYPE_SPACE_CONTAINER_FB};
    CHECK(emu_get_container(SESSION,entities[0].handle,&container)==XR_SUCCESS && container.uuidCountOutput==(unsigned)room_entity_count-1);
    container.uuidCapacityInput=1; container.uuids=walls;
    CHECK(emu_get_container(SESSION,entities[0].handle,&container)==XR_ERROR_SIZE_INSUFFICIENT);
    container.uuidCapacityInput=MAX_ENTITIES;
    CHECK(emu_get_container(SESSION,entities[0].handle,&container)==XR_SUCCESS);
    CHECK(!check_mesh(mesh_ok));
    return check_discovery();
}
static int create_test_anchor(XrSpace *out) {
    XrSpatialAnchorCreateInfoFB info={XR_TYPE_SPATIAL_ANCHOR_CREATE_INFO_FB,NULL,LOCAL,{{0,0,0,1},{1,0,0}},1};
    XrAsyncRequestIdFB req;
    CHECK(emu_create_anchor(SESSION,&info,&req)==XR_SUCCESS);
    XrEventDataBuffer ev;
    CHECK(pop_event(&ev) && ev.type==XR_TYPE_EVENT_DATA_SPATIAL_ANCHOR_CREATE_COMPLETE_FB);
    XrEventDataSpatialAnchorCreateCompleteFB *e=(void *)&ev;
    CHECK(e->result==XR_SUCCESS);
    *out=e->space;
    int i=fake_space_index(*out); CHECK(i>=0);
    CHECK(NEAR(entities[i].pose_in_stage.position.x,10) && NEAR(entities[i].pose_in_stage.position.z,4));
    XrPosef pose; XrSpaceLocationFlags flags;
    CHECK(locate_fake(*out,LOCAL,1,&pose,&flags) && NEAR(pose.position.x,1) && NEAR(pose.position.z,0));
    CHECK(locate_fake(LOCAL,*out,1,&pose,&flags) && NEAR(pose.position.x,-1));
    return 0;
}
static int check_persistence(const char *mode) {
    XrEventDataBuffer ev; XrAsyncRequestIdFB req;
    if(!strcmp(mode,"save") || !strcmp(mode,"save-failure")) {
        CHECK(entity_count==ENT_FIXED_COUNT);
        XrSpace spaces[2]; CHECK(!create_test_anchor(&spaces[0]) && !create_test_anchor(&spaces[1]));
        XrUuidEXT first,second;
        CHECK(emu_get_uuid(spaces[0],&first)==XR_SUCCESS && emu_get_uuid(spaces[1],&second)==XR_SUCCESS && !uuid_equal(&first,&second));
        XrSpaceSaveInfoFB fb={XR_TYPE_SPACE_SAVE_INFO_FB,NULL,spaces[0],XR_SPACE_STORAGE_LOCATION_LOCAL_FB,XR_SPACE_PERSISTENCE_MODE_INDEFINITE_FB};
        CHECK(emu_save_space(SESSION,&fb,&req)==XR_SUCCESS && pop_event(&ev));
        CHECK(ev.type==XR_TYPE_EVENT_DATA_SPACE_SAVE_COMPLETE_FB);
        if(!strcmp(mode,"save-failure")) {
            CHECK(((XrEventDataSpaceSaveCompleteFB *)&ev)->result==XR_ERROR_RUNTIME_FAILURE);
            CHECK(!entities[fake_space_index(spaces[0])].saved); return 0;
        }
        CHECK(((XrEventDataSpaceSaveCompleteFB *)&ev)->result==XR_SUCCESS);
        XrSpacesSaveInfoMETA meta={XR_TYPE_SPACES_SAVE_INFO_META,NULL,2,spaces};
        CHECK(emu_save_spaces(SESSION,&meta,&req)==XR_SUCCESS && pop_event(&ev));
        CHECK(ev.type==XR_TYPE_EVENT_DATA_SPACES_SAVE_RESULT_META && ((XrEventDataSpacesSaveResultMETA *)&ev)->result==XR_SUCCESS);
        scene_on_create_session(SESSION);
        CHECK(entity_count==ENT_FIXED_COUNT+2 && fake_space_index(spaces[0])<0);
        CHECK(uuid_equal(&first,&entities[ENT_FIXED_COUNT].uuid) && uuid_equal(&second,&entities[ENT_FIXED_COUNT+1].uuid));
    } else if(!strcmp(mode,"erase-failure")) {
        CHECK(entity_count==ENT_FIXED_COUNT+2);
        char path[4096],backup[4128]; CHECK(anchor_path(path,sizeof(path)));
        snprintf(backup,sizeof(backup),"%s.backup",path);
        CHECK(rename(path,backup)==0 && mkdir(path,0700)==0);
        XrSpace handle=entities[ENT_FIXED_COUNT].handle;
        XrSpacesEraseInfoMETA meta={XR_TYPE_SPACES_ERASE_INFO_META,NULL,1,&handle,0,NULL};
        CHECK(emu_erase_spaces(SESSION,&meta,&req)==XR_SUCCESS && pop_event(&ev));
        CHECK(ev.type==XR_TYPE_EVENT_DATA_SPACES_ERASE_RESULT_META && ((XrEventDataSpacesEraseResultMETA *)&ev)->result==XR_ERROR_RUNTIME_FAILURE);
        CHECK(fake_space_index(handle)>=0 && entities[ENT_FIXED_COUNT].saved);
        CHECK(rmdir(path)==0 && rename(backup,path)==0);
    } else if(!strcmp(mode,"erase")) {
        CHECK(entity_count==ENT_FIXED_COUNT+2);
        XrSpace old=entities[ENT_FIXED_COUNT].handle;
        XrSpaceEraseInfoFB fb={XR_TYPE_SPACE_ERASE_INFO_FB,NULL,old,XR_SPACE_STORAGE_LOCATION_LOCAL_FB};
        CHECK(emu_erase_space(SESSION,&fb,&req)==XR_SUCCESS && pop_event(&ev));
        CHECK(ev.type==XR_TYPE_EVENT_DATA_SPACE_ERASE_COMPLETE_FB && ((XrEventDataSpaceEraseCompleteFB *)&ev)->result==XR_SUCCESS);
        CHECK(fake_space_index(old)<0);
        XrUuidEXT uuid=entities[ENT_FIXED_COUNT+1].uuid;
        XrSpacesEraseInfoMETA meta={XR_TYPE_SPACES_ERASE_INFO_META,NULL,0,NULL,1,&uuid};
        CHECK(emu_erase_spaces(SESSION,&meta,&req)==XR_SUCCESS && pop_event(&ev));
        CHECK(ev.type==XR_TYPE_EVENT_DATA_SPACES_ERASE_RESULT_META && ((XrEventDataSpacesEraseResultMETA *)&ev)->result==XR_SUCCESS);
        scene_on_create_session(SESSION); CHECK(entity_count==ENT_FIXED_COUNT);
    } else if(!strcmp(mode,"reload")) {
        CHECK(entity_count==ENT_FIXED_COUNT+2);
        for(int i=ENT_FIXED_COUNT;i<entity_count;++i) CHECK(entities[i].saved && entities[i].is_anchor && NEAR(entities[i].pose_in_stage.position.z,4));
        CHECK(!check_discovery());
        XrUuidEXT uuid=entities[ENT_FIXED_COUNT].uuid;
        XrSpaceFilterUuidMETA filter={XR_TYPE_SPACE_FILTER_UUID_META,NULL,1,&uuid};
        const XrSpaceFilterBaseHeaderMETA *filters_[]={(const XrSpaceFilterBaseHeaderMETA *)&filter};
        XrSpaceDiscoveryInfoMETA discover={XR_TYPE_SPACE_DISCOVERY_INFO_META,NULL,1,filters_};
        CHECK(emu_discover_spaces(SESSION,&discover,&req)==XR_SUCCESS);
        XrSpaceDiscoveryResultMETA value;
        XrSpaceDiscoveryResultsMETA found={.type=XR_TYPE_SPACE_DISCOVERY_RESULTS_META,.resultCapacityInput=1,.results=&value};
        CHECK(emu_retrieve_discovery(SESSION,req,&found)==XR_SUCCESS && found.resultCountOutput==1 && uuid_equal(&value.uuid,&uuid));
        XrSpaceUuidFilterInfoFB fb_filter={XR_TYPE_SPACE_UUID_FILTER_INFO_FB,NULL,1,&uuid};
        XrSpaceQueryInfoFB fb_query={.type=XR_TYPE_SPACE_QUERY_INFO_FB,.maxResultCount=10,.filter=(const XrSpaceFilterInfoBaseHeaderFB *)&fb_filter};
        CHECK(emu_query_spaces_filtered(SESSION,(const XrSpaceQueryInfoBaseHeaderFB *)&fb_query,&req)==XR_SUCCESS);
        XrSpaceQueryResultFB fb_value;
        XrSpaceQueryResultsFB fb_found={.type=XR_TYPE_SPACE_QUERY_RESULTS_FB,.resultCapacityInput=1,.results=&fb_value};
        CHECK(emu_retrieve_results(SESSION,req,&fb_found)==XR_SUCCESS && fb_found.resultCountOutput==1 && uuid_equal(&fb_value.uuid,&uuid));
        while(pop_event(&ev)) {}
    } else CHECK(entity_count==ENT_FIXED_COUNT);
    return 0;
}
int main(int argc,char **argv) {
    CHECK(argc>=2);
    const char *mode=argv[1];
    if(!strcmp(mode,"override")) { scene_width=3; scene_depth=4; scene_height=2.7f; }
    emulate_scene=1;
    scene_app_mesh=!getenv("SCENE_NO_MESH_EXT");  // the app enabled XR_META_spatial_entity_mesh (the usual case)
    scene_on_create_session(SESSION);
    CHECK(is_scene_extension("XR_META_spatial_entity_discovery") && is_scene_extension("XR_META_spatial_entity_persistence") && is_scene_extension("XR_META_spatial_entity_mesh"));
    CHECK(scene_emulation("xrDiscoverSpacesMETA") && scene_emulation("xrGetSpaceTriangleMeshMETA") && !scene_emulation("garbage"));
    if(!strcmp(mode,"room")) { CHECK(argc==4); CHECK(!check_room(atoi(argv[2]),atoi(argv[3]))); }
    else if(!strcmp(mode,"derived")) {
        CHECK(room_from_file && wall_count==4 && room_entity_count==7);
        CHECK(!check_boundary(ENT_FLOOR,4) && !check_boundary(ENT_CEILING,4));
        CHECK(NEAR(entities[ENT_FLOOR].pose_in_stage.position.x,2) && NEAR(entities[ENT_FLOOR].pose_in_stage.position.z,1.5f));
        CHECK(NEAR(entities[ENT_CEILING].pose_in_stage.position.y,3.5f));
    } else if(!strcmp(mode,"many")) {
        CHECK(room_from_file && wall_count==300 && room_entity_count==303);
        for(int i=0;i<entity_count;++i) {
            CHECK(fake_space_index(entities[i].handle)==i);
            for(int j=0;j<i;++j) CHECK(!uuid_equal(&entities[i].uuid,&entities[j].uuid));
        }
        CHECK(!check_discovery());
    } else if(!strcmp(mode,"box") || !strcmp(mode,"override")) {
        CHECK(!room_from_file && entity_count==ENT_FIXED_COUNT && wall_count==4);
        CHECK(NEAR(entities[ENT_FLOOR].bounds.extent.width,scene_width?scene_width:1.5f));
        CHECK(NEAR(entities[ENT_FLOOR].bounds.extent.height,scene_depth?scene_depth:1.5f));
        CHECK(NEAR(entities[ENT_CEILING].pose_in_stage.position.y,scene_height));
        CHECK(!check_boundary(ENT_FLOOR,4) && uuid_equal(&entities[0].uuid,&(XrUuidEXT){{0}})==0);
        CHECK(!check_discovery());
    } else CHECK(!check_persistence(mode));
    XrAsyncRequestIdFB request;
    CHECK(emu_request_scene_capture(SESSION,NULL,&request)==XR_SUCCESS);
    XrEventDataBuffer ev; CHECK(pop_event(&ev) && ev.type==XR_TYPE_EVENT_DATA_SCENE_CAPTURE_COMPLETE_FB);
    reset_room();
    for(int i=0;i<MAX_QUERIES;++i) free(queries[i].results);
    return 0;
}

#ifdef SCENE_SHIM_TEST
// The shim loads this fake adapter by its production SONAME. Its scene functions are the real implementation.
__attribute__((visibility("default"))) PFN_xrVoidFunction framebridge_extension_proc(const char *name) {
    if (!emulate_scene) { emulate_scene=1; scene_on_create_session(SESSION); }
    return scene_emulation(name);
}
#endif
