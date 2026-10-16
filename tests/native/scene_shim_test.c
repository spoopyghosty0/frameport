// SPDX-License-Identifier: GPL-3.0-only
#include <openxr/openxr.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#define CHECK(c) do { if (!(c)) { fprintf(stderr,"FAIL %d: %s\n",__LINE__,#c); return 1; } } while(0)

#ifdef FAKE_DISPATCH
// Stand in for OVRPort: refuse all scene names. Any successful scene lookup must have bypassed this table.
static void marker(void) {}
XRAPI_ATTR XrResult XRAPI_CALL xrGetInstanceProcAddr(XrInstance instance,const char *name,PFN_xrVoidFunction *fn) {
    (void)instance;
    if (!strcmp(name,"testForwarded")) { *fn=marker; return XR_SUCCESS; }
    *fn=NULL; return XR_ERROR_FUNCTION_UNSUPPORTED;
}
#else
int main(int argc,char **argv) {
    CHECK(argc>2);
    CHECK(dlopen("libopenxr_loader_generic.so",RTLD_NOW|RTLD_LOCAL));
    void *shim=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL); CHECK(shim);
    PFN_xrGetInstanceProcAddr get=(PFN_xrGetInstanceProcAddr)dlsym(shim,"xrGetInstanceProcAddr"); CHECK(get);
    XrInstance instance=(XrInstance)(uintptr_t)1;
    PFN_xrVoidFunction fn=NULL;
    for(int i=2;i<argc;++i) CHECK(get(instance,argv[i],&fn)==XR_SUCCESS && fn);
    CHECK(get(instance,"testForwarded",&fn)==XR_SUCCESS && fn);
    CHECK(get(instance,"unknown",&fn)==XR_ERROR_FUNCTION_UNSUPPORTED && !fn);
    PFN_xrDiscoverSpacesMETA discover;
    PFN_xrRetrieveSpaceDiscoveryResultsMETA retrieve;
    CHECK(get(instance,"xrDiscoverSpacesMETA",&fn)==XR_SUCCESS); discover=(PFN_xrDiscoverSpacesMETA)fn;
    CHECK(get(instance,"xrRetrieveSpaceDiscoveryResultsMETA",&fn)==XR_SUCCESS); retrieve=(PFN_xrRetrieveSpaceDiscoveryResultsMETA)fn;
    XrSpaceDiscoveryInfoMETA info={.type=XR_TYPE_SPACE_DISCOVERY_INFO_META}; XrAsyncRequestIdFB req;
    CHECK(discover((XrSession)(uintptr_t)1,&info,&req)==XR_SUCCESS);
    XrSpaceDiscoveryResultsMETA results={.type=XR_TYPE_SPACE_DISCOVERY_RESULTS_META};
    CHECK(retrieve((XrSession)(uintptr_t)1,req,&results)==XR_SUCCESS && results.resultCountOutput==7);
    return 0;
}
#endif
