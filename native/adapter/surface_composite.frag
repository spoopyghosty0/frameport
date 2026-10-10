#version 450
// Reproject the decoded video into the game camera, then compose in linear light.
// Every output pixel belongs to one primary projection, including fades/edges.
layout(location=0) in vec2 uv;
layout(location=0) out vec4 outputColor;
layout(binding=0) uniform sampler2D videoImage;
layout(binding=1) uniform sampler2DArray sceneImage;
layout(push_constant) uniform Params {
 vec4 rotation; vec4 outputTans; vec4 videoTans;
 vec4 sceneRect; vec4 options; vec4 sceneOptions; vec4 sceneScale; vec4 sceneBias;
} p;
vec3 rotate(vec4 q,vec3 v){return v+2.0*cross(q.xyz,cross(q.xyz,v)+q.w*v);}
void main(){
 vec2 scenePixel=0.5/vec2(textureSize(sceneImage,0).xy);
 vec2 sceneCoord=vec2(uv.x,p.sceneOptions.z>0 ? 1-uv.y : uv.y);
 vec2 sceneUV=clamp(p.sceneRect.xy+sceneCoord*p.sceneRect.zw,p.sceneRect.xy+scenePixel,p.sceneRect.xy+p.sceneRect.zw-scenePixel);
 vec4 scene=textureLod(sceneImage,vec3(sceneUV,p.options.y),0);
 // Projection rows follow Vulkan/OpenXR's top-to-bottom convention.
 vec3 ray=rotate(p.rotation,vec3(mix(p.outputTans.x,p.outputTans.y,uv.x),mix(p.outputTans.w,p.outputTans.z,uv.y),-1));
 vec2 vuv=(ray.xy/-ray.z-p.videoTans.xz)/(p.videoTans.yw-p.videoTans.xz); vuv.y=1-vuv.y;
 vec4 video=vec4(0);
 if(ray.z<0 && all(greaterThanEqual(vuv,vec2(0))) && all(lessThanEqual(vuv,vec2(1))))
  {
  vec2 videoPixel=0.5/vec2(textureSize(videoImage,0));
  vec4 videoRect=vec4(p.options.x*.5,0,.5,1);
  vec2 videoUV=clamp(videoRect.xy+vuv*videoRect.zw,videoRect.xy+videoPixel,videoRect.xy+videoRect.zw-videoPixel);
  video=textureLod(videoImage,videoUV,0);
 }
 float alpha=p.options.z>0 ? video.a : 1;
 vec3 rgb=p.options.w>0 ? video.rgb*alpha : video.rgb;
 float sceneAlpha=p.sceneOptions.x>0 ? scene.a : 1;
 vec3 sceneRGB=p.sceneOptions.y>0 ? scene.rgb*sceneAlpha : scene.rgb;
 // OpenXR colour transforms act on straight colour, then restore premultiplication.
 vec4 sceneStraight=vec4(sceneAlpha>0 ? sceneRGB/sceneAlpha : vec3(0),sceneAlpha);
 sceneStraight=sceneStraight*max(p.sceneScale,vec4(0))+p.sceneBias;
 sceneAlpha=sceneStraight.a;sceneRGB=sceneStraight.rgb*sceneAlpha;
 vec4 result=vec4(rgb+sceneRGB*(1-alpha),alpha+sceneAlpha*(1-alpha));
 outputColor=result;
}
