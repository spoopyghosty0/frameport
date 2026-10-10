// SPDX-License-Identifier: GPL-3.0-only
// Compile once as a fake loader and once as a client of the production adapter.
#define XR_USE_GRAPHICS_API_VULKAN
#include <vulkan/vulkan.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include <string.h>

#ifdef FRAMEPORT_FAKE_RUNTIME
static XrResult device(XrInstance i, XrSystemId s, VkInstance v, VkPhysicalDevice *p) {
    (void)i; (void)s; (void)v; (void)p; return XR_ERROR_RUNTIME_FAILURE;
}
static XrResult extensions(XrInstance i, XrSystemId s, uint32_t cap, uint32_t *n, char *b) {
    (void)i; (void)s; (void)cap; (void)n; (void)b; return XR_ERROR_RUNTIME_FAILURE;
}
static XrResult create_device(XrInstance i, const XrVulkanDeviceCreateInfoKHR *info, VkDevice *d, VkResult *r) {
    (void)i; (void)info; (void)d; (void)r; return XR_ERROR_RUNTIME_FAILURE;
}
static XrResult create_instance(XrInstance i, const XrVulkanInstanceCreateInfoKHR *info, VkInstance *v, VkResult *r) {
    (void)i; (void)info; (void)v; (void)r; return XR_ERROR_RUNTIME_FAILURE;
}
XRAPI_ATTR XrResult XRAPI_CALL xrGetInstanceProcAddr(XrInstance instance, const char *name, PFN_xrVoidFunction *fn) {
    (void)instance;
    *fn = NULL;
    if (!strcmp(name, "xrGetVulkanGraphicsDeviceKHR")) *fn = (PFN_xrVoidFunction)device;
    if (!strcmp(name, "xrGetVulkanDeviceExtensionsKHR")) *fn = (PFN_xrVoidFunction)extensions;
    if (!strcmp(name, "xrCreateVulkanDeviceKHR")) *fn = (PFN_xrVoidFunction)create_device;
    if (!strcmp(name, "xrCreateVulkanInstanceKHR")) *fn = (PFN_xrVoidFunction)create_instance;
    return *fn ? XR_SUCCESS : XR_ERROR_FUNCTION_UNSUPPORTED;
}
#else
#include <assert.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    assert(argc == 4);
    void *adapter = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!adapter) { fprintf(stderr, "%s\n", dlerror()); return 1; }
    void *runtime = dlopen(argv[2], RTLD_NOW | RTLD_LOCAL);
    assert(runtime);
    int native = atoi(argv[3]);
    PFN_xrGetInstanceProcAddr get = (PFN_xrGetInstanceProcAddr)dlsym(adapter, "xrGetInstanceProcAddr");
    PFN_xrGetInstanceProcAddr original = (PFN_xrGetInstanceProcAddr)dlsym(runtime, "xrGetInstanceProcAddr");
    PFN_xrVoidFunction (*extension)(const char *) = dlsym(adapter, "framebridge_extension_proc");
    assert(get && original && extension);
    const char *names[] = {"xrGetVulkanGraphicsDeviceKHR", "xrGetVulkanDeviceExtensionsKHR",
                          "xrCreateVulkanDeviceKHR", "xrCreateVulkanInstanceKHR"};
    for (unsigned i = 0; i < 4; ++i) {
        PFN_xrVoidFunction actual = NULL, real = NULL;
        assert(get(XR_NULL_HANDLE, names[i], &actual) == XR_SUCCESS);
        assert(original(XR_NULL_HANDLE, names[i], &real) == XR_SUCCESS);
        assert(native ? actual != real : actual == real);
        assert(native ? extension(names[i]) == actual : extension(names[i]) == NULL);
    }
    if (!native) {
        // Exported symbols can be requested directly, bypassing GIPA.
        PFN_xrGetVulkanGraphicsDeviceKHR device = dlsym(adapter, names[0]);
        PFN_xrGetVulkanDeviceExtensionsKHR ext = dlsym(adapter, names[1]);
        PFN_xrCreateVulkanDeviceKHR cd = dlsym(adapter, names[2]);
        PFN_xrCreateVulkanInstanceKHR ci = dlsym(adapter, names[3]);
        assert(device(XR_NULL_HANDLE, 0, VK_NULL_HANDLE, NULL) == XR_ERROR_RUNTIME_FAILURE);
        assert(ext(XR_NULL_HANDLE, 0, 0, NULL, NULL) == XR_ERROR_RUNTIME_FAILURE);
        assert(cd(XR_NULL_HANDLE, NULL, NULL, NULL) == XR_ERROR_RUNTIME_FAILURE);
        assert(ci(XR_NULL_HANDLE, NULL, NULL, NULL) == XR_ERROR_RUNTIME_FAILURE);
    }
    printf("Vulkan video hooks: surface_native=%d passed\n", native);
    dlclose(adapter); dlclose(runtime);
    return 0;
}
#endif
