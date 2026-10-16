// SPDX-License-Identifier: GPL-3.0-only
// Included by scene_emu.c, using the cached OpenXR registry declarations (no private ABI copies).
static int meta_filter_matches(const XrSpaceDiscoveryInfoMETA *info, const scene_entity *e) {
    for (uint32_t k = 0; k < info->filterCount; ++k) {
        const XrSpaceFilterBaseHeaderMETA *f = info->filters[k];
        if (!f) continue;
        if (f->type == XR_TYPE_SPACE_FILTER_COMPONENT_META &&
            !entity_has_component(e, ((const XrSpaceFilterComponentMETA *)f)->componentType)) return 0;
        if (f->type == XR_TYPE_SPACE_FILTER_UUID_META) {
            const XrSpaceFilterUuidMETA *u = (const XrSpaceFilterUuidMETA *)f;
            if (!u->uuids || !uuid_matches(u->uuidCount, u->uuids, e)) return 0;
        }
    }
    return 1;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_discover_spaces(XrSession session, const XrSpaceDiscoveryInfoMETA *info,
                                                         XrAsyncRequestIdFB *request) {
    if (!info || !request || (info->filterCount && !info->filters)) return XR_ERROR_VALIDATION_FAILURE;
    pthread_mutex_lock(&scene_lock);
    scene_session = session;
    build_room();
    if (event_free() < 2) { pthread_mutex_unlock(&scene_lock); return XR_ERROR_LIMIT_REACHED; }
    scene_query *q = new_query();
    if (!q) { pthread_mutex_unlock(&scene_lock); return XR_ERROR_OUT_OF_MEMORY; }
    for (int i = 0; i < entity_count; ++i) {
        scene_entity *e = &entities[i];
        if (!e->erased && meta_filter_matches(info, e))
            q->results[q->count++] = (XrSpaceQueryResultFB){e->handle, e->uuid};
    }
    *request = q->id;
    XrEventDataSpaceDiscoveryResultsAvailableMETA avail = {XR_TYPE_EVENT_DATA_SPACE_DISCOVERY_RESULTS_AVAILABLE_META, NULL, *request};
    XrEventDataSpaceDiscoveryCompleteMETA done = {XR_TYPE_EVENT_DATA_SPACE_DISCOVERY_COMPLETE_META, NULL, *request, XR_SUCCESS};
    push_event(&avail, sizeof(avail)); push_event(&done, sizeof(done));
    LOG("scene: discovery request=%llu results=%u", (unsigned long long)*request, q->count);
    pthread_mutex_unlock(&scene_lock);
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_retrieve_discovery(XrSession session, XrAsyncRequestIdFB request,
                                                           XrSpaceDiscoveryResultsMETA *results) {
    (void)session;
    if (!results) return XR_ERROR_VALIDATION_FAILURE;
    pthread_mutex_lock(&scene_lock);
    scene_query *q = find_query(request);
    XrResult r = XR_SUCCESS;
    if (!q) r = XR_ERROR_VALIDATION_FAILURE;
    else {
        results->resultCountOutput = q->count;
        if (results->resultCapacityInput && results->resultCapacityInput < q->count) r = XR_ERROR_SIZE_INSUFFICIENT;
        else if (results->resultCapacityInput) {
            if (!results->results) r = XR_ERROR_VALIDATION_FAILURE;
            else for (uint32_t i = 0; i < q->count; ++i)
                results->results[i] = (XrSpaceDiscoveryResultMETA){q->results[i].space, q->results[i].uuid};
        }
    }
    pthread_mutex_unlock(&scene_lock);
    return r;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_save_spaces(XrSession session, const XrSpacesSaveInfoMETA *info,
                                                     XrAsyncRequestIdFB *request) {
    (void)session;
    if (!info || !request || !info->spaceCount || !info->spaces) return XR_ERROR_VALIDATION_FAILURE;
    pthread_mutex_lock(&scene_lock);
    if (!event_free()) { pthread_mutex_unlock(&scene_lock); return XR_ERROR_LIMIT_REACHED; }
    XrResult r = XR_SUCCESS;
    for (uint32_t k = 0; k < info->spaceCount; ++k) if (fake_space_index(info->spaces[k]) < 0) r = XR_ERROR_HANDLE_INVALID;
    *request = next_request++;
    XrEventDataSpacesSaveResultMETA done = {XR_TYPE_EVENT_DATA_SPACES_SAVE_RESULT_META, NULL, *request, r};
    push_event(&done, sizeof(done));
    pthread_mutex_unlock(&scene_lock);
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_erase_spaces(XrSession session, const XrSpacesEraseInfoMETA *info,
                                                      XrAsyncRequestIdFB *request) {
    (void)session;
    if (!info || !request || (info->spaceCount && !info->spaces) || (info->uuidCount && !info->uuids))
        return XR_ERROR_VALIDATION_FAILURE;
    pthread_mutex_lock(&scene_lock);
    if (!event_free()) { pthread_mutex_unlock(&scene_lock); return XR_ERROR_LIMIT_REACHED; }
    XrResult r = XR_SUCCESS;
    for (uint32_t k = 0; k < info->spaceCount; ++k) if (fake_space_index(info->spaces[k]) < 0) r = XR_ERROR_HANDLE_INVALID;
    *request = next_request++;
    XrEventDataSpacesEraseResultMETA done = {XR_TYPE_EVENT_DATA_SPACES_ERASE_RESULT_META, NULL, *request, r};
    push_event(&done, sizeof(done));
    pthread_mutex_unlock(&scene_lock);
    return XR_SUCCESS;
}
