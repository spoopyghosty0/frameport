// Frame side, variant "particles": the APK card comes apart at the portal. Its pieces stream out of the seam and
// gather into a shape of the game's own (picked from its title), which turns slowly, shies away from the pointer
// and turns with a drag, then scatters before the next game. Everything moves in the vertex shader: one draw call.
import * as THREE from 'three';
import { SEAM_TOP, palette, rng, type FrameSide } from './types';

const VERT = /* glsl */ `
  attribute vec3 aTarget; attribute vec4 aRand;   // x: when it leaves, y: where on the seam, z/w: scatter
  uniform float uLap, uTime, uScale, uPx, uPointerOn, uSeamTop;
  uniform vec2 uHalf, uRot; uniform vec3 uCenter, uPointer, uColA, uColB;
  varying vec3 vCol; varying float vAlpha; varying float vHot;
  mat3 rotY(float a) { float c = cos(a), s = sin(a); return mat3(c, 0., -s, 0., 1., 0., s, 0., c); }
  mat3 rotX(float a) { float c = cos(a), s = sin(a); return mat3(1., 0., 0., 0., c, s, 0., -s, c); }
  void main() {
    float emitAt = 0.34 + aRand.x * 0.2;
    float k = clamp((uLap - emitAt) / 0.17, 0.0, 1.0);
    k = 1.0 - pow(1.0 - k, 3.0);
    float alive = step(emitAt, uLap);
    float v = 0.21 + aRand.y * 0.56;                                   // the card's height on the seam
    vec3 seam = vec3((uSeamTop * (1.0 - v) * 2.0 - 1.0) * uHalf.x, (1.0 - 2.0 * v) * uHalf.y, 0.0);
    vec3 shape = rotY(uRot.x + uTime * 0.28) * rotX(uRot.y + 0.42) * aTarget;
    shape += 0.035 * vec3(sin(uTime * 1.3 + aRand.z * 20.0), cos(uTime * 1.1 + aRand.w * 20.0), sin(uTime * 0.9 + aRand.x * 20.0));
    vec3 tgt = shape * uScale + uCenter;
    vec3 mid = mix(seam, tgt, 0.45) + vec3(0.0, (aRand.z - 0.5) * uHalf.y * 0.9, (aRand.w - 0.5) * 3.0);
    vec3 p = mix(mix(seam, mid, k), mix(mid, tgt, k), k);
    float d = smoothstep(0.86, 0.985, uLap);                          // scatter before the next game
    vec3 dir = normalize(vec3(aRand.z - 0.5, aRand.w - 0.5, aRand.x - 0.5) + 0.001);
    p += dir * d * uScale * 3.2;
    vec2 toP = p.xy - uPointer.xy;
    float dist2 = dot(toP, toP);
    p.xy += normalize(toP + 0.0001) * uPointerOn * uScale * 0.5 * exp(-dist2 / (uScale * uScale * 0.1)) * k;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = uPx * (0.55 + aRand.w * 0.9) * (1.0 + (1.0 - k) * 1.4) * (8.0 / -mv.z);
    vCol = mix(uColA, uColB, clamp(aTarget.y * 0.5 + 0.5, 0.0, 1.0));
    vHot = 1.0 - k;                                                    // fresh from the portal: whiter
    vAlpha = alive * (1.0 - d) * (0.4 + 0.6 * k);
  }`;
const FRAG = /* glsl */ `
  varying vec3 vCol; varying float vAlpha; varying float vHot;
  void main() {
    float r = length(gl_PointCoord - 0.5);
    float a = smoothstep(0.5, 0.0, r);
    vec3 c = mix(vCol, vec3(1.0), vHot * 0.6) * (0.8 + (1.0 - r * 2.0) * 0.9);
    gl_FragColor = vec4(c, a * vAlpha);
    #include <colorspace_fragment>
  }`;

/** Points spread over a geometry's surface (random spot in a random triangle). */
function surface(geo: THREE.BufferGeometry, n: number, r: () => number): Float32Array {
  const g = geo.index ? geo.toNonIndexed() : geo;
  const pos = g.getAttribute('position');
  const tris = pos.count / 3;
  const out = new Float32Array(n * 3);
  const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
  for (let i = 0; i < n; i++) {
    const t = Math.floor(r() * tris) * 3;
    a.fromBufferAttribute(pos, t); b.fromBufferAttribute(pos, t + 1); c.fromBufferAttribute(pos, t + 2);
    let u = r(), v = r();
    if (u + v > 1) { u = 1 - u; v = 1 - v; }
    out.set([a.x + (b.x - a.x) * u + (c.x - a.x) * v, a.y + (b.y - a.y) * u + (c.y - a.y) * v, a.z + (b.z - a.z) * u + (c.z - a.z) * v], i * 3);
  }
  if (g !== geo) g.dispose();
  geo.dispose();
  return out;
}

/** The game's shape, about one unit across: a knot, a crystal, a helix, rings, a shell or a torus. */
function shapeFor(seedValue: number, n: number): Float32Array {
  const r = rng(seedValue);
  const kind = seedValue % 6;
  if (kind === 2) {                                                  // a double helix
    const out = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const s = r(), strand = i % 2 ? Math.PI : 0, a = s * Math.PI * 5 + strand;
      const rad = 0.42 + (r() - 0.5) * 0.06;
      out.set([Math.cos(a) * rad, (s - 0.5) * 1.9, Math.sin(a) * rad], i * 3);
    }
    return out;
  }
  if (kind === 4) {                                                  // a sphere shell with a bright core
    const out = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const y = 1 - (i / (n - 1)) * 2, rad = Math.sqrt(1 - y * y), a = i * 2.39996;
      const s = i % 7 === 0 ? 0.3 : 0.95;
      out.set([Math.cos(a) * rad * s, y * s, Math.sin(a) * rad * s], i * 3);
    }
    return out;
  }
  if (kind === 3) {                                                  // three crossed rings
    const out = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const a = r() * Math.PI * 2, j = (r() - 0.5) * 0.05, ring = i % 3;
      const p = [Math.cos(a) * (0.95 + j), Math.sin(a) * (0.95 + j), j];
      out.set(ring === 0 ? p : ring === 1 ? [p[0], p[2], p[1]] : [p[2], p[0], p[1]], i * 3);
    }
    return out;
  }
  const geo = kind === 0 ? new THREE.TorusKnotGeometry(0.6, 0.17, 220, 16, 2 + (seedValue % 2), 3 + (seedValue % 3))
    : kind === 1 ? new THREE.IcosahedronGeometry(1, 1)
    : new THREE.TorusGeometry(0.72, 0.26, 24, 80);
  return surface(geo, n, r);
}

const hsl = (h: number, s: number, l: number) => new THREE.Color().setHSL(((h % 360) + 360) % 360 / 360, s, l);

export function create(canvas: HTMLCanvasElement, opts: { still: boolean }): FrameSide {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: true, powerPreference: 'low-power' });
  const dpr = Math.min(devicePixelRatio || 1, 2);
  renderer.setPixelRatio(dpr);
  renderer.setClearColor(0x000000, 0);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
  camera.position.set(0, 0, 8);

  const N = matchMedia('(max-width: 700px)').matches ? 1700 : 3200;
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(N * 3), 3));   // unused: the shader places them
  geo.setAttribute('aTarget', new THREE.BufferAttribute(new Float32Array(N * 3), 3));
  const rand = new Float32Array(N * 4);
  const r0 = rng(11);
  for (let i = 0; i < N * 4; i++) rand[i] = r0();
  geo.setAttribute('aRand', new THREE.BufferAttribute(rand, 4));
  geo.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 100);

  const mat = new THREE.ShaderMaterial({
    vertexShader: VERT, fragmentShader: FRAG, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: {
      uLap: { value: 0 }, uTime: { value: 0 }, uScale: { value: 1 }, uPx: { value: 3 * dpr }, uPointerOn: { value: 0 },
      uSeamTop: { value: SEAM_TOP }, uHalf: { value: new THREE.Vector2(1, 1) }, uRot: { value: new THREE.Vector2() },
      uCenter: { value: new THREE.Vector3() }, uPointer: { value: new THREE.Vector3(99, 99, 0) },
      uColA: { value: new THREE.Color() }, uColB: { value: new THREE.Color() },
    },
  });
  scene.add(new THREE.Points(geo, mat));

  let pointerTarget = 0, pointerOn = 0;
  const half = new THREE.Vector2(1, 1);

  return {
    setGame(title) {
      const { h, h2, s } = palette(title);
      (geo.getAttribute('aTarget') as THREE.BufferAttribute).set(shapeFor(s * 31 + h, N));
      geo.getAttribute('aTarget').needsUpdate = true;
      (mat.uniforms.uColA.value as THREE.Color).copy(hsl(h, 0.9, 0.6));
      (mat.uniforms.uColB.value as THREE.Color).copy(hsl(h2, 0.95, 0.64));
    },
    frame(t, lap) {
      pointerOn += (pointerTarget - pointerOn) * 0.12;
      mat.uniforms.uLap.value = lap;
      mat.uniforms.uTime.value = opts.still ? 1.2 : t;
      mat.uniforms.uPointerOn.value = pointerOn;
      renderer.render(scene, camera);
    },
    resize(width, height) {
      const w = Math.max(1, width), h = Math.max(1, height);
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      half.y = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * camera.position.z;
      half.x = half.y * camera.aspect;
      (mat.uniforms.uHalf.value as THREE.Vector2).copy(half);
      // the shape sits in the middle of what is visible right of the seam
      (mat.uniforms.uCenter.value as THREE.Vector3).set((0.6 * 2 - 1) * half.x, 0.05 * half.y, 0);
      mat.uniforms.uScale.value = Math.min(half.y * 0.52, 0.8 * half.x * 0.62);
    },
    look(yaw, pitch) { (mat.uniforms.uRot.value as THREE.Vector2).set(yaw * 2.2, pitch * 2); },
    pointer(x, y, active) {
      pointerTarget = active ? 1 : 0;
      if (active) (mat.uniforms.uPointer.value as THREE.Vector3).set((x * 2 - 1) * half.x, (1 - 2 * y) * half.y, 0);
    },
    dispose() {
      geo.dispose();
      mat.dispose();
      renderer.dispose();
    },
  };
}
