// The VR worlds a game opens into: built once per environment, coloured per game (cover.ts hue/seed). Shared by
// the hero's Frame side (world.ts, in step with the APK card's lap) and vrscene.ts (the app demo's live view). Each
// environment adds one group to the scene and removes and frees everything it made in dispose().
//   grid    — a neon grid terrain flying toward the viewer between ridges, a sun with a ring, floating shapes
//   islands — dusk over a reflective sea: low-poly islands (some floating), a low sun with rays, clouds, lanterns
//   space   — a ringed planet in a nebula, an asteroid field, an orbiting wireframe station, drifting star dust
//   aurora  — a frozen night: aurora curtains over snowy peaks, mirrored in a frozen lake, ice shards (envs-more.ts)
//   reef    — under water: light shafts, caustics on the sand, kelp, glowing jellyfish, fish, bubbles (envs-more.ts)
import * as THREE from 'three';
import { palette, rng } from './types';
import { canvasTexture, disposeAll, glowTexture, hsl, mergeGeometries, moveCorners, NOISE, SKY_VERT } from './envkit';
export { hsl } from './envkit';
import { aurora, reef } from './envs-more';

export type EnvKind = 'grid' | 'islands' | 'space' | 'aurora' | 'reef';
export const ENV_KINDS: EnvKind[] = ['grid', 'islands', 'space', 'aurora', 'reef'];

/** How far the world is: open (0..1, fades everything), reveal (0..1, how far out the ground has lit up), grow (0..1,
 * the objects' size), still (reduced motion: no movement, one fixed moment). */
export interface EnvState { open: number; reveal: number; grow: number; still: boolean }

export interface Env {
  kind: EnvKind;
  /** Where the camera stands and looks by default in this world. */
  camera: { y: number; pitch: number; fov: number };
  setGame(title: string): void;
  update(t: number, s: EnvState): void;
  dispose(): void;
}

/** The environment a game opens into (stable per title). */
export function envKindFor(title: string): EnvKind {
  const { h, s } = palette(title);
  return ENV_KINDS[(h * 7 + s * 3) % ENV_KINDS.length];
}

export function buildEnv(kind: EnvKind, scene: THREE.Scene, opts: { small: boolean }): Env {
  if (kind === 'islands') return islands(scene, opts);
  if (kind === 'space') return space(scene, opts);
  if (kind === 'aurora') return aurora(scene, opts);
  if (kind === 'reef') return reef(scene, opts);
  return grid(scene, opts);
}

// ------------------------------------------------------------------ shared pieces
// ------------------------------------------------------------------ grid
const GRID_SKY = /* glsl */ `
  uniform vec3 uTop; uniform vec3 uHorizon; uniform vec3 uBottom; uniform float uOpen;
  varying vec3 vDir;
  void main() {
    float y = vDir.y;
    vec3 c = mix(uHorizon, uTop, smoothstep(0.0, 0.55, y));
    c = mix(c, uBottom, smoothstep(0.0, -0.12, y));
    c += uHorizon * exp(-abs(y) * 22.0) * 0.6;          // the bright band where the land meets the sky
    gl_FragColor = vec4(c, uOpen);
    #include <colorspace_fragment>
  }`;
const LAND_VERT = /* glsl */ `
  uniform float uScroll; uniform vec4 uShape;           // amplitude, frequency, valley width, phase
  varying vec2 vGrid; varying float vH; varying float vDist;
  float ridge(vec2 p) {
    float h = sin(p.x * uShape.y + uShape.w) * cos(p.y * uShape.y * 0.8 + uShape.w * 1.7);
    h += 0.5 * sin(p.x * uShape.y * 2.3 + p.y * uShape.y * 1.9 + uShape.w * 0.6);
    h += 0.25 * cos(p.x * uShape.y * 4.1 - p.y * uShape.y * 3.3);
    return abs(h);
  }
  void main() {
    vec3 p = position;
    vec2 g = vec2(p.x, p.z - uScroll);
    float valley = smoothstep(uShape.z, uShape.z * 3.2, abs(p.x));   // flat ground in the middle, ridges beside
    float near = mix(0.3, 1.0, smoothstep(-4.0, -45.0, p.z));        // low hills close by, the ridges further out
    float h = ridge(g * 0.06) * uShape.x * valley * near;
    p.y += h;
    vGrid = g; vH = h;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    vDist = length(mv.xyz);
    gl_Position = projectionMatrix * mv;
  }`;
const LAND_FRAG = /* glsl */ `
  uniform vec3 uLineA; uniform vec3 uLineB; uniform vec3 uFill; uniform float uOpen; uniform float uReveal;
  uniform float uCell;
  varying vec2 vGrid; varying float vH; varying float vDist;
  void main() {
    vec2 c = vGrid / uCell;
    vec2 w = fwidth(c);
    vec2 g = abs(fract(c - 0.5) - 0.5) / max(w, 1e-4);
    float line = 1.0 - min(min(g.x, g.y), 1.0);
    float fog = exp(-vDist * 0.022);
    float reveal = 1.0 - smoothstep(uReveal - 10.0, uReveal, vDist);   // lights up outward from the viewer
    vec3 lineCol = mix(uLineA, uLineB, smoothstep(0.0, 9.0, vH));
    vec3 col = uFill + lineCol * line * (1.6 + 0.8 * reveal);
    float a = (0.92 + line) * fog * reveal * uOpen;
    gl_FragColor = vec4(col * fog + lineCol * (1.0 - fog) * 0.15, clamp(a, 0.0, 1.0));
    #include <colorspace_fragment>
  }`;

function grid(scene: THREE.Scene, opts: { small: boolean }): Env {
  const root = new THREE.Group();
  scene.add(root);
  const glowTex = glowTexture();

  const sky = new THREE.ShaderMaterial({
    vertexShader: SKY_VERT, fragmentShader: GRID_SKY, side: THREE.BackSide, transparent: true, depthWrite: false,
    uniforms: { uTop: { value: new THREE.Color() }, uHorizon: { value: new THREE.Color() }, uBottom: { value: new THREE.Color() },
      uOpen: { value: 0 } },
  });
  const skyMesh = new THREE.Mesh(new THREE.SphereGeometry(300, 32, 16), sky);
  skyMesh.renderOrder = -1;
  root.add(skyMesh);

  const land = new THREE.ShaderMaterial({
    vertexShader: LAND_VERT, fragmentShader: LAND_FRAG, transparent: true,
    uniforms: { uScroll: { value: 0 }, uShape: { value: new THREE.Vector4(8, 1, 4, 0) }, uLineA: { value: new THREE.Color() },
      uLineB: { value: new THREE.Color() }, uFill: { value: new THREE.Color() }, uOpen: { value: 0 }, uReveal: { value: 0 },
      uCell: { value: 2.5 } },
  });
  const ground = new THREE.PlaneGeometry(220, 220, opts.small ? 110 : 180, opts.small ? 110 : 180);
  ground.rotateX(-Math.PI / 2);
  ground.translate(0, 0, -80);
  root.add(new THREE.Mesh(ground, land));

  const sunMat = new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true });
  const sun = new THREE.Sprite(sunMat);
  sun.position.set(0, 14, -170);
  root.add(sun);
  const ringMat = new THREE.MeshBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const ring = new THREE.Mesh(new THREE.TorusGeometry(26, 0.35, 8, 120), ringMat);
  ring.position.copy(sun.position);
  root.add(ring);

  const starGeo = new THREE.BufferGeometry();
  const nStars = opts.small ? 260 : 500;
  const starPos = new Float32Array(nStars * 3);
  const r0 = rng(7);
  for (let i = 0; i < nStars; i++) {
    const a = r0() * Math.PI * 2, y = 0.08 + r0() * 0.9, rad = Math.sqrt(1 - y * y);
    starPos.set([Math.cos(a) * rad * 280, y * 280, Math.sin(a) * rad * 280], i * 3);
  }
  starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  const starMat = new THREE.PointsMaterial({ size: 1.6, sizeAttenuation: false, transparent: true, depthWrite: false, color: 0xffffff });
  root.add(new THREE.Points(starGeo, starMat));

  const shapes = new THREE.Group();
  root.add(shapes);
  type Floater = { obj: THREE.Object3D; base: THREE.Vector3; spin: THREE.Vector3; bob: number; size: number };
  let floaters: Floater[] = [];
  const clearShapes = () => {
    shapes.traverse((o) => {
      const m = o as THREE.Mesh;
      m.geometry?.dispose();
      (m.material as THREE.Material | undefined)?.dispose?.();
    });
    shapes.clear();
    floaters = [];
  };

  return {
    kind: 'grid',
    camera: { y: 2.6, pitch: -0.07, fov: 62 },
    setGame(title) {
      const { h: h1, h2, s } = palette(title);
      const r = rng(s * 9301 + h1);
      (sky.uniforms.uTop.value as THREE.Color).copy(hsl(h1, 0.55, 0.07));
      (sky.uniforms.uHorizon.value as THREE.Color).copy(hsl(h2, 0.75, 0.38));
      (sky.uniforms.uBottom.value as THREE.Color).copy(hsl(h1, 0.4, 0.03));
      (land.uniforms.uLineA.value as THREE.Color).copy(hsl(h2, 0.95, 0.6));
      (land.uniforms.uLineB.value as THREE.Color).copy(hsl(h1, 0.9, 0.66));
      (land.uniforms.uFill.value as THREE.Color).copy(hsl(h1, 0.5, 0.035));
      (land.uniforms.uShape.value as THREE.Vector4).set(5 + r() * 7, 0.7 + r() * 0.9, 3.5 + r() * 4, r() * 6.28);
      land.uniforms.uCell.value = 2 + r() * 1.6;
      sunMat.color.copy(hsl(h2, 0.9, 0.62));
      ringMat.color.copy(hsl(h1, 0.85, 0.6));
      ring.rotation.set(1.2 + r() * 0.3, 0, r() * 0.6);

      clearShapes();
      const kinds = [
        () => new THREE.IcosahedronGeometry(1, 0), () => new THREE.OctahedronGeometry(1, 0), () => new THREE.TetrahedronGeometry(1.1, 0),
        () => new THREE.TorusGeometry(0.85, 0.28, 10, 28), () => new THREE.TorusKnotGeometry(0.7, 0.2, 80, 10, 2, 3),
        () => new THREE.DodecahedronGeometry(1, 0),
      ];
      const n = (opts.small ? 3 : 5) + Math.floor(r() * 3);
      for (let i = 0; i < n; i++) {
        const geo = kinds[Math.floor(r() * kinds.length)]();
        const col = hsl(r() < 0.5 ? h2 : h1, 0.9, 0.62);
        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 1),
          new THREE.LineBasicMaterial({ color: col, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
        const core = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: 0.16,
          blending: THREE.AdditiveBlending, depthWrite: false }));
        const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTex, color: col, transparent: true, opacity: 0.35,
          blending: THREE.AdditiveBlending, depthWrite: false }));
        halo.scale.setScalar(4.2);
        const g = new THREE.Group();
        g.add(core, edges, halo);
        const side = i % 2 ? 1 : -1;
        const z = -(24 + r() * 50);
        // inside the view: up to about a third of the way up the frame at that distance, never past its top
        const base = new THREE.Vector3(side * (3 + r() * 0.22 * -z), 3 + r() * 0.12 * -z, z);
        const size = 1.2 + r() * 2;
        g.position.copy(base);
        shapes.add(g);
        floaters.push({ obj: g, base, spin: new THREE.Vector3(r() - 0.5, r() - 0.5, r() - 0.5).multiplyScalar(0.9), bob: r() * 6.28, size });
      }
    },
    update(t, st) {
      sky.uniforms.uOpen.value = st.open;
      land.uniforms.uOpen.value = st.open;
      land.uniforms.uReveal.value = 6 + st.reveal * 260;
      land.uniforms.uScroll.value = st.still ? 40 : t * 7.5;
      starMat.opacity = st.open * 0.75;
      sunMat.opacity = st.open;
      ringMat.opacity = st.open * 0.55;
      sun.scale.setScalar(70 + (st.still ? 0 : Math.sin(t * 0.7) * 3));
      for (const f of floaters) {
        f.obj.scale.setScalar(f.size * Math.max(st.grow, 0.0001));
        if (!st.still) {
          f.obj.rotation.x = f.spin.x * t;
          f.obj.rotation.y = f.spin.y * t + 0.4;
          f.obj.position.y = f.base.y + Math.sin(t * 0.8 + f.bob) * 0.8;
        } else {
          f.obj.rotation.set(0.5, 0.7, 0.2);
        }
      }
    },
    dispose() { clearShapes(); disposeAll(root, [glowTex]); },
  };
}

// ------------------------------------------------------------------ islands
// The dusk sky: a gradient from the game's colours, a warm horizon, the sun's halo. The sea reflects the same sky.
const DUSK = /* glsl */ `
  uniform vec3 uTop; uniform vec3 uHorizon; uniform vec3 uLow; uniform vec3 uSun; uniform vec3 uSunDir;
  vec3 dusk(vec3 d) {
    float y = d.y;
    vec3 c = mix(uHorizon, uTop, smoothstep(0.0, 0.6, y));
    c = mix(c, uLow, smoothstep(0.0, -0.25, y));
    float s = max(dot(normalize(d), uSunDir), 0.0);
    c += uSun * (pow(s, 600.0) * 4.0 + pow(s, 40.0) * 0.6 + pow(s, 6.0) * 0.22);
    c += uHorizon * exp(-abs(y) * 30.0) * 0.35;
    return c;
  }`;
const ISLE_SKY = /* glsl */ `
  uniform float uOpen;
  ${DUSK}
  varying vec3 vDir;
  void main() {
    gl_FragColor = vec4(dusk(vDir), uOpen);
    #include <colorspace_fragment>
  }`;
const SEA_VERT = /* glsl */ `
  uniform float uTime;
  varying vec3 vW; varying float vDist;
  void main() {
    vec3 p = position;
    float t = uTime;
    p.y += sin(p.x * 0.12 + t * 0.9) * 0.35 + sin(p.z * 0.17 - t * 0.7) * 0.3
         + sin((p.x + p.z) * 0.31 + t * 1.3) * 0.12 + sin((p.x - p.z * 0.6) * 0.6 + t * 1.9) * 0.05;
    vec4 w = modelMatrix * vec4(p, 1.0);
    vW = w.xyz;
    vec4 mv = viewMatrix * w;
    vDist = length(mv.xyz);
    gl_Position = projectionMatrix * mv;
  }`;
const SEA_FRAG = /* glsl */ `
  uniform vec3 uDeep; uniform float uOpen; uniform float uReveal;
  ${DUSK}
  varying vec3 vW; varying float vDist;
  void main() {
    vec3 n = normalize(cross(dFdx(vW), dFdy(vW)));
    if (n.y < 0.0) n = -n;
    vec3 v = normalize(cameraPosition - vW);
    float fres = pow(1.0 - max(dot(n, v), 0.0), 4.0);
    vec3 r = reflect(-v, n);
    r.y = abs(r.y);
    vec3 refl = dusk(r);
    vec3 col = mix(uDeep, refl, 0.18 + 0.82 * fres);
    col += uSun * pow(max(dot(r, uSunDir), 0.0), 240.0) * 2.2;            // the sun's glitter path
    float fog = smoothstep(60.0, 240.0, vDist);
    col = mix(col, dusk(normalize(vec3(-v.x, 0.01, -v.z))), fog);           // into the horizon haze
    float reveal = 1.0 - smoothstep(uReveal - 14.0, uReveal, vDist);
    gl_FragColor = vec4(col, uOpen * reveal);
    #include <colorspace_fragment>
  }`;

function islands(scene: THREE.Scene, opts: { small: boolean }): Env {
  const root = new THREE.Group();
  scene.add(root);
  const glowTex = glowTexture();
  const cloudTex = canvasTexture(128, (g, n) => {
    for (let i = 0; i < 9; i++) {
      const x = n * (0.2 + 0.6 * ((i * 37) % 10) / 10), y = n * (0.45 + 0.12 * Math.sin(i * 2.1)), rr = n * (0.18 + 0.1 * ((i * 13) % 5) / 5);
      const gr = g.createRadialGradient(x, y, 0, x, y, rr);
      gr.addColorStop(0, 'rgba(255,255,255,0.55)');
      gr.addColorStop(1, 'rgba(255,255,255,0)');
      g.fillStyle = gr;
      g.fillRect(0, 0, n, n);
    }
  });
  const rayTex = canvasTexture(256, (g, n) => {
    g.translate(n / 2, n / 2);
    for (let i = 0; i < 22; i++) {
      const a = (i / 22) * Math.PI * 2 + Math.sin(i * 3.7) * 0.1, wd = 0.03 + 0.05 * ((i * 7) % 5) / 5;
      const gr = g.createRadialGradient(0, 0, 0, 0, 0, n / 2);
      gr.addColorStop(0, 'rgba(255,255,255,0.5)');
      gr.addColorStop(1, 'rgba(255,255,255,0)');
      g.fillStyle = gr;
      g.beginPath();
      g.moveTo(0, 0);
      g.arc(0, 0, n / 2, a - wd, a + wd);
      g.closePath();
      g.fill();
    }
  });

  const dusk = {
    uTop: { value: new THREE.Color() }, uHorizon: { value: new THREE.Color() }, uLow: { value: new THREE.Color() },
    uSun: { value: new THREE.Color() }, uSunDir: { value: new THREE.Vector3(0, 0.08, -1).normalize() },
  };
  const sky = new THREE.ShaderMaterial({
    vertexShader: SKY_VERT, fragmentShader: ISLE_SKY, side: THREE.BackSide, transparent: true, depthWrite: false,
    uniforms: { ...dusk, uOpen: { value: 0 } },
  });
  const skyMesh = new THREE.Mesh(new THREE.SphereGeometry(300, 32, 16), sky);
  skyMesh.renderOrder = -2;
  root.add(skyMesh);

  const sea = new THREE.ShaderMaterial({
    vertexShader: SEA_VERT, fragmentShader: SEA_FRAG, transparent: true,
    uniforms: { ...dusk, uTime: { value: 0 }, uDeep: { value: new THREE.Color() }, uOpen: { value: 0 }, uReveal: { value: 0 } },
  });
  const seaGeo = new THREE.PlaneGeometry(520, 520, opts.small ? 90 : 160, opts.small ? 90 : 160);
  seaGeo.rotateX(-Math.PI / 2);
  seaGeo.translate(0, 0, -200);
  const seaMesh = new THREE.Mesh(seaGeo, sea);
  seaMesh.renderOrder = -1;
  root.add(seaMesh);

  // light from the low sun, a dusky fill from the sky
  const hemi = new THREE.HemisphereLight(0xffffff, 0x000000, 1.1);
  const sunLight = new THREE.DirectionalLight(0xffffff, 2.6);
  root.add(hemi, sunLight, sunLight.target);

  const sunMat = new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true });
  const sun = new THREE.Sprite(sunMat);
  const rayMat = new THREE.SpriteMaterial({ map: rayTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true });
  const rays = new THREE.Sprite(rayMat);
  root.add(rays, sun);

  const isles = new THREE.Group();
  const clouds = new THREE.Group();
  root.add(isles, clouds);
  type Isle = { obj: THREE.Object3D; base: THREE.Vector3; bob: number; float: boolean; size: number };
  let isleList: Isle[] = [];
  type Cloud = { obj: THREE.Sprite; x0: number; speed: number };
  let cloudList: Cloud[] = [];

  // lanterns drifting up from the sea
  const nLan = opts.small ? 26 : 60;
  const lanPos = new Float32Array(nLan * 3);
  const lanSeed = new Float32Array(nLan);
  const rl = rng(11);
  for (let i = 0; i < nLan; i++) {
    lanPos.set([(rl() - 0.5) * 120, rl() * 30, -20 - rl() * 110], i * 3);
    lanSeed[i] = rl();
  }
  const lanGeo = new THREE.BufferGeometry();
  lanGeo.setAttribute('position', new THREE.BufferAttribute(lanPos, 3));
  const lanMat = new THREE.PointsMaterial({ map: glowTex, size: 1.6, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
  root.add(new THREE.Points(lanGeo, lanMat));

  // birds: a few Vs crossing the sky, wings beating
  const birds: { obj: THREE.Line; x0: number; y: number; z: number; speed: number; ph: number }[] = [];
  const birdMat = new THREE.LineBasicMaterial({ transparent: true, depthWrite: false });
  for (let i = 0; i < (opts.small ? 4 : 7); i++) {
    const geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(-0.9, 0, 0), new THREE.Vector3(0, -0.25, 0), new THREE.Vector3(0.9, 0, 0)]);
    const line = new THREE.Line(geo, birdMat);
    root.add(line);
    birds.push({ obj: line, x0: (rl() - 0.5) * 140, y: 14 + rl() * 18, z: -50 - rl() * 70, speed: 1.5 + rl() * 2, ph: rl() * 6.28 });
  }

  const clearGame = () => {
    for (const g of [isles, clouds]) {
      g.traverse((o) => {
        const m = o as THREE.Mesh;
        m.geometry?.dispose();
        (m.material as THREE.Material | undefined)?.dispose?.();
      });
      g.clear();
    }
    isleList = [];
    cloudList = [];
  };

  function islandGeometry(r: () => number, rock: THREE.Color, top: THREE.Color, float: boolean) {
    // a jittered low-poly lump, flattened; floating ones get a rocky cone beneath
    const geo = moveCorners(new THREE.IcosahedronGeometry(1, 1), (x, y, z) => {
      const j = [1 + (r() - 0.5) * 0.35, 1 + (r() - 0.5) * 0.35, 1 + (r() - 0.5) * 0.35];
      return [x * j[0], Math.max(y, float ? -0.15 : -0.6) * j[1] * 0.55, z * j[2]];
    });
    const parts: THREE.BufferGeometry[] = [geo];
    if (float) {
      const cone = new THREE.ConeGeometry(0.95, 2.2, 7, 2).toNonIndexed();
      cone.rotateX(Math.PI);
      cone.translate(0, -1.1, 0);                                  // its base sits inside the lump: no gap between them
      moveCorners(cone, (x, y, z) => { const k = 1 + (r() - 0.5) * 0.3; return [x * k, y, z * k]; });
      parts.push(cone);
    }
    const merged = mergeGeometries(parts);
    const mp = merged.attributes.position as THREE.BufferAttribute;
    const colors = new Float32Array(mp.count * 3);
    for (let i = 0; i < mp.count; i++) {
      const c = mp.getY(i) > 0.18 ? top : rock;
      colors.set([c.r, c.g, c.b], i * 3);
    }
    merged.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    merged.computeVertexNormals();
    return merged;
  }

  return {
    kind: 'islands',
    camera: { y: 4.2, pitch: -0.03, fov: 60 },
    setGame(title) {
      const { h: h1, h2, s } = palette(title);
      const r = rng(s * 7919 + h1 * 13);
      dusk.uTop.value.copy(hsl(h1, 0.55, 0.12));
      dusk.uHorizon.value.copy(hsl(h2, 0.85, 0.5));
      dusk.uLow.value.copy(hsl(h1, 0.5, 0.06));
      dusk.uSun.value.copy(hsl(h2 + 15, 1, 0.62));
      const sx = (r() - 0.5) * 0.9;
      dusk.uSunDir.value.set(sx, 0.05 + r() * 0.08, -1).normalize();
      (sea.uniforms.uDeep.value as THREE.Color).copy(hsl(h1, 0.6, 0.05));
      const sunPos = dusk.uSunDir.value.clone().multiplyScalar(240);
      sun.position.copy(sunPos);
      rays.position.copy(sunPos);
      sunMat.color.copy(hsl(h2 + 15, 1, 0.68));
      rayMat.color.copy(hsl(h2, 0.9, 0.6));
      sunLight.position.copy(sunPos);
      sunLight.color.copy(hsl(h2 + 15, 0.9, 0.7));
      hemi.color.copy(hsl(h1, 0.6, 0.55));
      hemi.groundColor.copy(hsl(h1, 0.4, 0.05));
      lanMat.color.copy(hsl(h2 + 25, 1, 0.66));
      birdMat.color.copy(hsl(h1, 0.4, 0.08));

      clearGame();
      const rock = hsl(h1, 0.25, 0.13), top = hsl(h2 - 40, 0.45, 0.24);
      const mat = new THREE.MeshStandardMaterial({ vertexColors: true, flatShading: true, roughness: 1, metalness: 0 });
      const treeMat = new THREE.MeshStandardMaterial({ color: hsl(h1 + 120, 0.3, 0.12), flatShading: true, roughness: 1 });
      const n = (opts.small ? 4 : 7) + Math.floor(r() * 3);
      for (let i = 0; i < n; i++) {
        const float = r() < 0.45;
        const g = new THREE.Group();
        g.add(new THREE.Mesh(islandGeometry(r, rock, top, float), mat));
        for (let k = Math.floor(r() * 4); k > 0; k--) {
          const tree = new THREE.Mesh(new THREE.ConeGeometry(0.12, 0.55 + r() * 0.3, 6), treeMat);
          tree.position.set((r() - 0.5) * 0.9, 0.62, (r() - 0.5) * 0.9);
          g.add(tree);
        }
        // mostly right of the seam (the canvas's left part is behind the portal), small close by, big far out
        const side = i === 0 || r() < 0.7 ? 1 : -1;
        const z = -(42 + r() * 140);
        const size = 1.6 + r() * 3 + (-z) * 0.035;
        const base = new THREE.Vector3(side * (5 + r() * 0.42 * -z), float ? 4 + r() * 0.1 * -z : 0.2, z);
        g.position.copy(base);
        g.rotation.y = r() * 6.28;
        isles.add(g);
        isleList.push({ obj: g, base, bob: r() * 6.28, float, size });
      }
      const cloudMat = () => new THREE.SpriteMaterial({ map: cloudTex, color: hsl(h2, 0.6, 0.62), transparent: true, depthWrite: false });
      for (let i = 0; i < (opts.small ? 5 : 9); i++) {
        const c = new THREE.Sprite(cloudMat());
        const z = -120 - r() * 120;
        c.position.set((r() - 0.5) * 300, 18 + r() * 40, z);
        c.scale.set(60 + r() * 60, 18 + r() * 14, 1);
        clouds.add(c);
        cloudList.push({ obj: c, x0: c.position.x, speed: 0.6 + r() });
      }
    },
    update(t, st) {
      sky.uniforms.uOpen.value = st.open;
      sea.uniforms.uOpen.value = st.open;
      sea.uniforms.uReveal.value = 8 + st.reveal * 320;
      sea.uniforms.uTime.value = st.still ? 3 : t;
      sunMat.opacity = st.open;
      sun.scale.setScalar(44 + (st.still ? 0 : Math.sin(t * 0.6) * 2));
      rayMat.opacity = st.open * 0.32;
      rays.scale.setScalar(230);
      rayMat.rotation = st.still ? 0.3 : t * 0.02;
      lanMat.opacity = st.open * 0.9;
      birdMat.opacity = st.open * 0.85;
      for (const isle of isleList) {
        isle.obj.scale.setScalar(isle.size * Math.max(st.grow, 0.0001));
        isle.obj.position.y = isle.base.y + (isle.float && !st.still ? Math.sin(t * 0.5 + isle.bob) * 0.6 : 0);
      }
      for (const c of cloudList) {
        (c.obj.material as THREE.SpriteMaterial).opacity = st.open * 0.38;
        c.obj.position.x = st.still ? c.x0 : ((c.x0 + t * c.speed + 150) % 300) - 150;
      }
      if (!st.still) {
        const p = lanGeo.attributes.position as THREE.BufferAttribute;
        for (let i = 0; i < nLan; i++) {
          const y = ((lanSeed[i] * 30 + t * (0.6 + lanSeed[i])) % 32);
          p.setY(i, y);
          p.setX(i, lanPos[i * 3] + Math.sin(t * 0.4 + lanSeed[i] * 9) * 1.5);
        }
        p.needsUpdate = true;
      }
      for (const b of birds) {
        b.obj.position.set(st.still ? b.x0 : ((b.x0 + t * b.speed + 90) % 180) - 90, b.y + Math.sin(t * 0.7 + b.ph) * 0.8, b.z);
        b.obj.scale.set(1.6, st.still ? 1 : 0.4 + Math.abs(Math.sin(t * 5 + b.ph)) * 1.4, 1);
      }
    },
    dispose() { clearGame(); disposeAll(root, [glowTex, cloudTex, rayTex]); },
  };
}

/** Non-indexed geometries with the same attributes, joined (enough for our low-poly pieces). */
// ------------------------------------------------------------------ space
const NEBULA = /* glsl */ `
  uniform vec3 uA; uniform vec3 uB; uniform vec3 uDark; uniform float uOpen; uniform float uSeed;
  ${NOISE}
  varying vec3 vDir;
  void main() {
    vec3 d = normalize(vDir) * 2.2 + uSeed;
    float n1 = fbm(d), n2 = fbm(d * 1.7 + 3.1);
    float band = exp(-pow(dot(normalize(vDir), normalize(vec3(0.3, 1.0, 0.2))) * 2.4, 2.0));  // a milky band across
    vec3 c = uDark;
    c += uA * pow(n1, 2.6) * 1.6 * (0.4 + band);
    c += uB * pow(n2, 3.4) * 1.9 * band;
    c += vec3(1.0) * pow(max(noise(normalize(vDir) * 180.0) - 0.82, 0.0) * 5.5, 2.0);   // faint far stars
    gl_FragColor = vec4(c, uOpen);
    #include <colorspace_fragment>
  }`;
const PLANET_VERT = /* glsl */ `
  varying vec3 vN; varying vec3 vP; varying vec3 vWN; varying vec3 vView;
  void main() {
    vN = normalize(normal); vP = position;
    vWN = normalize(mat3(modelMatrix) * normal);
    vec4 w = modelMatrix * vec4(position, 1.0);
    vView = normalize(cameraPosition - w.xyz);
    gl_Position = projectionMatrix * viewMatrix * w;
  }`;
const PLANET_FRAG = /* glsl */ `
  uniform vec3 uA; uniform vec3 uB; uniform vec3 uSunDir; uniform float uOpen; uniform float uSeed; uniform float uTime;
  ${NOISE}
  varying vec3 vN; varying vec3 vP; varying vec3 vWN; varying vec3 vView;
  void main() {
    vec3 p = normalize(vP);
    float lat = p.y * 6.0 + fbm(p * 3.0 + uSeed + vec3(uTime * 0.01, 0.0, 0.0)) * 2.2;
    float bands = 0.5 + 0.5 * sin(lat * 2.4);
    vec3 col = mix(uA, uB, bands);
    col *= 0.6 + 0.6 * fbm(p * 9.0 + uSeed);
    float lit = max(dot(vWN, uSunDir), 0.0);
    col = col * (0.06 + lit * 1.1);
    float rim = pow(1.0 - max(dot(vWN, vView), 0.0), 3.0);
    col += uB * rim * (0.25 + lit) * 0.9;                 // the atmosphere at the edge
    gl_FragColor = vec4(col, uOpen);
    #include <colorspace_fragment>
  }`;
const RING_VERT = /* glsl */ `
  varying vec2 vUv2; varying vec3 vW;
  void main() { vUv2 = position.xy; vW = (modelMatrix * vec4(position, 1.0)).xyz; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const RING_FRAG = /* glsl */ `
  uniform vec3 uA; uniform vec3 uB; uniform float uOpen; uniform float uInner; uniform float uOuter; uniform float uSeed;
  ${NOISE}
  varying vec2 vUv2; varying vec3 vW;
  void main() {
    float r = (length(vUv2) - uInner) / (uOuter - uInner);
    float b = 0.55 + 0.45 * sin(r * 70.0 + uSeed) * sin(r * 23.0);
    b *= smoothstep(0.0, 0.08, r) * smoothstep(1.0, 0.85, r);
    b *= 0.6 + 0.6 * noise(vec3(r * 40.0, uSeed, 0.0));
    vec3 col = mix(uA, uB, r);
    gl_FragColor = vec4(col * 1.2, b * 0.75 * uOpen);
    #include <colorspace_fragment>
  }`;

function space(scene: THREE.Scene, opts: { small: boolean }): Env {
  const root = new THREE.Group();
  scene.add(root);
  const glowTex = glowTexture();
  const sunDir = new THREE.Vector3(0.7, 0.25, -0.6).normalize();

  const nebula = new THREE.ShaderMaterial({
    vertexShader: SKY_VERT, fragmentShader: NEBULA, side: THREE.BackSide, transparent: true, depthWrite: false,
    uniforms: { uA: { value: new THREE.Color() }, uB: { value: new THREE.Color() }, uDark: { value: new THREE.Color() },
      uOpen: { value: 0 }, uSeed: { value: 0 } },
  });
  const sky = new THREE.Mesh(new THREE.SphereGeometry(320, 40, 20), nebula);
  sky.renderOrder = -2;
  root.add(sky);

  // stars: a far shell, and near dust that drifts past (parallax)
  const r0 = rng(23);
  const nFar = opts.small ? 500 : 1100;
  const far = new Float32Array(nFar * 3);
  for (let i = 0; i < nFar; i++) {
    const u = r0() * 2 - 1, a = r0() * Math.PI * 2, rr = Math.sqrt(1 - u * u);
    far.set([Math.cos(a) * rr * 290, u * 290, Math.sin(a) * rr * 290], i * 3);
  }
  const farGeo = new THREE.BufferGeometry();
  farGeo.setAttribute('position', new THREE.BufferAttribute(far, 3));
  const farMat = new THREE.PointsMaterial({ size: 1.4, sizeAttenuation: false, transparent: true, depthWrite: false, color: 0xffffff });
  root.add(new THREE.Points(farGeo, farMat));
  const nDust = opts.small ? 160 : 380;
  const dust = new Float32Array(nDust * 3);
  for (let i = 0; i < nDust; i++) dust.set([(r0() - 0.5) * 90, (r0() - 0.5) * 50, -r0() * 140], i * 3);
  const dustGeo = new THREE.BufferGeometry();
  dustGeo.setAttribute('position', new THREE.BufferAttribute(dust, 3));
  const dustMat = new THREE.PointsMaterial({ map: glowTex, size: 0.6, transparent: true, depthWrite: false, color: 0xffffff, blending: THREE.AdditiveBlending });
  root.add(new THREE.Points(dustGeo, dustMat));

  const sunMat = new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true });
  const star = new THREE.Sprite(sunMat);
  star.position.copy(sunDir).multiplyScalar(260);
  star.scale.setScalar(55);
  root.add(star);
  root.add(new THREE.AmbientLight(0xffffff, 0.15));
  const light = new THREE.DirectionalLight(0xffffff, 2.8);
  light.position.copy(sunDir).multiplyScalar(100);
  root.add(light, light.target);

  // the planet, its atmosphere and rings
  const planetMat = new THREE.ShaderMaterial({
    vertexShader: PLANET_VERT, fragmentShader: PLANET_FRAG, transparent: true,
    uniforms: { uA: { value: new THREE.Color() }, uB: { value: new THREE.Color() }, uSunDir: { value: sunDir }, uOpen: { value: 0 },
      uSeed: { value: 0 }, uTime: { value: 0 } },
  });
  const planet = new THREE.Group();
  const ball = new THREE.Mesh(new THREE.SphereGeometry(30, 64, 32), planetMat);
  const haloMat = new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true });
  const halo = new THREE.Sprite(haloMat);
  halo.scale.setScalar(96);
  const ringMat = new THREE.ShaderMaterial({
    vertexShader: RING_VERT, fragmentShader: RING_FRAG, transparent: true, side: THREE.DoubleSide, depthWrite: false,
    uniforms: { uA: { value: new THREE.Color() }, uB: { value: new THREE.Color() }, uOpen: { value: 0 }, uInner: { value: 38 },
      uOuter: { value: 66 }, uSeed: { value: 0 } },
  });
  const rings = new THREE.Mesh(new THREE.RingGeometry(38, 66, 160, 1), ringMat);
  planet.add(halo, ball, rings);
  root.add(planet);

  // asteroids: one jittered rock, many instances
  const rockGeo = new THREE.DodecahedronGeometry(1, 0);
  moveCorners(rockGeo, (x, y, z) => [x * (0.75 + r0() * 0.5), y * (0.65 + r0() * 0.5), z * (0.75 + r0() * 0.5)]);
  rockGeo.computeVertexNormals();
  const rockMat = new THREE.MeshStandardMaterial({ flatShading: true, roughness: 1, metalness: 0.05 });
  const nRocks = opts.small ? 55 : 140;
  const rocks = new THREE.InstancedMesh(rockGeo, rockMat, nRocks);
  const rockData: { p: THREE.Vector3; s: number; spin: THREE.Vector3 }[] = [];
  root.add(rocks);

  // the station: a ring with spokes and a hub, lit windows blinking
  const station = new THREE.Group();
  const lineMat = new THREE.LineBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const hullMat = new THREE.MeshStandardMaterial({ color: 0x1a1d24, roughness: 0.6, metalness: 0.5, transparent: true });
  const ringGeo = new THREE.TorusGeometry(7, 0.55, 8, 56);
  station.add(new THREE.Mesh(ringGeo, hullMat), new THREE.LineSegments(new THREE.EdgesGeometry(ringGeo, 20), lineMat));
  const hubGeo = new THREE.CylinderGeometry(1.1, 1.1, 4, 10);
  hubGeo.rotateX(Math.PI / 2);
  station.add(new THREE.Mesh(hubGeo, hullMat), new THREE.LineSegments(new THREE.EdgesGeometry(hubGeo, 20), lineMat));
  for (let k = 0; k < 4; k++) {
    const spoke = new THREE.CylinderGeometry(0.15, 0.15, 6, 5);
    spoke.translate(0, 3.6, 0);
    spoke.rotateZ((k / 4) * Math.PI * 2);
    station.add(new THREE.Mesh(spoke, hullMat));
  }
  const panel = new THREE.BoxGeometry(9, 0.08, 2.4);
  panel.translate(0, 0, -3.4);
  station.add(new THREE.LineSegments(new THREE.EdgesGeometry(panel), lineMat));
  const blinks: THREE.Sprite[] = [];
  const blinkMat = () => new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true });
  for (let k = 0; k < 6; k++) {
    const b = new THREE.Sprite(blinkMat());
    const a = (k / 6) * Math.PI * 2;
    b.position.set(Math.cos(a) * 7, Math.sin(a) * 7, 0);
    b.scale.setScalar(1.6);
    station.add(b);
    blinks.push(b);
  }
  root.add(station);

  let side = 1;
  return {
    kind: 'space',
    camera: { y: 0, pitch: 0.02, fov: 58 },
    setGame(title) {
      const { h: h1, h2, s } = palette(title);
      const r = rng(s * 4099 + h1 * 7);
      side = r() < 0.5 ? -1 : 1;
      (nebula.uniforms.uA.value as THREE.Color).copy(hsl(h1, 0.75, 0.36));
      (nebula.uniforms.uB.value as THREE.Color).copy(hsl(h2, 0.85, 0.5));
      (nebula.uniforms.uDark.value as THREE.Color).copy(hsl(h1, 0.5, 0.02));
      nebula.uniforms.uSeed.value = r() * 40;
      (planetMat.uniforms.uA.value as THREE.Color).copy(hsl(h2, 0.55, 0.42));
      (planetMat.uniforms.uB.value as THREE.Color).copy(hsl(h1 + 20, 0.65, 0.6));
      planetMat.uniforms.uSeed.value = r() * 20;
      (ringMat.uniforms.uA.value as THREE.Color).copy(hsl(h2, 0.5, 0.55));
      (ringMat.uniforms.uB.value as THREE.Color).copy(hsl(h1, 0.6, 0.65));
      ringMat.uniforms.uSeed.value = r() * 10;
      haloMat.color.copy(hsl(h1 + 20, 0.8, 0.5));
      sunMat.color.copy(hsl(h2 + 30, 0.6, 0.85));
      // the planet high on the right (the canvas's left part is behind the portal), the station in front of it
      planet.position.set(14 + r() * 22, 16 + r() * 12, -150 - r() * 30);
      rings.rotation.set(Math.PI / 2 - 0.35 - r() * 0.3, 0.2 * side, r() * 0.4);
      lineMat.color.copy(hsl(h2, 0.95, 0.62));
      station.position.set(side < 0 ? 4 + r() * 5 : 15 + r() * 6, side < 0 ? -4 + r() * 3 : 1 + r() * 4, -48 - r() * 12);
      station.rotation.set(0.5 + r() * 0.4, r() * 0.8, 0);
      blinks.forEach((b, k) => (b.material as THREE.SpriteMaterial).color.copy(hsl(k % 2 ? h1 : h2, 1, 0.65)));
      rockMat.color.copy(hsl(h1, 0.12, 0.42));
      rockData.length = 0;
      for (let i = 0; i < nRocks; i++) {
        // a belt sweeping across the view, thicker on the planet's side
        const z = -18 - r() * 120;
        const x = (r() - 0.5) * (40 + -z * 1.1) + side * 8;
        const y = (r() - 0.5) * 14 - 6 + x * 0.12 * side;
        rockData.push({ p: new THREE.Vector3(x, y, z), s: 0.2 + Math.pow(r(), 3) * 2.4,
          spin: new THREE.Vector3(r() - 0.5, r() - 0.5, r() - 0.5).multiplyScalar(1.2) });
      }
    },
    update(t, st) {
      nebula.uniforms.uOpen.value = st.open;
      farMat.opacity = st.open * 0.85;
      dustMat.opacity = st.open * 0.7;
      sunMat.opacity = st.open;
      planetMat.uniforms.uOpen.value = st.open;
      planetMat.uniforms.uTime.value = st.still ? 0 : t;
      ringMat.uniforms.uOpen.value = st.open;
      haloMat.opacity = st.open * 0.35;
      lineMat.opacity = st.open * 0.9;
      hullMat.opacity = st.open;
      const g = Math.max(st.grow, 0.0001);
      planet.scale.setScalar(0.85 + 0.15 * g);
      planet.rotation.y = st.still ? 0 : t * 0.01;
      station.scale.setScalar(g);
      if (!st.still) station.rotation.z = t * 0.15;
      blinks.forEach((b, k) => { (b.material as THREE.SpriteMaterial).opacity = st.open * (st.still ? 0.8 : 0.35 + 0.65 * Math.max(0, Math.sin(t * 2.4 + k * 1.3))); });
      const m = new THREE.Matrix4(), q = new THREE.Quaternion(), e = new THREE.Euler(), sc = new THREE.Vector3();
      rockData.forEach((d, i) => {
        e.set(d.spin.x * (st.still ? 1 : t), d.spin.y * (st.still ? 1 : t), d.spin.z * (st.still ? 1 : t));
        q.setFromEuler(e);
        sc.setScalar(d.s * g);
        const p = d.p.clone();
        if (!st.still) p.x += Math.sin(t * 0.05 + i) * 0.6;
        rocks.setMatrixAt(i, m.compose(p, q, sc));
      });
      rocks.instanceMatrix.needsUpdate = true;
      rockMat.opacity = st.open;
      rockMat.transparent = st.open < 1;
      if (!st.still) {
        const p = dustGeo.attributes.position as THREE.BufferAttribute;
        for (let i = 0; i < nDust; i++) {
          let z = dust[i * 3 + 2] + t * 4;
          z = ((z % 140) + 140) % 140 - 140;
          p.setZ(i, z + 10);
        }
        p.needsUpdate = true;
      }
    },
    dispose() { disposeAll(root, [glowTex]); },
  };
}
