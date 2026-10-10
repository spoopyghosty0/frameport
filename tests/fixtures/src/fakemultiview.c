// Test fixture for patches/frame/gl_multiview_fbo.py: an own-engine library (like Doom3Quest's libdoom3.so) that loads
// OpenGL ES with dlopen("libGLESv3.so"), also calls GL directly and compiles OVR_multiview vertex shaders.
// Build: aarch64-linux-android29-clang -shared -O2 -fPIC fakemultiview.c -Wl,--no-as-needed -lGLESv3 -lEGL
//        -Wl,-soname,libfakemultiview.so -o ../libfakemultiview_arm64.so
#include <GLES3/gl3.h>
#include <dlfcn.h>

const char *const vertex_shader =
    "#version 300 es\n#define NUM_VIEWS 2\n#extension GL_OVR_multiview2 : enable\nlayout(num_views=NUM_VIEWS) in;\n"
    "uniform mat4 m[NUM_VIEWS];\nin vec4 v;\nvoid main() { gl_Position = m[gl_ViewID_OVR] * v; }\n";

void *load_gles(void) {
    void *h = dlopen("libGLESv3.so", RTLD_NOW);
    return h ? h : dlopen("libGLESv2.so", RTLD_NOW);
}

void bind_pool(GLuint fb) { glBindFramebuffer(GL_FRAMEBUFFER, fb); }
