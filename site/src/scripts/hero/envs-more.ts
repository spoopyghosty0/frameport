// Two more worlds for envs.ts (same Env contract: built once, recoloured and re-seeded per game, freed in dispose()):
//   aurora — a frozen night: aurora curtains over snowy low-poly peaks, mirrored in a cracked frozen lake, floating
//            ice shards, a moon, falling snow
//   reef   — under water: the bright surface overhead, light shafts, a sandy floor with moving caustics, swaying
//            kelp, glowing jellyfish, a school of fish, rising bubbles
import * as THREE from 'three';
import type { Env } from './envs';
import { palette, rng } from './types';
import { disposeAll, glowTexture, hsl, moveCorners, NOISE, SKY_VERT } from './envkit';

type Opts = { small: boolean };
const keepAlpha = (m: THREE.Material, a: number) => { m.opacity = a; m.transparent = true; };

// ------------------------------------------------------------------ aurora
const NIGHT = /* glsl */ `
  uniform vec3 uTop; uniform vec3 uHorizon; uniform vec3 uGlow; uniform float uOpen; uniform float uSeed;
  ${NOISE}
  varying vec3 vDir;
  void main() {
    vec3 d = normalize(vDir);
    float y = d.y;
    vec3 c = mix(uHorizon, uTop, smoothstep(-0.02, 0.55, y));
    c += uGlow * exp(-abs(y - 0.2) * 4.5) * 0.16 * (0.55 + 0.45 * fbm(d * 3.0 + uSeed));   // the aurora's haze
    c += vec3(1.0) * pow(max(noise(d * 220.0 + uSeed) - 0.8, 0.0) * 5.0, 2.0) * smoothstep(0.03, 0.25, y);
    c = mix(c, uHorizon * 0.5, smoothstep(0.0, -0.08, y));
    gl_FragColor = vec4(c, uOpen);
    #include <colorspace_fragment>
  }`;
const CURTAIN_VERT = /* glsl */ `
  uniform float uTime; uniform float uSeed;
  varying vec2 vUv;
  void main() {
    vUv = uv;
    vec3 p = position;
    float x = uv.x;
    p.z += sin(x * 4.0 + uSeed + uTime * 0.1) * 26.0 + sin(x * 11.0 - uTime * 0.17 + uSeed * 2.0) * 6.0;
    p.y += (sin(x * 5.0 + uTime * 0.18 + uSeed) * 0.12 + sin(x * 13.0 - uTime * 0.25) * 0.04) * (1.0 - uv.y * 0.6);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
  }`;
const CURTAIN_FRAG = /* glsl */ `
  uniform vec3 uA; uniform vec3 uB; uniform float uOpen; uniform float uTime; uniform float uSeed; uniform float uMirror; uniform float uGain;
  ${NOISE}
  varying vec2 vUv;
  void main() {
    float x = vUv.x, y = vUv.y;
    float folds = noise(vec3(x * 6.0 + uSeed, uTime * 0.12, 0.0));
    float rays = 0.5 + 0.5 * sin(x * 90.0 + folds * 7.0 + uTime * 0.35);
    rays = 0.7 + 0.3 * rays * (0.5 + noise(vec3(x * 22.0, uTime * 0.3, uSeed)));
    float body = smoothstep(0.0, 0.03, y) * (pow(1.0 - y, 2.2) + exp(-y * 16.0) * 0.6);
    float edge = smoothstep(0.0, 0.12, x) * smoothstep(1.0, 0.88, x);
    float patches = smoothstep(0.28, 0.62, noise(vec3(x * 4.0 + uSeed, uTime * 0.06, 2.0)));   // gaps along the curtain
    float pulse = 0.7 + 0.3 * noise(vec3(x * 3.0, uTime * 0.2, uSeed + 5.0));
    vec3 col = mix(uA, uB, smoothstep(0.08, 0.7, y)) + uA * smoothstep(0.12, 0.0, y) * 1.1;   // the bright lower hem
    gl_FragColor = vec4(col * 1.25 * uGain, body * edge * rays * mix(0.45, 1.0, patches) * pulse * uOpen * uMirror);
    #include <colorspace_fragment>
  }`;
const LAKE_VERT = /* glsl */ `
  varying vec3 vW; varying float vDist;
  void main() {
    vec4 w = modelMatrix * vec4(position, 1.0);
    vW = w.xyz;
    vec4 mv = viewMatrix * w;
    vDist = length(mv.xyz);
    gl_Position = projectionMatrix * mv;
  }`;
const LAKE_FRAG = /* glsl */ `
  uniform vec3 uIce; uniform vec3 uGlow; uniform vec3 uHaze; uniform float uOpen; uniform float uReveal; uniform float uTime;
  ${NOISE}
  varying vec3 vW; varying float vDist;
  void main() {
    vec3 v = normalize(cameraPosition - vW);
    float fres = pow(1.0 - clamp(v.y, 0.0, 1.0), 3.0);
    vec2 q = vW.xz;
    float c1 = abs(noise(vec3(q * 0.16, 1.0)) - 0.5), c2 = abs(noise(vec3(q * 0.47, 7.0)) - 0.5);
    float crack = (smoothstep(0.022, 0.0, c1) * 0.9 + smoothstep(0.016, 0.0, c2) * 0.45) * (1.0 - smoothstep(18.0, 80.0, vDist));
    float glint = pow(noise(vec3(q * 5.0, uTime * 0.4)), 14.0) * 6.0 * (1.0 - smoothstep(6.0, 40.0, vDist));
    vec3 col = uIce + uGlow * (0.02 + 0.1 * fres) + vec3(0.7, 0.85, 1.0) * (crack * 0.22 + glint);
    float a = mix(0.82, 0.5, fres) + noise(vec3(q * 2.0, 3.0)) * 0.06;   // the mirror shows more at grazing angles
    float fog = smoothstep(90.0, 260.0, vDist);
    col = mix(col, uHaze, fog);
    a = max(a, fog * 0.85);
    float reveal = 1.0 - smoothstep(uReveal - 14.0, uReveal, vDist);
    gl_FragColor = vec4(col, a * uOpen * reveal);
    #include <colorspace_fragment>
  }`;

export function aurora(scene: THREE.Scene, opts: Opts): Env {
  const root = new THREE.Group();
  scene.add(root);
  const glowTex = glowTexture();

  const skyU = { uTop: { value: new THREE.Color() }, uHorizon: { value: new THREE.Color() }, uGlow: { value: new THREE.Color() },
    uOpen: { value: 0 }, uSeed: { value: 0 } };
  const sky = new THREE.ShaderMaterial({ vertexShader: SKY_VERT, fragmentShader: NIGHT, side: THREE.BackSide, transparent: true, depthWrite: false, uniforms: skyU });
  const skyMesh = new THREE.Mesh(new THREE.SphereGeometry(300, 32, 16), sky);
  skyMesh.renderOrder = -3;
  root.add(skyMesh);

  // what the lake mirrors lives in `mirror` (scaled -1 in y: three flips the faces' winding by itself)
  const world = new THREE.Group(), mirror = new THREE.Group();
  mirror.scale.y = -1;
  root.add(world, mirror);

  // three curtains, each a wide ribbon that folds; the mirrored copies share their uniforms, only dimmer
  const curtainGeo = new THREE.PlaneGeometry(1, 1, opts.small ? 80 : 140, 1);
  curtainGeo.translate(0, 0.5, 0);
  const curtains: { mesh: THREE.Mesh; ghost: THREE.Mesh; u: Record<string, THREE.IUniform> }[] = [];
  for (let k = 0; k < 4; k++) {
    const u = { uA: { value: new THREE.Color() }, uB: { value: new THREE.Color() }, uOpen: { value: 0 }, uTime: { value: 0 }, uSeed: { value: k * 3.1 },
      uGain: { value: 1 } };
    const mat = (m: number) => new THREE.ShaderMaterial({ vertexShader: CURTAIN_VERT, fragmentShader: CURTAIN_FRAG, uniforms: { ...u, uMirror: { value: m } },
      transparent: true, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending });
    const mesh = new THREE.Mesh(curtainGeo, mat(1)), ghost = new THREE.Mesh(curtainGeo, mat(0.42));
    mesh.renderOrder = ghost.renderOrder = -2;
    world.add(mesh);
    mirror.add(ghost);
    curtains.push({ mesh, ghost, u });
  }

  // a far shell of crisp stars above the horizon, and the moon
  const r0 = rng(31);
  const nStars = opts.small ? 350 : 800;
  const starPos = new Float32Array(nStars * 3);
  for (let i = 0; i < nStars; i++) {
    const u = 0.08 + r0() * 0.92, a = r0() * Math.PI * 2, rr = Math.sqrt(1 - u * u);
    starPos.set([Math.cos(a) * rr * 280, u * 280, Math.sin(a) * rr * 280], i * 3);
  }
  const starGeo = new THREE.BufferGeometry();
  starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  const starMat = new THREE.PointsMaterial({ size: 1.3, sizeAttenuation: false, transparent: true, depthWrite: false, color: 0xffffff });
  root.add(new THREE.Points(starGeo, starMat));
  const moonMat = new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true });
  const moonCore = new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true, color: 0xffffff });
  const moon = new THREE.Sprite(moonMat), core = new THREE.Sprite(moonCore);
  moon.scale.setScalar(48);
  core.scale.setScalar(11);
  root.add(moon, core);

  const hemi = new THREE.HemisphereLight(0xffffff, 0x000000, 1.5);
  const moonLight = new THREE.DirectionalLight(0xffffff, 2.4);
  root.add(hemi, moonLight, moonLight.target);

  // the frozen lake over the mirrored world
  const lakeU = { uIce: { value: new THREE.Color() }, uGlow: { value: new THREE.Color() }, uHaze: { value: new THREE.Color() },
    uOpen: { value: 0 }, uReveal: { value: 0 }, uTime: { value: 0 } };
  const lake = new THREE.ShaderMaterial({ vertexShader: LAKE_VERT, fragmentShader: LAKE_FRAG, transparent: true, depthWrite: false, uniforms: lakeU });
  const lakeGeo = new THREE.PlaneGeometry(620, 620);
  lakeGeo.rotateX(-Math.PI / 2);
  lakeGeo.translate(0, 0, -220);
  const lakeMesh = new THREE.Mesh(lakeGeo, lake);
  lakeMesh.renderOrder = 5;
  root.add(lakeMesh);

  // the far shore: snow from a jagged edge out to the horizon
  const shoreGeo = new THREE.PlaneGeometry(900, 300, 90, 1).toNonIndexed();
  shoreGeo.rotateX(-Math.PI / 2);
  shoreGeo.translate(0, 0.35, -285);
  moveCorners(shoreGeo, (x, y, z) => [x, y, z > -140 ? z - 6 * (0.5 + 0.5 * Math.sin(x * 0.07) * Math.cos(x * 0.023)) - r0() * 7 : z]);
  shoreGeo.computeVertexNormals();
  const shoreMat = new THREE.MeshStandardMaterial({ roughness: 1, metalness: 0 });
  root.add(new THREE.Mesh(shoreGeo, shoreMat));

  // snow drifting down
  const nSnow = opts.small ? 160 : 380;
  const snow = new Float32Array(nSnow * 3), snowSeed = new Float32Array(nSnow);
  for (let i = 0; i < nSnow; i++) {
    snow.set([(r0() - 0.4) * 90, r0() * 34, -4 - r0() * 80], i * 3);
    snowSeed[i] = r0();
  }
  const snowGeo = new THREE.BufferGeometry();
  snowGeo.setAttribute('position', new THREE.BufferAttribute(snow.slice(), 3));
  const snowMat = new THREE.PointsMaterial({ map: glowTex, size: 0.32, transparent: true, depthWrite: false, color: 0xffffff });
  root.add(new THREE.Points(snowGeo, snowMat));

  // per game: the peaks (and their reflections) and the ice shards
  const peakMat = new THREE.MeshStandardMaterial({ vertexColors: true, flatShading: true, roughness: 0.85, metalness: 0 });
  const fill = new THREE.DirectionalLight(0xffffff, 1.3);   // the aurora's light on the faces toward the viewer
  fill.position.set(-40, 50, 120);
  root.add(fill, fill.target);
  const shardMat = new THREE.MeshStandardMaterial({ flatShading: true, roughness: 0.15, metalness: 0.2, transparent: true, opacity: 0.85 });
  const shardLine = new THREE.LineBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const peaks = new THREE.Group(), peaksMirror = new THREE.Group(), shards = new THREE.Group();
  world.add(peaks, shards);
  mirror.add(peaksMirror);
  let peakList: { obj: THREE.Object3D; ghost: THREE.Object3D; size: number }[] = [];
  let shardList: { obj: THREE.Object3D; base: THREE.Vector3; spin: THREE.Vector3; bob: number; size: number }[] = [];
  const clearGame = () => {
    for (const g of [peaks, shards]) g.traverse((o) => (o as THREE.Mesh).geometry?.dispose());
    peaks.clear(); peaksMirror.clear(); shards.clear();
    peakList = []; shardList = [];
  };

  function peakGeometry(r: () => number, rock: THREE.Color, snowC: THREE.Color) {
    // a jagged cone with a snow cap: whole faces are snow or rock, so the cap's edge stays crisp
    const sides = 6 + Math.floor(r() * 3);
    const geo = new THREE.ConeGeometry(0.6, 1, sides, 3).toNonIndexed();
    geo.translate(0, 0.5, 0);
    moveCorners(geo, (x, y, z) => {
      if (y > 0.99) return [x + (r() - 0.5) * 0.12, y, z + (r() - 0.5) * 0.12];
      const k = 1 + (r() - 0.5) * 0.2;
      return [x * k + (r() - 0.5) * 0.05, y < 0.01 ? 0 : y * (1 + (r() - 0.5) * 0.12), z * k];
    });
    const p = geo.attributes.position as THREE.BufferAttribute;
    const colors = new Float32Array(p.count * 3);
    const line = 0.22 + r() * 0.14;
    for (let i = 0; i < p.count; i += 3) {
      const cy = (p.getY(i) + p.getY(i + 1) + p.getY(i + 2)) / 3;
      const c = cy > line + Math.sin(i * 1.7) * 0.06 || cy < 0.12 ? snowC : rock;
      for (let j = 0; j < 3; j++) colors.set([c.r, c.g, c.b], (i + j) * 3);
    }
    geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    geo.computeVertexNormals();
    return geo;
  }

  return {
    kind: 'aurora',
    camera: { y: 2.6, pitch: 0.08, fov: 60 },
    setGame(title) {
      const { h: h1, h2, s } = palette(title);
      const r = rng(s * 6151 + h1 * 17);
      const ha = h1, hb = h2, night = 225 + (h1 % 50) - 25;
      skyU.uTop.value.copy(hsl(night, 0.5, 0.025));
      skyU.uHorizon.value.copy(hsl(night, 0.4, 0.085));
      skyU.uGlow.value.copy(hsl(ha, 0.9, 0.5));
      skyU.uSeed.value = r() * 30;
      curtains.forEach((c, k) => {
        (c.u.uA.value as THREE.Color).copy(hsl(k === 1 ? hb : ha, 0.95, 0.55));
        (c.u.uB.value as THREE.Color).copy(hsl((k === 1 ? ha : hb) + 30, 0.85, 0.45));
        c.u.uSeed.value = r() * 20;
        // wide ribbons over the far shore, mostly right (left is behind the portal), their hems above the peaks;
        // the last one hangs high and leans over the viewer, so the upper sky glows in any framing
        const over = k === 3;
        const w = over ? 170 + r() * 40 : 150 + r() * 70, hgt = over ? 70 : 60 + r() * 25;
        const x = over ? 30 + r() * 20 : 25 + (k - 1) * 45 + (r() - 0.5) * 20;
        const y = over ? 34 + r() * 6 : 22 + k * 5 + r() * 4, z = over ? -80 - r() * 15 : -135 - k * 28 - r() * 15;
        const ry = (r() - 0.5) * 0.5;
        c.u.uGain.value = over ? 1.1 : 1.25;
        for (const m of [c.mesh, c.ghost]) {
          m.scale.set(w, hgt, 1);
          m.position.set(x, y, z);
          m.rotation.set(over ? 0.62 : 0, ry, 0);
        }
      });
      const moonDir = new THREE.Vector3(0.55 + r() * 0.3, 0.42 + r() * 0.15, -1).normalize();
      moon.position.copy(moonDir).multiplyScalar(250);
      core.position.copy(moon.position);
      moonMat.color.copy(hsl(hb, 0.4, 0.55));
      moonLight.position.copy(moonDir).multiplyScalar(100);
      moonLight.color.copy(hsl(night, 0.25, 0.85));
      hemi.color.copy(hsl(ha, 0.6, 0.4));
      hemi.groundColor.copy(hsl(night, 0.4, 0.05));
      lakeU.uIce.value.copy(hsl(night, 0.45, 0.02));
      lakeU.uGlow.value.copy(hsl(ha, 0.9, 0.5));
      fill.color.copy(hsl(ha, 0.5, 0.75));
      peakMat.emissive.copy(hsl(night, 0.3, 0.035));
      lakeU.uHaze.value.copy(hsl(night, 0.4, 0.085));
      shoreMat.color.copy(hsl(night, 0.15, 0.78));
      snowMat.color.copy(hsl(hb, 0.3, 0.9));
      shardMat.color.copy(hsl(ha + 10, 0.5, 0.62));
      shardMat.emissive.copy(hsl(ha, 0.9, 0.12));
      shardLine.color.copy(hsl(ha, 1, 0.65));

      clearGame();
      const rock = hsl(night + 10, 0.22, 0.2), snowC = hsl(night, 0.1, 0.97);
      const n = (opts.small ? 6 : 9) + Math.floor(r() * 3);
      for (let i = 0; i < n; i++) {
        const geo = peakGeometry(r, rock, snowC);
        const mesh = new THREE.Mesh(geo, peakMat), ghost = new THREE.Mesh(geo, peakMat);
        const z = -175 - r() * 100;
        const x = (r() < 0.8 ? 1 : -1) * (r() * 0.5 * -z) + 25;
        const size = 14 + r() * 22 + (-z) * 0.07;
        for (const m of [mesh, ghost]) {
          m.position.set(x, 0, z);
          m.rotation.y = r() * 6.28;
        }
        mesh.scale.set(size * (0.8 + r() * 0.5), size, size * (0.8 + r() * 0.4));
        ghost.scale.copy(mesh.scale);
        ghost.rotation.y = mesh.rotation.y;
        peaks.add(mesh);
        peaksMirror.add(ghost);
        peakList.push({ obj: mesh, ghost, size });
      }
      for (let i = 0; i < (opts.small ? 5 : 8); i++) {
        const geo = moveCorners(new THREE.OctahedronGeometry(1, 0), (x, y, z) => [x * (0.7 + r() * 0.5), y * (2 + r() * 1.2), z * (0.7 + r() * 0.5)]);
        geo.computeVertexNormals();
        const g = new THREE.Group();
        g.add(new THREE.Mesh(geo, shardMat), new THREE.LineSegments(new THREE.EdgesGeometry(geo), shardLine));
        const z = -26 - r() * 50;
        const base = new THREE.Vector3((i === 0 || r() < 0.8 ? 1 : -1) * (4 + r() * 0.45 * -z), 3 + r() * 7, z);
        g.position.copy(base);
        shards.add(g);
        shardList.push({ obj: g, base, spin: new THREE.Vector3(r() - 0.5, r() - 0.5, r() - 0.5).multiplyScalar(0.5), bob: r() * 6.28, size: 0.6 + r() * 0.8 });
      }
    },
    update(t, st) {
      const tt = st.still ? 4 : t;
      skyU.uOpen.value = st.open;
      curtains.forEach((c) => { c.u.uOpen.value = st.open; c.u.uTime.value = tt; });
      lakeU.uOpen.value = st.open;
      lakeU.uReveal.value = 8 + st.reveal * 320;
      lakeU.uTime.value = tt;
      starMat.opacity = st.open * (st.still ? 0.8 : 0.65 + 0.15 * Math.sin(t * 1.3));
      moonMat.opacity = st.open * 0.5;
      moonCore.opacity = st.open;
      snowMat.opacity = st.open * 0.85;
      keepAlpha(peakMat, st.open);
      peakMat.transparent = st.open < 1;
      keepAlpha(shardMat, st.open * 0.85);
      keepAlpha(shoreMat, st.open); shoreMat.transparent = st.open < 1;
      shardLine.opacity = st.open * 0.8;
      const g = Math.max(st.grow, 0.0001);
      for (const p of peakList) { p.obj.scale.y = p.size * (0.35 + 0.65 * g); p.ghost.scale.y = p.obj.scale.y; }
      for (const sh of shardList) {
        sh.obj.scale.setScalar(sh.size * g);
        sh.obj.rotation.set(sh.spin.x * tt + 0.3, sh.spin.y * tt * 2, sh.spin.z * tt);
        sh.obj.position.y = sh.base.y + (st.still ? 0 : Math.sin(t * 0.6 + sh.bob) * 0.7);
      }
      if (!st.still) {
        const p = snowGeo.attributes.position as THREE.BufferAttribute;
        for (let i = 0; i < nSnow; i++) {
          const sp = 0.8 + snowSeed[i] * 1.2;
          p.setY(i, ((snow[i * 3 + 1] - t * sp) % 34 + 34) % 34);
          p.setX(i, snow[i * 3] + Math.sin(t * 0.5 + snowSeed[i] * 20) * 1.2);
        }
        p.needsUpdate = true;
      }
    },
    dispose() { clearGame(); disposeAll(root, [glowTex]); },
  };
}

// ------------------------------------------------------------------ reef
const WATER = /* glsl */ `
  uniform vec3 uDeep; uniform vec3 uShallow; uniform vec3 uLight; uniform float uOpen; uniform float uTime;
  ${NOISE}
  varying vec3 vDir;
  void main() {
    vec3 d = normalize(vDir);
    float y = d.y;
    vec3 c = mix(uDeep, uShallow, smoothstep(-0.3, 0.75, y));
    // the bright surface overhead (Snell's window), rippling
    float win = smoothstep(0.42, 0.9, y);
    vec2 q = d.xz / max(y, 0.2) * 3.0;
    float rip = noise(vec3(q * 1.4, uTime * 0.3)) * 0.6 + noise(vec3(q * 4.2, uTime * 0.55)) * 0.4;
    c += uLight * win * (0.3 + 0.7 * rip * rip) * 1.1;
    c *= 1.0 - smoothstep(0.0, -0.6, y) * 0.55;
    gl_FragColor = vec4(c, uOpen);
    #include <colorspace_fragment>
  }`;
// the sea floor's height, the same in the shader and in JS (kelp and rocks stand on it)
const FLOOR_H = (x: number, z: number) => Math.sin(x * 0.08) * Math.cos(z * 0.06) * 1.4 + Math.sin(x * 0.21 + z * 0.13) * 0.5 - 0.6;
const FLOOR_VERT = /* glsl */ `
  varying vec3 vW; varying float vDist;
  void main() {
    vec4 w = modelMatrix * vec4(position, 1.0);
    w.y += sin(w.x * 0.08) * cos(w.z * 0.06) * 1.4 + sin(w.x * 0.21 + w.z * 0.13) * 0.5 - 0.6;
    vW = w.xyz;
    vec4 mv = viewMatrix * w;
    vDist = length(mv.xyz);
    gl_Position = projectionMatrix * mv;
  }`;
const FLOOR_FRAG = /* glsl */ `
  uniform vec3 uSand; uniform vec3 uLight; uniform vec3 uFog; uniform float uOpen; uniform float uReveal; uniform float uTime;
  ${NOISE}
  varying vec3 vW; varying float vDist;
  float caustic(vec2 p, float t) {
    float a = noise(vec3(p, t)), b = noise(vec3(p + 3.7, -t * 0.8 + 1.0));
    return pow(clamp(1.0 - abs(a - b) * 3.2, 0.0, 1.0), 7.0);
  }
  void main() {
    vec3 n = normalize(cross(dFdx(vW), dFdy(vW)));
    if (n.y < 0.0) n = -n;
    float lit = 0.35 + 0.65 * max(dot(n, normalize(vec3(0.3, 1.0, 0.2))), 0.0);
    vec3 col = uSand * lit * (0.85 + 0.3 * noise(vec3(vW.xz * 0.7, 2.0)));
    float c = caustic(vW.xz * 0.35, uTime * 0.45) + caustic(vW.xz * 0.8 + 9.0, uTime * 0.6) * 0.6;
    col += uLight * c * 0.5 * (1.0 - smoothstep(20.0, 90.0, vDist));
    col = mix(col, uFog, smoothstep(8.0, 120.0, vDist));
    float reveal = 1.0 - smoothstep(uReveal - 12.0, uReveal, vDist);
    gl_FragColor = vec4(col, uOpen * reveal);
    #include <colorspace_fragment>
  }`;
const SHAFT_VERT = /* glsl */ `
  varying vec2 vUv;
  void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const SHAFT_FRAG = /* glsl */ `
  uniform vec3 uCol; uniform float uOpen; uniform float uTime; uniform float uPh;
  varying vec2 vUv;
  void main() {
    float edge = pow(sin(vUv.x * 3.14159), 2.0);
    float len = smoothstep(0.0, 0.7, vUv.y);
    float flick = 0.6 + 0.4 * sin(uTime * 0.6 + uPh + vUv.y * 3.0);
    gl_FragColor = vec4(uCol, edge * len * flick * uOpen * 0.16);
    #include <colorspace_fragment>
  }`;
const KELP_VERT = /* glsl */ `
  uniform float uTime;
  varying float vH; varying float vDist; varying float vRib;
  void main() {
    vec3 p = position;
    float h = p.y;                                        // 0 at the root, 1 at the tip
    p.x *= 1.0 - h * 0.65;
    vec4 base = instanceMatrix * vec4(0.0, 0.0, 0.0, 1.0);
    float len = length(instanceMatrix[1].xyz);
    float ph = base.x * 0.37 + base.z * 0.21;
    vec4 w = modelMatrix * instanceMatrix * vec4(p, 1.0);
    w.x += sin(uTime * 0.8 + ph + h * 2.4) * h * h * len * 0.22;
    w.z += cos(uTime * 0.6 + ph * 1.3 + h * 1.8) * h * h * len * 0.12;
    vH = h; vRib = sin(h * len * 3.0 + ph);
    vec4 mv = viewMatrix * w;
    vDist = length(mv.xyz);
    gl_Position = projectionMatrix * mv;
  }`;
const KELP_FRAG = /* glsl */ `
  uniform vec3 uBase; uniform vec3 uTip; uniform vec3 uFog; uniform float uOpen;
  varying float vH; varying float vDist; varying float vRib;
  void main() {
    vec3 col = mix(uBase, uTip, vH) * (0.8 + 0.2 * vRib);
    col = mix(col, uFog, smoothstep(10.0, 110.0, vDist));
    gl_FragColor = vec4(col, uOpen);
    #include <colorspace_fragment>
  }`;
const JELLY_VERT = /* glsl */ `
  varying vec3 vN; varying vec3 vV; varying float vY;
  void main() {
    vN = normalize(normalMatrix * normal);
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vV = normalize(-mv.xyz);
    vY = position.y;
    gl_Position = projectionMatrix * mv;
  }`;
const JELLY_FRAG = /* glsl */ `
  uniform vec3 uCol; uniform float uOpen; uniform float uGlow;
  varying vec3 vN; varying vec3 vV; varying float vY;
  void main() {
    float rim = pow(1.0 - abs(dot(normalize(vN), normalize(vV))), 2.0);
    float rings = 0.75 + 0.25 * sin(vY * 30.0);
    vec3 col = uCol * (0.1 + rim * 0.85 * rings) * uGlow;
    gl_FragColor = vec4(col, uOpen);
    #include <colorspace_fragment>
  }`;

export function reef(scene: THREE.Scene, opts: Opts): Env {
  const root = new THREE.Group();
  scene.add(root);
  const glowTex = glowTexture();
  const prevFog = scene.fog;
  const fog = new THREE.FogExp2(0x000000, 0.02);
  scene.fog = fog;

  const waterU = { uDeep: { value: new THREE.Color() }, uShallow: { value: new THREE.Color() }, uLight: { value: new THREE.Color() },
    uOpen: { value: 0 }, uTime: { value: 0 } };
  const water = new THREE.ShaderMaterial({ vertexShader: SKY_VERT, fragmentShader: WATER, side: THREE.BackSide, transparent: true, depthWrite: false, uniforms: waterU });
  const waterMesh = new THREE.Mesh(new THREE.SphereGeometry(300, 32, 16), water);
  waterMesh.renderOrder = -3;
  root.add(waterMesh);

  const floorU = { uSand: { value: new THREE.Color() }, uLight: { value: new THREE.Color() }, uFog: { value: new THREE.Color() },
    uOpen: { value: 0 }, uReveal: { value: 0 }, uTime: { value: 0 } };
  const floor = new THREE.ShaderMaterial({ vertexShader: FLOOR_VERT, fragmentShader: FLOOR_FRAG, transparent: true, uniforms: floorU });
  const floorGeo = new THREE.PlaneGeometry(320, 320, opts.small ? 80 : 130, opts.small ? 80 : 130);
  floorGeo.rotateX(-Math.PI / 2);
  floorGeo.translate(0, 0, -130);
  const floorMesh = new THREE.Mesh(floorGeo, floor);
  floorMesh.renderOrder = -1;
  root.add(floorMesh);

  const hemi = new THREE.HemisphereLight(0xffffff, 0x000000, 1.2);
  const sunLight = new THREE.DirectionalLight(0xffffff, 1.5);
  sunLight.position.set(10, 60, -10);
  root.add(hemi, sunLight, sunLight.target);

  // light shafts slanting down from the surface
  const shafts: { mesh: THREE.Mesh; u: Record<string, THREE.IUniform> }[] = [];
  const shaftGeo = new THREE.PlaneGeometry(1, 1);
  shaftGeo.translate(0, 0.5, 0);
  for (let k = 0; k < (opts.small ? 5 : 8); k++) {
    const u = { uCol: { value: new THREE.Color() }, uOpen: { value: 0 }, uTime: { value: 0 }, uPh: { value: k * 1.9 } };
    const mesh = new THREE.Mesh(shaftGeo, new THREE.ShaderMaterial({ vertexShader: SHAFT_VERT, fragmentShader: SHAFT_FRAG, uniforms: u,
      transparent: true, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending }));
    mesh.renderOrder = 2;
    root.add(mesh);
    shafts.push({ mesh, u });
  }

  // kelp: one blade, many instances, swaying in the shader
  const bladeGeo = new THREE.PlaneGeometry(0.9, 1, 1, 14);
  bladeGeo.translate(0, 0.5, 0);
  const kelpU = { uBase: { value: new THREE.Color() }, uTip: { value: new THREE.Color() }, uFog: { value: new THREE.Color() },
    uOpen: { value: 0 }, uTime: { value: 0 } };
  const kelpMat = new THREE.ShaderMaterial({ vertexShader: KELP_VERT, fragmentShader: KELP_FRAG, uniforms: kelpU, side: THREE.DoubleSide, transparent: true });
  const nKelp = opts.small ? 70 : 150;
  const kelp = new THREE.InstancedMesh(bladeGeo, kelpMat, nKelp);
  kelp.frustumCulled = false;
  root.add(kelp);
  let kelpData: { p: THREE.Vector3; h: number; ry: number }[] = [];

  // jellyfish: a glowing bell that pulses, trailing tentacles, a halo
  const bellGeo = new THREE.SphereGeometry(1, 22, 10, 0, Math.PI * 2, 0, Math.PI * 0.52);
  const TENT = 6, SEG = 10;
  type Jelly = { g: THREE.Group; bell: THREE.Mesh; u: Record<string, THREE.IUniform>; halo: THREE.Sprite; lines: THREE.Line[];
    base: THREE.Vector3; ph: number; size: number };
  const jellies: Jelly[] = [];
  const tentMat = new THREE.LineBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  for (let k = 0; k < (opts.small ? 5 : 8); k++) {
    const u = { uCol: { value: new THREE.Color() }, uOpen: { value: 0 }, uGlow: { value: 1 } };
    const bell = new THREE.Mesh(bellGeo, new THREE.ShaderMaterial({ vertexShader: JELLY_VERT, fragmentShader: JELLY_FRAG, uniforms: u,
      transparent: true, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending }));
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true, fog: false }));
    halo.scale.setScalar(5);
    const g = new THREE.Group();
    g.add(halo, bell);
    const lines: THREE.Line[] = [];
    for (let i = 0; i < TENT; i++) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(SEG * 3), 3));
      const line = new THREE.Line(geo, tentMat);
      line.frustumCulled = false;
      g.add(line);
      lines.push(line);
    }
    root.add(g);
    jellies.push({ g, bell, u, halo, lines, base: new THREE.Vector3(), ph: k * 1.7, size: 1 });
  }

  // a school of small fish circling
  const nFish = opts.small ? 36 : 80;
  const fishGeo = new THREE.ConeGeometry(0.13, 0.55, 4);
  fishGeo.rotateZ(-Math.PI / 2);
  const fishMat = new THREE.MeshStandardMaterial({ flatShading: true, roughness: 0.35, metalness: 0.6, transparent: true });
  const fish = new THREE.InstancedMesh(fishGeo, fishMat, nFish);
  fish.frustumCulled = false;
  root.add(fish);
  const fishSeed = Array.from({ length: nFish }, (_, i) => { const r = rng(900 + i); return [r(), r(), r()]; });
  const school = { c: new THREE.Vector3(), rx: 8, rz: 5 };

  // rocks with coral, per game
  const rockMat = new THREE.MeshStandardMaterial({ vertexColors: true, flatShading: true, roughness: 1, metalness: 0 });
  const coralMat = new THREE.MeshStandardMaterial({ flatShading: true, roughness: 0.7, metalness: 0 });
  const rocks = new THREE.Group();
  root.add(rocks);
  let rockList: { obj: THREE.Object3D; size: number }[] = [];
  const clearGame = () => {
    rocks.traverse((o) => (o as THREE.Mesh).geometry?.dispose());
    rocks.clear();
    rockList = [];
  };

  // bubbles rising from vents, and marine snow drifting
  const r0 = rng(57);
  const nBub = opts.small ? 60 : 140;
  const bub = new Float32Array(nBub * 3), bubSeed = new Float32Array(nBub), vents: THREE.Vector3[] = [];
  const bubGeo = new THREE.BufferGeometry();
  bubGeo.setAttribute('position', new THREE.BufferAttribute(bub, 3));
  const bubMat = new THREE.PointsMaterial({ map: glowTex, size: 0.32, transparent: true, depthWrite: false, color: 0xffffff, blending: THREE.AdditiveBlending });
  const bubbles = new THREE.Points(bubGeo, bubMat);
  bubbles.frustumCulled = false;
  root.add(bubbles);
  for (let i = 0; i < nBub; i++) bubSeed[i] = r0();
  const nSnow = opts.small ? 180 : 420;
  const snow = new Float32Array(nSnow * 3);
  for (let i = 0; i < nSnow; i++) snow.set([(r0() - 0.4) * 80, r0() * 26, -3 - r0() * 70], i * 3);
  const snowGeo = new THREE.BufferGeometry();
  snowGeo.setAttribute('position', new THREE.BufferAttribute(snow.slice(), 3));
  const snowMat = new THREE.PointsMaterial({ map: glowTex, size: 0.14, transparent: true, depthWrite: false, color: 0xffffff });
  root.add(new THREE.Points(snowGeo, snowMat));

  const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), e = new THREE.Euler(), sc = new THREE.Vector3(), pv = new THREE.Vector3();

  return {
    kind: 'reef',
    camera: { y: 4.2, pitch: 0.03, fov: 62 },
    setGame(title) {
      const { h: h1, h2, s } = palette(title);
      const r = rng(s * 3571 + h1 * 23);
      const water0 = 185 + ((h1 - 185) * 0.25);           // always sea-coloured, leaning toward the game's hue
      waterU.uDeep.value.copy(hsl(water0 + 20, 0.7, 0.04));
      waterU.uShallow.value.copy(hsl(water0, 0.65, 0.26));
      waterU.uLight.value.copy(hsl(h2 * 0.2 + 160, 0.6, 0.62));
      const fogC = hsl(water0 + 8, 0.6, 0.1);
      fog.color.copy(fogC);
      floorU.uSand.value.copy(hsl(40 + (h2 % 30), 0.3, 0.25));
      floorU.uLight.value.copy(hsl(water0 - 20, 0.6, 0.72));
      floorU.uFog.value.copy(fogC);
      kelpU.uFog.value.copy(fogC);
      kelpU.uBase.value.copy(hsl(110 + (h1 % 40), 0.45, 0.08));
      kelpU.uTip.value.copy(hsl(95 + (h2 % 50), 0.55, 0.3));
      hemi.color.copy(hsl(water0, 0.6, 0.6));
      hemi.groundColor.copy(hsl(40, 0.3, 0.12));
      shafts.forEach((sh, k) => {
        (sh.u.uCol.value as THREE.Color).copy(hsl(water0 - 15, 0.5, 0.75));
        // tall slanted planes, mostly right of the seam, between the viewer and the far kelp
        sh.mesh.position.set(-6 + r() * 60 + k * 3, -2, -18 - r() * 70);
        sh.mesh.scale.set(3 + r() * 6, 70, 1);
        sh.mesh.rotation.set(0, (r() - 0.5) * 0.4, 0.22 + r() * 0.12);
      });
      jellies.forEach((j, k) => {
        const c = hsl(k % 2 ? h2 : h1 + 30, 1, 0.55);
        (j.u.uCol.value as THREE.Color).copy(c);
        (j.halo.material as THREE.SpriteMaterial).color.copy(c);
        j.base.set((k === 0 || r() < 0.8 ? 1 : -1) * (3 + r() * 20), 5 + r() * 11, -12 - r() * 42);
        j.size = 0.7 + r() * 1.3;
        j.ph = r() * 6.28;
      });
      tentMat.color.copy(hsl(h1 + 30, 0.9, 0.6));
      fishMat.color.copy(hsl(h2 + 180, 0.5, 0.62));
      school.c.set(10 + r() * 10, 6 + r() * 4, -26 - r() * 14);
      coralMat.color.copy(hsl(h2, 0.75, 0.55));
      coralMat.emissive.copy(hsl(h2, 0.9, 0.12));

      kelpData = [];
      vents.length = 0;
      const clumps = opts.small ? 10 : 18;
      for (let c = 0; c < clumps && kelpData.length < nKelp; c++) {
        const z = -10 - r() * 95;
        const x = (r() < 0.78 ? 1 : -1) * (3 + r() * 0.75 * -z) + 2;
        const n = 4 + Math.floor(r() * 7);
        for (let i = 0; i < n && kelpData.length < nKelp; i++) {
          const px = x + (r() - 0.5) * 3, pz = z + (r() - 0.5) * 3;
          kelpData.push({ p: new THREE.Vector3(px, FLOOR_H(px, pz), pz), h: 5 + r() * 12, ry: r() * 6.28 });
        }
        if (c % 3 === 0) vents.push(new THREE.Vector3(x, FLOOR_H(x, z), z));
      }
      kelp.count = kelpData.length;

      clearGame();
      const rock = hsl(h1 + 200, 0.15, 0.16), moss = hsl(120 + (h1 % 40), 0.3, 0.2);
      for (let i = 0; i < (opts.small ? 6 : 10); i++) {
        const geo = moveCorners(new THREE.IcosahedronGeometry(1, 1), (x, y, z) => {
          const k = 1 + (r() - 0.5) * 0.4;
          return [x * k, Math.max(y, -0.3) * 0.6 * (1 + (r() - 0.5) * 0.3), z * k];
        });
        const p = geo.attributes.position as THREE.BufferAttribute;
        const colors = new Float32Array(p.count * 3);
        for (let v = 0; v < p.count; v += 3) {
          const c = (p.getY(v) + p.getY(v + 1) + p.getY(v + 2)) / 3 > 0.3 ? moss : rock;
          for (let j = 0; j < 3; j++) colors.set([c.r, c.g, c.b], (v + j) * 3);
        }
        geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
        geo.computeVertexNormals();
        const g = new THREE.Group();
        g.add(new THREE.Mesh(geo, rockMat));
        for (let k = 2 + Math.floor(r() * 4); k > 0; k--) {
          const cg = new THREE.DodecahedronGeometry(0.16 + r() * 0.14, 0);
          const coral = new THREE.Mesh(cg, coralMat);
          coral.position.set((r() - 0.5) * 1.2, 0.45 + r() * 0.2, (r() - 0.5) * 1.2);
          coral.scale.y = 1.4 + r() * 1.4;
          g.add(coral);
        }
        const z = -12 - r() * 70;
        const x = (r() < 0.75 ? 1 : -1) * (3 + r() * 0.6 * -z);
        g.position.set(x, FLOOR_H(x, z), z);
        g.rotation.y = r() * 6.28;
        rocks.add(g);
        rockList.push({ obj: g, size: 1.2 + r() * 2.4 });
        if (i % 2 === 0) vents.push(new THREE.Vector3(x, FLOOR_H(x, z) + 0.6, z));
      }
      for (let i = 0; i < nBub; i++) {
        const v = vents[i % vents.length];
        bub.set([v.x, v.y, v.z], i * 3);
      }
      (bubGeo.attributes.position as THREE.BufferAttribute).needsUpdate = true;
      bubMat.color.copy(hsl(water0 - 20, 0.4, 0.9));
      snowMat.color.copy(hsl(water0, 0.3, 0.85));
    },
    update(t, st) {
      const tt = st.still ? 3 : t;
      waterU.uOpen.value = st.open;
      waterU.uTime.value = tt;
      floorU.uOpen.value = st.open;
      floorU.uReveal.value = 8 + st.reveal * 300;
      floorU.uTime.value = tt;
      kelpU.uOpen.value = st.open;
      kelpU.uTime.value = tt;
      kelpMat.transparent = st.open < 1;
      shafts.forEach((sh) => { sh.u.uOpen.value = st.open; sh.u.uTime.value = tt; });
      bubMat.opacity = st.open * 0.8;
      snowMat.opacity = st.open * 0.5;
      tentMat.opacity = st.open * 0.55;
      keepAlpha(rockMat, st.open); rockMat.transparent = st.open < 1;
      keepAlpha(coralMat, st.open); coralMat.transparent = st.open < 1;
      fishMat.opacity = st.open;
      const g = Math.max(st.grow, 0.0001);

      kelpData.forEach((k, i) => {
        q.setFromEuler(e.set(0, k.ry, 0));
        sc.set(1, k.h * (0.25 + 0.75 * g), 1);
        kelp.setMatrixAt(i, m4.compose(k.p, q, sc));
      });
      kelp.instanceMatrix.needsUpdate = true;
      for (const rk of rockList) rk.obj.scale.setScalar(rk.size * (0.3 + 0.7 * g));

      for (const j of jellies) {
        const ph = tt * 1.5 + j.ph;
        const beat = Math.max(0, Math.sin(ph));                       // contracts, then drifts open
        const rise = st.still ? 0 : ((t * 0.35 + j.ph * 3) % 18);
        j.g.position.set(j.base.x + Math.sin(tt * 0.2 + j.ph) * 1.5, j.base.y + rise - 6 * (st.still ? 0 : 1), j.base.z);
        j.g.scale.setScalar(j.size * g);
        j.bell.scale.set(1 - beat * 0.16, 0.85 + beat * 0.2, 1 - beat * 0.16);
        j.u.uOpen.value = st.open * Math.min(1, rise / 2) * Math.min(1, (18 - rise) / 3) + (st.still ? st.open : 0);
        j.u.uGlow.value = 0.8 + beat * 0.5;
        (j.halo.material as THREE.SpriteMaterial).opacity = (j.u.uOpen.value as number) * (0.25 + beat * 0.2);
        j.lines.forEach((line, i) => {
          const a = (i / TENT) * Math.PI * 2;
          const p = line.geometry.attributes.position as THREE.BufferAttribute;
          for (let k = 0; k < SEG; k++) {
            const f = k / (SEG - 1);
            const sway = Math.sin(tt * 1.4 + j.ph + i * 0.7 - f * 3) * f * 0.5;
            p.setXYZ(k, Math.cos(a) * (0.75 - f * 0.25) + sway, -0.05 - f * 3.4 - beat * f * 0.4, Math.sin(a) * (0.75 - f * 0.25) + sway * 0.4);
          }
          p.needsUpdate = true;
        });
      }

      for (let i = 0; i < nFish; i++) {
        const [a, b, c] = fishSeed[i];
        const ang = tt * (0.28 + b * 0.05) + a * 1.4;
        pv.set(school.c.x + Math.cos(ang) * (school.rx + c * 3), school.c.y + Math.sin(ang * 2 + b * 6) * 0.8 + (b - 0.5) * 3,
          school.c.z + Math.sin(ang) * (school.rz + c * 2));
        q.setFromEuler(e.set(0, -ang - Math.PI / 2, Math.sin(tt * 6 + a * 20) * 0.08));
        sc.setScalar(g * (0.7 + c * 0.6));
        fish.setMatrixAt(i, m4.compose(pv, q, sc));
      }
      fish.instanceMatrix.needsUpdate = true;

      if (!st.still) {
        const p = bubGeo.attributes.position as THREE.BufferAttribute;
        for (let i = 0; i < nBub; i++) {
          const v = vents[i % Math.max(1, vents.length)];
          if (!v) break;
          const y = (bubSeed[i] * 24 + t * (1.6 + bubSeed[i] * 1.6)) % 24;
          p.setXYZ(i, v.x + Math.sin(t * 2 + bubSeed[i] * 40) * 0.25 * (1 + y * 0.05), v.y + y, v.z + Math.cos(t * 1.7 + bubSeed[i] * 30) * 0.25);
        }
        p.needsUpdate = true;
        const sp = snowGeo.attributes.position as THREE.BufferAttribute;
        for (let i = 0; i < nSnow; i++) {
          sp.setY(i, ((snow[i * 3 + 1] - t * 0.25 + Math.sin(t * 0.3 + i) * 0.4) % 26 + 26) % 26);
          sp.setX(i, snow[i * 3] + Math.sin(t * 0.2 + i * 0.7) * 0.8);
        }
        sp.needsUpdate = true;
      }
    },
    dispose() {
      clearGame();
      if (scene.fog === fog) scene.fog = prevFog;
      disposeAll(root, [glowTex]);
    },
  };
}
