// Two neon worlds for envs.ts (same Env contract: built once, recoloured and re-seeded per game, freed in dispose()):
//   city — a rain-soaked megacity at night: an avenue of towers with lit window grids and neon crowns, blade signs
//          and animated billboards, flying traffic, a hologram over the street, the wet street mirroring it all
//   tron — inside a digital arena: light cycles racing and leaving glowing walls that fade, a glossy circuit floor
//          with running pulses, the arena's stands and halo ring, falling data streams, a recognizer hovering
import * as THREE from 'three';
import type { Env } from './envs';
import { palette, rng } from './types';
import { canvasTexture, disposeAll, glowTexture, hsl, NOISE, SKY_VERT } from './envkit';

type Opts = { small: boolean };
// a hash without sin() (sin of big arguments turns tiny interpolation differences into per-pixel noise)
const HASH2 = /* glsl */ `float hh(vec2 p) { vec3 q = fract(vec3(p.xyx) * 0.1031); q += dot(q, q.yzx + 33.33); return fract((q.x + q.y) * q.z); }`;

// ------------------------------------------------------------------ city
const CITY_SKY = /* glsl */ `
  uniform vec3 uTop; uniform vec3 uGlowA; uniform vec3 uGlowB; uniform float uOpen; uniform float uTime; uniform float uSeed;
  ${NOISE}
  varying vec3 vDir;
  void main() {
    vec3 d = normalize(vDir);
    float y = d.y;
    vec3 c = uTop * (0.55 + 0.45 * smoothstep(0.0, 0.6, y));
    float haze = exp(-max(y, 0.0) * 5.0);                                   // the city's light on the smog
    c += mix(uGlowA, uGlowB, 0.5 + 0.5 * sin(d.x * 2.2 + uSeed)) * haze * 0.5;
    float cl = fbm(vec3(d.xz / max(y, 0.08) * 0.5 + uSeed, uTime * 0.02));
    c += mix(uGlowA, uGlowB, 0.3) * pow(cl, 2.6) * smoothstep(0.03, 0.4, y) * 0.55;   // low clouds lit from below
    c = mix(c, uGlowB * 0.12, smoothstep(0.0, -0.1, y));
    gl_FragColor = vec4(c, uOpen);
    #include <colorspace_fragment>
  }`;
const TOWER_VERT = /* glsl */ `
  varying vec3 vI; varying vec3 vN; varying float vSeed; varying float vDist; varying float vLy;
  void main() {
    vec4 iw = instanceMatrix * vec4(position, 1.0);        // before the mirror: the reflection shows the same windows
    vI = iw.xyz;
    vN = normalize(mat3(instanceMatrix) * normal);
    vec4 b = instanceMatrix * vec4(0.0, 0.0, 0.0, 1.0);
    vSeed = fract(sin(dot(b.xz, vec2(12.9898, 78.233))) * 43758.5453);
    vLy = position.y;
    vec4 mv = viewMatrix * modelMatrix * iw;
    vDist = length(mv.xyz);
    gl_Position = projectionMatrix * mv;
  }`;
const TOWER_FRAG = /* glsl */ `
  uniform vec3 uBody; uniform vec3 uWarm; uniform vec3 uCool; uniform vec3 uNeonA; uniform vec3 uNeonB; uniform vec3 uFog;
  uniform float uOpen; uniform float uTime; uniform float uMirror;
  varying vec3 vI; varying vec3 vN; varying float vSeed; varying float vDist; varying float vLy;
  ${HASH2}
  void main() {
    vec3 n = normalize(vN);
    float seed = floor(vSeed * 1024.0 + 0.5);                     // the same on every pixel of a tower
    vec3 col = uBody * (0.55 + 0.25 * abs(n.x) + 0.2 * max(n.y, 0.0));
    if (abs(n.y) < 0.5) {
      float u = abs(n.x) > 0.5 ? vI.z : vI.x;
      vec2 cell = vec2(u / 0.7, vI.y / 0.95);
      vec2 f = fract(cell), id = floor(cell);
      float win = step(0.2, f.x) * step(f.x, 0.8) * step(0.24, f.y) * step(f.y, 0.76);
      float r = hh(id + vec2(seed * 7.0, seed * 3.0));
      float floorLit = step(0.3, hh(vec2(id.y * 1.7, seed + 5.0)));          // whole floors lit or dark
      float lit = step(0.48, r) * floorLit;
      vec3 wc = r > 0.94 ? (r > 0.97 ? uNeonA : uNeonB) : (r > 0.8 ? uCool : uWarm);
      float blink = r > 0.985 ? step(0.5, fract(uTime * 0.7 + r * 9.0)) : 1.0;
      col += wc * win * lit * blink * (0.55 + 0.6 * hh(id + 3.1));
      float crown = smoothstep(0.972, 0.98, vLy) * (1.0 - smoothstep(0.99, 0.995, vLy));
      col += mix(uNeonA, uNeonB, step(512.0, seed)) * crown * 2.2;
    }
    col = mix(col, uFog, smoothstep(30.0, 300.0, vDist));
    gl_FragColor = vec4(col * uMirror, uOpen);
    #include <colorspace_fragment>
  }`;
const STREET_VERT = /* glsl */ `
  varying vec3 vW; varying float vDist;
  void main() {
    vec4 w = modelMatrix * vec4(position, 1.0);
    vW = w.xyz;
    vec4 mv = viewMatrix * w;
    vDist = length(mv.xyz);
    gl_Position = projectionMatrix * mv;
  }`;
const STREET_FRAG = /* glsl */ `
  uniform vec3 uWet; uniform vec3 uGlow; uniform vec3 uFog; uniform float uOpen; uniform float uReveal; uniform float uTime;
  ${NOISE}
  varying vec3 vW; varying float vDist;
  void main() {
    vec3 v = normalize(cameraPosition - vW);
    float fres = pow(1.0 - clamp(v.y, 0.0, 1.0), 2.5);
    vec2 q = vW.xz;
    vec2 cell = floor(q * 0.7), f = fract(q * 0.7) - 0.5;                 // raindrops landing: rings in puddles
    float ph = fract(uTime * 1.1 + hash(vec3(cell, 1.0)));
    float ring = smoothstep(0.05, 0.0, abs(length(f) - ph * 0.45)) * (1.0 - ph) * (1.0 - smoothstep(4.0, 26.0, vDist));
    float puddle = smoothstep(0.3, 0.7, noise(vec3(q * 0.1, 4.0)));
    vec3 col = uWet + uGlow * ring * 0.5 * puddle;
    float a = mix(0.9, 0.3, fres * (0.35 + 0.65 * puddle));
    float fog = smoothstep(50.0, 280.0, vDist);
    col = mix(col, uFog, fog);
    a = max(a, fog);
    float reveal = 1.0 - smoothstep(uReveal - 12.0, uReveal, vDist);
    gl_FragColor = vec4(col, a * uOpen * reveal);
    #include <colorspace_fragment>
  }`;
const PLAIN_VERT = /* glsl */ `
  varying vec2 vUv;
  void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const BILLBOARD_FRAG = /* glsl */ `
  uniform vec3 uA; uniform vec3 uB; uniform float uTime; uniform float uOpen; uniform float uSeed; uniform float uMirror;
  varying vec2 vUv;
  void main() {
    vec2 p = vUv;
    float scan = 0.82 + 0.18 * sin(p.y * 200.0 + uTime * 9.0);
    float band = 0.5 + 0.5 * sin((p.x + p.y * 0.35) * 6.0 - uTime * 1.3 + uSeed);
    vec3 c = mix(uA, uB, band) * 0.4;
    vec2 q = (p - vec2(0.27, 0.5)) * vec2(2.4, 1.0);
    float logo = smoothstep(0.035, 0.0, abs(length(q) - 0.27 - 0.025 * sin(uTime * 3.0 + uSeed)));
    logo += smoothstep(0.03, 0.0, abs(q.x + q.y * 0.6)) * step(length(q), 0.27);
    float bars = step(0.52, p.x) * step(p.x, 0.92) * step(0.45, fract(p.y * 5.0 + uSeed)) * step(abs(p.y - 0.5), 0.32)
      * step(fract(p.x * 3.0 + uSeed) , 0.85 + 0.15 * sin(uTime + p.y * 9.0));
    c += (logo + bars * 0.7) * mix(uB, vec3(1.0), 0.45);
    float frame = 1.0 - step(0.025, p.x) * step(p.x, 0.975) * step(0.05, p.y) * step(p.y, 0.95);
    c += uA * frame * 1.2;
    float flick = 1.0 - 0.6 * step(0.985, fract(sin(floor(uTime * 14.0) * 1.7 + uSeed) * 43758.5));
    gl_FragColor = vec4(c * scan * flick * 1.25 * uMirror, uOpen);
    #include <colorspace_fragment>
  }`;

/** Blade sign glyphs: a column of blocky characters, drawn squat so they look right on a tall, thin sign. */
function signTexture(seed: number) {
  const r = rng(seed);
  return canvasTexture(256, (g, n) => {
    g.fillStyle = '#000';
    g.fillRect(0, 0, n, n);
    g.strokeStyle = '#fff';
    g.shadowColor = '#fff';
    g.shadowBlur = 6;
    g.lineWidth = 10;
    g.strokeRect(8, 4, n - 16, n - 8);
    const rows = 5;
    g.lineWidth = 14;
    for (let i = 0; i < rows; i++) {
      const y0 = 14 + i * ((n - 28) / rows), hgt = (n - 28) / rows - 10;
      for (let k = 0; k < 4; k++) {
        g.beginPath();
        if (r() < 0.5) { const yy = y0 + r() * hgt; g.moveTo(40 + r() * 40, yy); g.lineTo(n - 40 - r() * 40, yy); }
        else { const xx = 44 + r() * (n - 88); g.moveTo(xx, y0 + 2); g.lineTo(xx, y0 + hgt); }
        g.stroke();
      }
    }
  });
}

export function city(scene: THREE.Scene, opts: Opts): Env {
  const root = new THREE.Group();
  scene.add(root);
  const glowTex = glowTexture();
  const signTexs = [signTexture(3), signTexture(7), signTexture(11)];
  const CX = 9;                                           // the avenue runs right of centre (left is behind the portal)

  const skyU = { uTop: { value: new THREE.Color() }, uGlowA: { value: new THREE.Color() }, uGlowB: { value: new THREE.Color() },
    uOpen: { value: 0 }, uTime: { value: 0 }, uSeed: { value: 0 } };
  const sky = new THREE.ShaderMaterial({ vertexShader: SKY_VERT, fragmentShader: CITY_SKY, side: THREE.BackSide, transparent: true, depthWrite: false, uniforms: skyU });
  const skyMesh = new THREE.Mesh(new THREE.SphereGeometry(400, 32, 16), sky);
  skyMesh.renderOrder = -3;
  root.add(skyMesh);

  const world = new THREE.Group(), mirror = new THREE.Group();
  mirror.scale.y = -1;
  root.add(world, mirror);

  // towers: one box, many instances; the mirror is a second instanced mesh with the same matrices
  const towerU = { uBody: { value: new THREE.Color() }, uWarm: { value: new THREE.Color() }, uCool: { value: new THREE.Color() },
    uNeonA: { value: new THREE.Color() }, uNeonB: { value: new THREE.Color() }, uFog: { value: new THREE.Color() },
    uOpen: { value: 0 }, uTime: { value: 0 } };
  const towerMat = (m: number) => new THREE.ShaderMaterial({ vertexShader: TOWER_VERT, fragmentShader: TOWER_FRAG, uniforms: { ...towerU, uMirror: { value: m } } });
  const boxGeo = new THREE.BoxGeometry(1, 1, 1);
  boxGeo.translate(0, 0.5, 0);
  const MAXT = opts.small ? 110 : 190;
  const towers = new THREE.InstancedMesh(boxGeo, towerMat(1), MAXT), towersM = new THREE.InstancedMesh(boxGeo, towerMat(0.4), MAXT);
  towers.frustumCulled = towersM.frustumCulled = false;
  world.add(towers);
  mirror.add(towersM);
  let towerData: { x: number; z: number; w: number; d: number; h: number }[] = [];

  // the wet street
  const streetU = { uWet: { value: new THREE.Color() }, uGlow: { value: new THREE.Color() }, uFog: { value: new THREE.Color() },
    uOpen: { value: 0 }, uReveal: { value: 0 }, uTime: { value: 0 } };
  const street = new THREE.ShaderMaterial({ vertexShader: STREET_VERT, fragmentShader: STREET_FRAG, transparent: true, depthWrite: false, uniforms: streetU });
  const streetGeo = new THREE.PlaneGeometry(700, 700);
  streetGeo.rotateX(-Math.PI / 2);
  streetGeo.translate(0, 0, -300);
  const streetMesh = new THREE.Mesh(streetGeo, street);
  streetMesh.renderOrder = 5;
  root.add(streetMesh);

  // signs and billboards (per game), each with its reflection
  const signs = new THREE.Group(), signsM = new THREE.Group();
  world.add(signs);
  mirror.add(signsM);
  type Sign = { mats: THREE.Material[]; ph: number; flick: number };
  let signList: Sign[] = [];
  const boards: { u: Record<string, THREE.IUniform> }[] = [];
  const clearSigns = () => {
    for (const g of [signs, signsM]) g.traverse((o) => { const m = o as THREE.Mesh; m.geometry?.dispose(); (m.material as THREE.Material | undefined)?.dispose?.(); });
    signs.clear(); signsM.clear();
    signList = []; boards.length = 0;
  };

  // flying traffic: short streaks along the avenue and across it
  const nCars = opts.small ? 50 : 110;
  const carPos = new Float32Array(nCars * 6), carCol = new Float32Array(nCars * 6);
  const carGeo = new THREE.BufferGeometry();
  carGeo.setAttribute('position', new THREE.BufferAttribute(carPos, 3));
  carGeo.setAttribute('color', new THREE.BufferAttribute(carCol, 3));
  const carMat = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const cars = new THREE.LineSegments(carGeo, carMat);
  cars.frustumCulled = false;
  world.add(cars);
  const carHead = new THREE.PointsMaterial({ map: glowTex, size: 1.1, vertexColors: true, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
  const headPos = new Float32Array(nCars * 3), headCol = new Float32Array(nCars * 3);
  const headGeo = new THREE.BufferGeometry();
  headGeo.setAttribute('position', new THREE.BufferAttribute(headPos, 3));
  headGeo.setAttribute('color', new THREE.BufferAttribute(headCol, 3));
  const heads = new THREE.Points(headGeo, carHead);
  heads.frustumCulled = false;
  world.add(heads);
  let carData: { along: boolean; a: number; b: number; y: number; speed: number; off: number; len: number }[] = [];

  // rain
  const nRain = opts.small ? 450 : 1100;
  const rainPos = new Float32Array(nRain * 6), rainSeed = new Float32Array(nRain * 3);
  const r0 = rng(77);
  for (let i = 0; i < nRain; i++) rainSeed.set([(r0() - 0.35) * 70, r0() * 34, -4 - r0() * 60], i * 3);
  const rainGeo = new THREE.BufferGeometry();
  rainGeo.setAttribute('position', new THREE.BufferAttribute(rainPos, 3));
  const rainMat = new THREE.LineBasicMaterial({ transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
  const rain = new THREE.LineSegments(rainGeo, rainMat);
  rain.frustumCulled = false;
  root.add(rain);

  // a hologram over the street: a wireframe shape and a ring in a projector's beam
  const holo = new THREE.Group();
  const holoMat = new THREE.LineBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const holoShape = new THREE.LineSegments(new THREE.WireframeGeometry(new THREE.IcosahedronGeometry(1, 1)), holoMat);
  const holoRing = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.TorusGeometry(1.6, 0.04, 4, 48)), holoMat);
  holoRing.rotation.x = Math.PI / 2;
  const holoGlowMat = new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true });
  const holoGlow = new THREE.Sprite(holoGlowMat);
  holoGlow.scale.setScalar(5);
  const beamMat = new THREE.MeshBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide });
  const beamGeo = new THREE.ConeGeometry(1.4, 1, 24, 1, true);
  beamGeo.translate(0, -0.5, 0);
  const beam = new THREE.Mesh(beamGeo, beamMat);
  holo.add(holoGlow, holoShape, holoRing);
  world.add(holo, beam);

  const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), sc = new THREE.Vector3(), pv = new THREE.Vector3();
  let neon: THREE.Color[] = [];

  return {
    kind: 'city',
    camera: { y: 10, pitch: 0.03, fov: 62 },
    setGame(title) {
      const { h: h1, h2, s } = palette(title);
      const r = rng(s * 2711 + h1 * 29);
      const night = 235 + (h1 % 40) - 20;
      neon = [hsl(h1, 1, 0.56), hsl(h2, 1, 0.56), hsl(h1 + 180, 1, 0.6)];
      skyU.uTop.value.copy(hsl(night, 0.5, 0.012));
      skyU.uGlowA.value.copy(hsl(h1, 0.8, 0.32));
      skyU.uGlowB.value.copy(hsl(h2, 0.8, 0.3));
      skyU.uSeed.value = r() * 20;
      towerU.uBody.value.copy(hsl(night, 0.3, 0.012));
      towerU.uWarm.value.copy(hsl(38, 0.75, 0.6));
      towerU.uCool.value.copy(hsl(195, 0.6, 0.65));
      towerU.uNeonA.value.copy(neon[0]);
      towerU.uNeonB.value.copy(neon[1]);
      const fogC = hsl(h1 + 10, 0.55, 0.06);
      towerU.uFog.value.copy(fogC);
      streetU.uWet.value.copy(hsl(night, 0.4, 0.008));
      streetU.uGlow.value.copy(neon[1]);
      streetU.uFog.value.copy(fogC);
      rainMat.color.copy(hsl(h2, 0.4, 0.75));
      holoMat.color.copy(neon[2]);
      holoGlowMat.color.copy(neon[2]);
      beamMat.color.copy(neon[2]);

      // two rows of towers on each side of the avenue, then a far skyline
      towerData = [];
      for (let z = -10; z > -330 && towerData.length < MAXT - 40; z -= 9 + r() * 9) {
        for (const side of [-1, 1]) {
          const w = 6 + r() * 8, d = 6 + r() * 8;
          const x = CX + side * ((side < 0 ? 15 : 12) + r() * 4 + w / 2);
          towerData.push({ x, z, w, d, h: 16 + r() * 45 + (-z) * 0.14 });
          const w2 = 7 + r() * 10;
          towerData.push({ x: x + side * (w / 2 + 3 + r() * 6 + w2 / 2), z: z - r() * 6, w: w2, d: 7 + r() * 9, h: 30 + r() * 70 + (-z) * 0.18 });
        }
      }
      while (towerData.length < MAXT) {
        towerData.push({ x: (r() - 0.4) * 520, z: -300 - r() * 120, w: 10 + r() * 16, d: 10 + r() * 16, h: 50 + r() * 140 });
      }
      towers.count = towersM.count = towerData.length;

      clearSigns();
      const near = towerData.filter((t) => t.z > -190 && t.z < -14 && Math.abs(t.x - CX) < 40);
      const nSigns = opts.small ? 22 : 40;
      for (let i = 0; i < nSigns && near.length; i++) {
        const t = near[Math.floor(r() * near.length)];
        const side = t.x > CX ? 1 : -1;
        const col = neon[Math.floor(r() * 3)];
        const blade = r() < 0.65;
        const hgt = blade ? 7 + r() * 7 : 5 + r() * 4, wd = blade ? 2 + r() * 0.8 : 9 + r() * 7;
        const y = 6 + r() * Math.max(2, Math.min(t.h * 0.8, 50) - hgt);
        const geo = new THREE.PlaneGeometry(wd, hgt);
        const make = (m: number) => {
          if (blade) {
            return new THREE.MeshBasicMaterial({ map: signTexs[i % 3], color: col.clone().multiplyScalar(1.4 * m), transparent: true, depthWrite: false,
              blending: THREE.AdditiveBlending, side: THREE.DoubleSide });
          }
          const u = { uA: { value: col.clone() }, uB: { value: neon[(i + 1) % 3].clone() }, uTime: { value: 0 }, uOpen: { value: 0 }, uSeed: { value: r() * 9 }, uMirror: { value: m } };
          boards.push({ u });
          return new THREE.ShaderMaterial({ vertexShader: PLAIN_VERT, fragmentShader: BILLBOARD_FRAG, uniforms: u, transparent: true, depthWrite: false,
            blending: THREE.AdditiveBlending, side: THREE.DoubleSide });
        };
        const mats = [make(1), make(0.45)];
        const meshes = mats.map((m) => new THREE.Mesh(geo, m));
        for (const mesh of meshes) {
          if (blade) mesh.position.set(t.x - side * (t.w / 2 - wd / 2 - 0.3), y + hgt / 2, t.z + t.d / 2 + 0.25);   // on the front, facing the viewer
          else if (r() < 0.5) mesh.position.set(t.x + (r() - 0.5) * (t.w - wd) * 0.5, y + hgt / 2, t.z + t.d / 2 + 0.25);
          else {
            mesh.position.set(t.x - side * (t.w / 2 + 0.15), y + hgt / 2, t.z);
            mesh.rotation.y = -side * Math.PI / 2;
          }
        }
        signs.add(meshes[0]);
        signsM.add(meshes[1]);
        signList.push({ mats, ph: r() * 6.28, flick: r() });
      }

      carData = [];
      for (let i = 0; i < nCars; i++) {
        const along = r() < 0.55;
        carData.push({ along, a: along ? CX + (r() - 0.5) * 16 : -20 - r() * 150, b: 0, y: 9 + r() * 32, speed: (r() < 0.5 ? -1 : 1) * (14 + r() * 22),
          off: r() * 400, len: 1.5 + r() * 2.5 });
        const head = r() < 0.5 ? new THREE.Color(1, 0.95, 0.85) : neon[i % 3];
        const tail = carData[i].speed > 0 ? hsl(0, 1, 0.5) : neon[(i + 1) % 3];
        carCol.set([head.r, head.g, head.b, tail.r * 0.6, tail.g * 0.6, tail.b * 0.6], i * 6);
        headCol.set([head.r, head.g, head.b], i * 3);
      }
      (carGeo.attributes.color as THREE.BufferAttribute).needsUpdate = true;
      (headGeo.attributes.color as THREE.BufferAttribute).needsUpdate = true;

      holo.position.set(CX + 2 + r() * 5, 24 + r() * 6, -60 - r() * 20);
      beam.position.copy(holo.position);
      beam.scale.set(1, holo.position.y, 1);
      beam.position.y = holo.position.y;
    },
    update(t, st) {
      const tt = st.still ? 5 : t;
      skyU.uOpen.value = st.open;
      skyU.uTime.value = tt;
      towerU.uOpen.value = st.open;
      towerU.uTime.value = tt;
      for (const m of [towers.material, towersM.material] as THREE.ShaderMaterial[]) m.transparent = st.open < 1;
      streetU.uOpen.value = st.open;
      streetU.uReveal.value = 8 + st.reveal * 380;
      streetU.uTime.value = tt;
      const g = Math.max(st.grow, 0.0001);
      towerData.forEach((d, i) => {
        sc.set(d.w, d.h * (0.2 + 0.8 * g), d.d);
        m4.compose(pv.set(d.x, 0, d.z), q, sc);
        towers.setMatrixAt(i, m4);
        towersM.setMatrixAt(i, m4);
      });
      towers.instanceMatrix.needsUpdate = towersM.instanceMatrix.needsUpdate = true;
      for (const sg of signList) {
        const f = st.still ? 1 : sg.flick > 0.85 ? (Math.sin(t * 23 + sg.ph) > -0.7 || Math.sin(t * 0.9 + sg.ph) > 0 ? 1 : 0.15) : 0.85 + 0.15 * Math.sin(t * 2 + sg.ph);
        sg.mats.forEach((m) => { if (!(m instanceof THREE.ShaderMaterial)) m.opacity = st.open * g * f; });
      }
      for (const b of boards) { b.u.uOpen.value = st.open * g; b.u.uTime.value = tt; }
      carMat.opacity = st.open * 0.9;
      carHead.opacity = st.open;
      for (let i = 0; i < carData.length; i++) {
        const c = carData[i];
        const span = c.along ? 300 : 260;
        const s = ((c.off + tt * c.speed) % span + span) % span;
        const dir = Math.sign(c.speed);
        let hx, hz, tx, tz;
        if (c.along) { hz = -8 - s; hx = c.a; tx = hx; tz = hz + dir * c.len; }
        else { hx = dir > 0 ? -90 + s : 170 - s; hz = c.a; tz = hz; tx = hx - dir * c.len; }
        carPos.set([hx, c.y, hz, tx, c.y, tz], i * 6);
        headPos.set([hx, c.y, hz], i * 3);
      }
      (carGeo.attributes.position as THREE.BufferAttribute).needsUpdate = true;
      (headGeo.attributes.position as THREE.BufferAttribute).needsUpdate = true;
      rainMat.opacity = st.open * 0.35;
      for (let i = 0; i < nRain; i++) {
        const x = rainSeed[i * 3], z = rainSeed[i * 3 + 2];
        const y = ((rainSeed[i * 3 + 1] - tt * 26) % 34 + 34) % 34 - 2;
        rainPos.set([x + y * 0.08, y, z, x + y * 0.08 + 0.12, y + 1.3, z], i * 6);
      }
      (rainGeo.attributes.position as THREE.BufferAttribute).needsUpdate = true;
      holo.scale.setScalar(5.5 * g);
      holoShape.rotation.set(tt * 0.3, tt * 0.5, 0);
      holoRing.rotation.z = tt * 0.8;
      const hf = st.still ? 1 : 0.75 + 0.25 * Math.sin(t * 13) * Math.sin(t * 3.1);
      holoMat.opacity = st.open * hf;
      holoGlowMat.opacity = st.open * 0.5 * hf;
      beamMat.opacity = st.open * 0.07 * g;
    },
    dispose() { clearSigns(); disposeAll(root, [glowTex, ...signTexs]); },
  };
}

// ------------------------------------------------------------------ tron
const ARENA_SKY = /* glsl */ `
  uniform vec3 uTop; uniform vec3 uA; uniform vec3 uB; uniform float uOpen;
  varying vec3 vDir;
  void main() {
    vec3 d = normalize(vDir);
    float y = d.y;
    vec3 c = uTop * (0.35 + 0.65 * smoothstep(-0.1, 0.7, y));
    c += uA * exp(-abs(y - 0.03) * 28.0) * 0.3;
    float az = atan(d.x, d.z);
    c += uB * smoothstep(0.992, 1.0, cos(az * 48.0)) * smoothstep(0.1, 0.45, y) * (1.0 - smoothstep(0.45, 0.85, y)) * 0.12;
    gl_FragColor = vec4(c, uOpen);
    #include <colorspace_fragment>
  }`;
const ARENA_FLOOR = /* glsl */ `
  uniform vec3 uBase; uniform vec3 uA; uniform vec3 uB; uniform vec3 uFog; uniform float uOpen; uniform float uReveal; uniform float uTime;
  varying vec3 vW; varying float vDist;
  ${HASH2}
  void main() {
    vec2 q = vW.xz / 5.0;
    vec2 id = floor(q), f = fract(q);
    float r = hh(id), o = 0.2 + 0.6 * hh(id + 7.0);
    float w = fwidth(q.x) * 1.3 + 0.01;
    float hz = smoothstep(w, 0.0, abs(f.y - o)) * step(r, 0.42) * step(0.1, f.x) * step(f.x, 0.9 + 0.1 * hh(id + 2.0));
    float vt = smoothstep(w, 0.0, abs(f.x - o)) * step(0.58, r) * step(0.1, f.y);
    float pad = smoothstep(0.05, 0.03, length(f - vec2(r < 0.5 ? 0.1 : o, r < 0.5 ? o : 0.1))) * step(abs(r - 0.5), 0.42);
    float trace = max(max(hz, vt), pad);
    float along = r < 0.5 ? q.x : q.y;
    float pulse = smoothstep(0.9, 1.0, fract(along * 0.22 - uTime * 0.5 + r * 3.0));
    float nearF = 1.0 - smoothstep(25.0, 150.0, vDist);
    vec3 col = uBase + mix(uA, uB, step(0.5, hh(id + 4.0))) * trace * (0.16 + pulse * 1.3) * nearF;
    vec2 gq = vW.xz / 20.0;
    vec2 g = abs(fract(gq - 0.5) - 0.5) / max(fwidth(gq), 1e-4);
    col += uA * (1.0 - min(min(g.x, g.y), 1.0)) * 0.08;
    vec3 v = normalize(cameraPosition - vW);
    float fres = pow(1.0 - clamp(v.y, 0.0, 1.0), 3.0);
    float a = mix(0.95, 0.62, fres);
    float fog = smoothstep(70.0, 260.0, vDist);
    col = mix(col, uFog, fog);
    a = max(a, fog);
    float reveal = 1.0 - smoothstep(uReveal - 12.0, uReveal, vDist);
    gl_FragColor = vec4(col, a * uOpen * reveal);
    #include <colorspace_fragment>
  }`;
const TRAIL_VERT = /* glsl */ `
  attribute float aFade; attribute float aY;
  varying float vF; varying float vY;
  void main() { vF = aFade; vY = aY; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const TRAIL_FRAG = /* glsl */ `
  uniform vec3 uCol; uniform float uOpen; uniform float uMirror;
  varying float vF; varying float vY;
  void main() {
    float a = pow(vF, 1.4) * smoothstep(0.0, 0.03, 1.0 - vF + 0.03);
    float glow = 0.16 + smoothstep(0.72, 1.0, vY) * 1.2 + smoothstep(0.25, 0.0, vY) * 0.45;
    vec3 c = uCol * glow + vec3(1.0) * smoothstep(0.92, 1.0, vY) * 0.8;
    gl_FragColor = vec4(c * uMirror, a * uOpen);
    #include <colorspace_fragment>
  }`;
const WALL_FRAG = /* glsl */ `
  uniform vec3 uA; uniform vec3 uB; uniform float uOpen; uniform float uTime; uniform float uMirror;
  varying vec2 vUv;
  ${HASH2}
  void main() {
    float x = vUv.x * 120.0, y = vUv.y;
    vec3 c = vec3(0.006, 0.008, 0.016);
    float run = smoothstep(0.8, 1.0, fract(vUv.x * 10.0 - uTime * 0.12));
    float bands = smoothstep(0.014, 0.0, abs(y - 0.16)) * (0.7 + run * 1.6) + smoothstep(0.008, 0.0, abs(y - 0.62)) * 0.6
      + smoothstep(0.025, 0.0, abs(y - 0.985)) * 0.9;
    float pillars = smoothstep(0.06, 0.0, abs(fract(x / 4.0) - 0.5)) * step(0.16, y) * step(y, 0.98) * 0.3;
    vec2 seat = vec2(floor(x * 2.0), floor(y * 46.0));
    float crowd = step(0.2, y) * step(y, 0.58) * step(0.72, hh(seat)) * smoothstep(0.35, 0.0, abs(fract(y * 46.0) - 0.5))
      * (0.6 + 0.4 * sin(uTime * 2.0 + hh(seat + 1.0) * 30.0));
    c += uA * bands + uB * pillars + mix(uA, uB, hh(seat + 5.0)) * crowd * 0.45;
    gl_FragColor = vec4(c * uMirror, uOpen);
    #include <colorspace_fragment>
  }`;

/** A light cycle's route: straight runs with right-angle turns inside a box, extended as it goes. */
class Route {
  pts: THREE.Vector2[] = [];
  cum: number[] = [];
  private dir = new THREE.Vector2(1, 0);
  constructor(private r: () => number, start: THREE.Vector2, private box: { x0: number; x1: number; z0: number; z1: number }) {
    this.pts.push(start.clone());
    this.cum.push(0);
    this.dir.set(this.r() < 0.5 ? 1 : -1, 0);
    if (this.r() < 0.5) this.dir.set(0, this.dir.x);
  }
  private inside(p: THREE.Vector2) { return p.x > this.box.x0 && p.x < this.box.x1 && p.y > this.box.z0 && p.y < this.box.z1; }
  ensure(d: number) {
    while (this.cum[this.cum.length - 1] < d) {
      const last = this.pts[this.pts.length - 1];
      const turn = this.r() < 0.5 ? 1 : -1;
      const opts = [new THREE.Vector2(-this.dir.y * turn, this.dir.x * turn), new THREE.Vector2(this.dir.y * turn, -this.dir.x * turn), this.dir.clone()];
      let len = 14 + this.r() * 26, next: THREE.Vector2 | null = null;
      for (const o of opts) {
        for (const l of [len, len * 0.5, 5]) {
          const p = last.clone().addScaledVector(o, l);
          if (this.inside(p)) { next = p; this.dir.copy(o); len = l; break; }
        }
        if (next) break;
      }
      if (!next) { this.dir.negate(); next = last.clone().addScaledVector(this.dir, 6); len = 6; }
      this.pts.push(next);
      this.cum.push(this.cum[this.cum.length - 1] + len);
    }
  }
  trim(d: number) {
    while (this.cum.length > 3 && this.cum[1] < d) { this.pts.shift(); this.cum.shift(); }
  }
  at(d: number, out: THREE.Vector2, dirOut?: THREE.Vector2) {
    let i = this.cum.length - 2;
    while (i > 0 && this.cum[i] > d) i--;
    const a = this.pts[i], b = this.pts[i + 1];
    const seg = this.cum[i + 1] - this.cum[i];
    const f = seg > 0 ? THREE.MathUtils.clamp((d - this.cum[i]) / seg, 0, 1) : 0;
    out.copy(a).lerp(b, f);
    if (dirOut) dirOut.copy(b).sub(a).normalize();
    return out;
  }
}

export function tron(scene: THREE.Scene, opts: Opts): Env {
  const root = new THREE.Group();
  scene.add(root);
  const glowTex = glowTexture();
  const ARENA = new THREE.Vector3(12, 0, -80);

  const skyU = { uTop: { value: new THREE.Color() }, uA: { value: new THREE.Color() }, uB: { value: new THREE.Color() }, uOpen: { value: 0 } };
  const sky = new THREE.ShaderMaterial({ vertexShader: SKY_VERT, fragmentShader: ARENA_SKY, side: THREE.BackSide, transparent: true, depthWrite: false, uniforms: skyU });
  const skyMesh = new THREE.Mesh(new THREE.SphereGeometry(400, 32, 16), sky);
  skyMesh.renderOrder = -3;
  root.add(skyMesh);

  const world = new THREE.Group(), mirror = new THREE.Group();
  mirror.scale.y = -1;
  root.add(world, mirror);

  const floorU = { uBase: { value: new THREE.Color() }, uA: { value: new THREE.Color() }, uB: { value: new THREE.Color() }, uFog: { value: new THREE.Color() },
    uOpen: { value: 0 }, uReveal: { value: 0 }, uTime: { value: 0 } };
  const floor = new THREE.ShaderMaterial({ vertexShader: STREET_VERT, fragmentShader: ARENA_FLOOR, transparent: true, depthWrite: false, uniforms: floorU });
  const floorGeo = new THREE.PlaneGeometry(700, 700);
  floorGeo.rotateX(-Math.PI / 2);
  floorGeo.translate(0, 0, -250);
  const floorMesh = new THREE.Mesh(floorGeo, floor);
  floorMesh.renderOrder = 5;
  root.add(floorMesh);

  // the arena: its wall with stands, and the halo rings above
  const wallU = { uA: { value: new THREE.Color() }, uB: { value: new THREE.Color() }, uOpen: { value: 0 }, uTime: { value: 0 } };
  const wallGeo = new THREE.CylinderGeometry(170, 170, 34, 160, 1, true);
  wallGeo.translate(0, 17, 0);
  for (const [grp, m] of [[world, 1], [mirror, 0.35]] as const) {
    const wall = new THREE.Mesh(wallGeo, new THREE.ShaderMaterial({ vertexShader: PLAIN_VERT, fragmentShader: WALL_FRAG, uniforms: { ...wallU, uMirror: { value: m } },
      side: THREE.BackSide, transparent: true }));
    wall.position.copy(ARENA);
    wall.renderOrder = -2;
    grp.add(wall);
  }
  const ringMat = new THREE.MeshBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const ringMat2 = new THREE.MeshBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const rings = new THREE.Group();
  const ringA = new THREE.Mesh(new THREE.TorusGeometry(120, 0.7, 6, 220), ringMat);
  const ringB = new THREE.Mesh(new THREE.TorusGeometry(104, 0.35, 6, 200), ringMat2);
  ringA.rotation.x = ringB.rotation.x = Math.PI / 2;
  rings.add(ringA, ringB);
  rings.position.set(ARENA.x, 62, ARENA.z);
  world.add(rings);

  // light cycles and their walls
  const N = opts.small ? 4 : 6, SAMPLES = 80, H = 1.6;
  type Cycle = { route: Route; speed: number; len: number; d0: number; geo: THREE.BufferGeometry; u: Record<string, THREE.IUniform>;
    body: THREE.Group; bodyM: THREE.Group; glow: THREE.Sprite; edgeMat: THREE.LineBasicMaterial };
  let cycles: Cycle[] = [];
  const trailIndex: number[] = [];
  for (let i = 0; i < SAMPLES - 1; i++) { const a = i * 2; trailIndex.push(a, a + 1, a + 2, a + 1, a + 3, a + 2); }
  const bodyGeo = new THREE.BoxGeometry(2.4, 0.75, 0.55);
  bodyGeo.translate(0, 0.45, 0);
  const bodyEdges = new THREE.EdgesGeometry(bodyGeo);
  const bodyMat = new THREE.MeshStandardMaterial({ color: 0x07080c, roughness: 0.3, metalness: 0.8, transparent: true });
  const cycleGroup = new THREE.Group(), cycleGroupM = new THREE.Group();
  world.add(cycleGroup);
  mirror.add(cycleGroupM);
  const clearCycles = () => {
    for (const c of cycles) { c.geo.dispose(); c.edgeMat.dispose(); (c.glow.material as THREE.Material).dispose(); }
    cycleGroup.traverse((o) => { const m = o as THREE.Mesh; if (m.material instanceof THREE.ShaderMaterial) m.material.dispose(); });
    cycleGroupM.traverse((o) => { const m = o as THREE.Mesh; if (m.material instanceof THREE.ShaderMaterial) m.material.dispose(); });
    cycleGroup.clear(); cycleGroupM.clear();
    cycles = [];
  };

  // data streams falling by the far wall
  const nData = opts.small ? 160 : 380;
  const dataPos = new Float32Array(nData * 6), dataSeed = new Float32Array(nData * 4);
  const dataGeo = new THREE.BufferGeometry();
  dataGeo.setAttribute('position', new THREE.BufferAttribute(dataPos, 3));
  const dataMat = new THREE.LineBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const data = new THREE.LineSegments(dataGeo, dataMat);
  data.frustumCulled = false;
  world.add(data);

  // the recognizer: a beam on two splayed legs, lit edges, a searchlight underneath
  const reco = new THREE.Group();
  const recoMat = new THREE.MeshStandardMaterial({ color: 0x0a0b10, roughness: 0.35, metalness: 0.85, transparent: true });
  const recoLine = new THREE.LineBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const part = (geo: THREE.BufferGeometry, x: number, y: number, z: number, rz = 0) => {
    const g = new THREE.Group();
    g.add(new THREE.Mesh(geo, recoMat), new THREE.LineSegments(new THREE.EdgesGeometry(geo), recoLine));
    g.position.set(x, y, z);
    g.rotation.z = rz;
    reco.add(g);
  };
  part(new THREE.BoxGeometry(17, 2.2, 5), 0, 0, 0);
  part(new THREE.BoxGeometry(5, 3.2, 6), 0, 0.4, 0.6);
  for (const s of [-1, 1]) {
    part(new THREE.BoxGeometry(2.6, 11, 4.4), s * 7.2, -6, 0, s * 0.14);
    part(new THREE.BoxGeometry(3.6, 1.4, 5.2), s * 8.1, -11.6, 0);
  }
  const scanMat = new THREE.MeshBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide });
  const scanGeo = new THREE.ConeGeometry(6, 1, 32, 1, true);
  scanGeo.translate(0, -0.5, 0);
  const scan = new THREE.Mesh(scanGeo, scanMat);
  world.add(reco, scan);

  const hemi = new THREE.HemisphereLight(0xffffff, 0x000000, 0.6);
  const key = new THREE.DirectionalLight(0xffffff, 1.2);
  key.position.set(-30, 60, 40);
  root.add(hemi, key, key.target);

  const v2 = new THREE.Vector2(), d2 = new THREE.Vector2();
  let recoBase = new THREE.Vector3();

  return {
    kind: 'tron',
    camera: { y: 6.5, pitch: -0.1, fov: 64 },
    setGame(title) {
      const { h: h1, h2, s } = palette(title);
      const r = rng(s * 1597 + h1 * 31);
      const cA = hsl(h1, 1, 0.55), cB = hsl(h2, 1, 0.55), cC = hsl(h1 + 180, 1, 0.58);
      skyU.uTop.value.copy(hsl(225, 0.6, 0.008));
      skyU.uA.value.copy(cA);
      skyU.uB.value.copy(cB);
      floorU.uBase.value.copy(hsl(225, 0.5, 0.004));
      floorU.uA.value.copy(cA);
      floorU.uB.value.copy(cB);
      floorU.uFog.value.copy(hsl(225, 0.6, 0.01));
      wallU.uA.value.copy(cA);
      wallU.uB.value.copy(cB);
      ringMat.color.copy(cA);
      ringMat2.color.copy(cB);
      dataMat.color.copy(cB);
      recoLine.color.copy(cC);
      scanMat.color.copy(cC);
      hemi.color.copy(cA);

      clearCycles();
      const box = { x0: -10, x1: 42, z0: -78, z1: -12 };
      const colors = [cA, cB, cC, hsl(h2 + 60, 1, 0.6), hsl(h1 + 90, 1, 0.6)];
      for (let i = 0; i < N; i++) {
        const route = new Route(r, new THREE.Vector2(box.x0 + 6 + r() * 48, box.z0 + 6 + r() * 50), box);
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(SAMPLES * 6), 3));
        const fade = new Float32Array(SAMPLES * 2), ys = new Float32Array(SAMPLES * 2);
        for (let k = 0; k < SAMPLES; k++) { fade.set([k / (SAMPLES - 1), k / (SAMPLES - 1)], k * 2); ys.set([0, 1], k * 2); }
        geo.setAttribute('aFade', new THREE.BufferAttribute(fade, 1));
        geo.setAttribute('aY', new THREE.BufferAttribute(ys, 1));
        geo.setIndex(trailIndex);
        const col = colors[i % colors.length];
        const u = { uCol: { value: col.clone() }, uOpen: { value: 0 } };
        const mk = (m: number) => new THREE.ShaderMaterial({ vertexShader: TRAIL_VERT, fragmentShader: TRAIL_FRAG, uniforms: { ...u, uMirror: { value: m } },
          transparent: true, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending });
        const wall = new THREE.Mesh(geo, mk(1)), wallM = new THREE.Mesh(geo, mk(0.16));
        wall.frustumCulled = wallM.frustumCulled = false;
        const edgeMat = new THREE.LineBasicMaterial({ color: col, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
        const body = new THREE.Group(), bodyM = new THREE.Group();
        for (const b of [body, bodyM]) b.add(new THREE.Mesh(bodyGeo, bodyMat), new THREE.LineSegments(bodyEdges, edgeMat));
        const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTex, color: col, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true }));
        glow.scale.setScalar(4);
        body.add(glow);
        cycleGroup.add(wall, body);
        cycleGroupM.add(wallM, bodyM);
        cycles.push({ route, speed: 13 + r() * 7, len: 40 + r() * 25, d0: 90 + r() * 40, geo, u, body, bodyM, glow, edgeMat });
      }

      for (let i = 0; i < nData; i++) {
        const a = -0.9 + r() * 1.8;                                   // columns along the far part of the wall
        dataSeed.set([ARENA.x + Math.sin(a) * 150, ARENA.z - Math.cos(a) * 150, r() * 60, 0.6 + r() * 2.2], i * 4);
      }
      recoBase = new THREE.Vector3(13 + r() * 8, 18 + r() * 4, -66 - r() * 10);
    },
    update(t, st) {
      const tt = st.still ? 6 : t;
      skyU.uOpen.value = st.open;
      floorU.uOpen.value = st.open;
      floorU.uReveal.value = 8 + st.reveal * 380;
      floorU.uTime.value = tt;
      wallU.uOpen.value = st.open;
      wallU.uTime.value = tt;
      const g = Math.max(st.grow, 0.0001);
      ringMat.opacity = st.open * 0.85;
      ringMat2.opacity = st.open * 0.5;
      rings.scale.setScalar(0.6 + 0.4 * g);
      rings.rotation.y = tt * 0.03;
      ringB.position.y = Math.sin(tt * 0.4) * 2;
      bodyMat.opacity = st.open;

      for (const c of cycles) {
        const d = c.d0 + tt * c.speed;
        c.route.ensure(d + 1);
        c.route.trim(d - c.len - 2);
        c.u.uOpen.value = st.open;
        const p = c.geo.attributes.position as THREE.BufferAttribute;
        const hgt = H * (0.3 + 0.7 * g);
        for (let k = 0; k < SAMPLES; k++) {
          const s = Math.max(c.route.cum[0], d - c.len * (1 - k / (SAMPLES - 1)));
          c.route.at(s, v2);
          p.setXYZ(k * 2, v2.x, 0, v2.y);
          p.setXYZ(k * 2 + 1, v2.x, hgt, v2.y);
        }
        p.needsUpdate = true;
        c.geo.computeBoundingSphere();
        c.route.at(d, v2, d2);
        for (const b of [c.body, c.bodyM]) {
          b.position.set(v2.x, 0, v2.y);
          b.rotation.y = Math.atan2(-d2.y, d2.x);
          b.scale.setScalar(g);
        }
        c.edgeMat.opacity = st.open;
        (c.glow.material as THREE.SpriteMaterial).opacity = st.open * 0.8;
      }

      dataMat.opacity = st.open * 0.7;
      for (let i = 0; i < nData; i++) {
        const [x, z, y0, len] = [dataSeed[i * 4], dataSeed[i * 4 + 1], dataSeed[i * 4 + 2], dataSeed[i * 4 + 3]];
        const y = ((y0 - tt * (6 + len * 3)) % 60 + 60) % 60;
        dataPos.set([x, y, z, x, y + len, z], i * 6);
      }
      (dataGeo.attributes.position as THREE.BufferAttribute).needsUpdate = true;

      reco.position.set(recoBase.x + Math.sin(tt * 0.15) * 6, recoBase.y + Math.sin(tt * 0.6) * 0.8, recoBase.z + Math.cos(tt * 0.12) * 5);
      reco.rotation.y = -0.4 + Math.sin(tt * 0.1) * 0.3;
      reco.scale.setScalar(1.3 * g);
      recoMat.opacity = st.open;
      recoLine.opacity = st.open;
      scan.position.set(reco.position.x, reco.position.y - 12 * g, reco.position.z);
      scan.scale.set(1 + Math.sin(tt * 0.7) * 0.3, Math.max(0.01, scan.position.y), 1);
      scan.rotation.z = Math.sin(tt * 0.5) * 0.25;
      scanMat.opacity = st.open * 0.06 * g;
    },
    dispose() { clearCycles(); disposeAll(root, [glowTex]); },
  };
}
