// Frame side, variant "world": the game arrives as an abstract VR world. A neon grid terrain flies toward the
// viewer between two mountain ridges, under a sky in the game's colours, with a glowing sun and a few floating
// shapes. It opens from the portal as the card crosses (the grid lights up outward from the seam), stays alive, and
// closes again before the next game.
import * as THREE from 'three';
import { palette, rng, smooth, type FrameSide } from './types';

const SKY_VERT = /* glsl */ `
  varying vec3 vDir;
  void main() {
    vDir = normalize(position);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }`;
const SKY_FRAG = /* glsl */ `
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

function glowTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d')!;
  const r = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  r.addColorStop(0, 'rgba(255,255,255,1)');
  r.addColorStop(0.22, 'rgba(255,255,255,0.85)');
  r.addColorStop(0.5, 'rgba(255,255,255,0.18)');
  r.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = r;
  g.fillRect(0, 0, 128, 128);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

const hsl = (h: number, s: number, l: number) => new THREE.Color().setHSL(((h % 360) + 360) % 360 / 360, s, l);

export function create(canvas: HTMLCanvasElement, opts: { still: boolean }): FrameSide {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'low-power' });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(62, 1, 0.1, 500);
  camera.rotation.order = 'YXZ';

  const sky = new THREE.ShaderMaterial({
    vertexShader: SKY_VERT, fragmentShader: SKY_FRAG, side: THREE.BackSide, transparent: true, depthWrite: false,
    uniforms: { uTop: { value: new THREE.Color() }, uHorizon: { value: new THREE.Color() }, uBottom: { value: new THREE.Color() },
      uOpen: { value: 0 } },
  });
  const skyMesh = new THREE.Mesh(new THREE.SphereGeometry(300, 32, 16), sky);
  skyMesh.renderOrder = -1;
  scene.add(skyMesh);

  const land = new THREE.ShaderMaterial({
    vertexShader: LAND_VERT, fragmentShader: LAND_FRAG, transparent: true,
    uniforms: { uScroll: { value: 0 }, uShape: { value: new THREE.Vector4(8, 1, 4, 0) }, uLineA: { value: new THREE.Color() },
      uLineB: { value: new THREE.Color() }, uFill: { value: new THREE.Color() }, uOpen: { value: 0 }, uReveal: { value: 0 },
      uCell: { value: 2.5 } },
  });
  const ground = new THREE.PlaneGeometry(220, 220, 180, 180);
  ground.rotateX(-Math.PI / 2);
  ground.translate(0, 0, -80);
  scene.add(new THREE.Mesh(ground, land));

  const glowTex = glowTexture();
  const sunMat = new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true });
  const sun = new THREE.Sprite(sunMat);
  sun.position.set(0, 14, -170);
  scene.add(sun);
  const ringMat = new THREE.MeshBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const ring = new THREE.Mesh(new THREE.TorusGeometry(26, 0.35, 8, 120), ringMat);
  ring.position.copy(sun.position);
  scene.add(ring);

  // stars over the upper sky
  const starGeo = new THREE.BufferGeometry();
  const starPos = new Float32Array(500 * 3);
  const r0 = rng(7);
  for (let i = 0; i < 500; i++) {
    const a = r0() * Math.PI * 2, y = 0.08 + r0() * 0.9, rad = Math.sqrt(1 - y * y);
    starPos.set([Math.cos(a) * rad * 280, y * 280, Math.sin(a) * rad * 280], i * 3);
  }
  starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  const starMat = new THREE.PointsMaterial({ size: 1.6, sizeAttenuation: false, transparent: true, depthWrite: false, color: 0xffffff });
  scene.add(new THREE.Points(starGeo, starMat));

  // the floating shapes: the game's own set, rebuilt per game
  const shapes = new THREE.Group();
  scene.add(shapes);
  type Floater = { obj: THREE.Object3D; base: THREE.Vector3; spin: THREE.Vector3; bob: number; size: number };
  let floaters: Floater[] = [];
  const disposeShapes = () => {
    shapes.traverse((o) => {
      const m = o as THREE.Mesh;
      m.geometry?.dispose();
      (m.material as THREE.Material | undefined)?.dispose?.();
    });
    shapes.clear();
    floaters = [];
  };

  let yaw = 0, pitch = 0;
  let w = 1, h = 1;

  function setGame(title: string) {
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

    disposeShapes();
    const kinds = [
      () => new THREE.IcosahedronGeometry(1, 0), () => new THREE.OctahedronGeometry(1, 0), () => new THREE.TetrahedronGeometry(1.1, 0),
      () => new THREE.TorusGeometry(0.85, 0.28, 10, 28), () => new THREE.TorusKnotGeometry(0.7, 0.2, 80, 10, 2, 3),
      () => new THREE.DodecahedronGeometry(1, 0),
    ];
    const n = 5 + Math.floor(r() * 3);
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
  }

  function frame(t: number, lap: number) {
    // open as the card crosses (30–56 % of the lap), close before the next game arrives
    const open = smooth(0.34, 0.56, lap) * (1 - smooth(0.9, 0.985, lap));
    const reveal = smooth(0.36, 0.7, lap);
    const still = opts.still;
    sky.uniforms.uOpen.value = open;
    land.uniforms.uOpen.value = open;
    land.uniforms.uReveal.value = 6 + reveal * 260;
    land.uniforms.uScroll.value = still ? 40 : t * 7.5;
    starMat.opacity = open * 0.75;
    sunMat.opacity = open;
    ringMat.opacity = open * 0.55;
    sun.scale.setScalar(70 + (still ? 0 : Math.sin(t * 0.7) * 3));
    for (const f of floaters) {
      const grow = smooth(0.45, 0.66, lap) * (1 - smooth(0.88, 0.97, lap));
      f.obj.scale.setScalar(f.size * Math.max(grow, 0.0001));
      if (!still) {
        f.obj.rotation.x = f.spin.x * t;
        f.obj.rotation.y = f.spin.y * t + 0.4;
        f.obj.position.y = f.base.y + Math.sin(t * 0.8 + f.bob) * 0.8;
      } else {
        f.obj.rotation.set(0.5, 0.7, 0.2);
      }
    }
    // step through the portal: the view starts behind it and moves in while the world opens
    const enter = 1 - smooth(0.34, 0.62, lap);
    camera.position.set(0, 2.6 + enter * 1.5, enter * 9);
    camera.fov = 62 + enter * 16;
    camera.updateProjectionMatrix();
    const drift = still ? 0 : Math.sin(t * 0.13) * 0.06;
    camera.rotation.y = yaw + drift + 0.06;
    camera.rotation.x = -0.07 + pitch + (still ? 0 : Math.sin(t * 0.17) * 0.015);
    renderer.render(scene, camera);
  }

  function resize(width: number, height: number) {
    w = Math.max(1, width); h = Math.max(1, height);
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }

  return {
    setGame,
    frame,
    resize,
    look(y, p) { yaw = y; pitch = p; },
    pointer() { /* the world only looks around */ },
    dispose() {
      disposeShapes();
      scene.traverse((o) => {
        const m = o as THREE.Mesh;
        m.geometry?.dispose();
        (m.material as THREE.Material | undefined)?.dispose?.();
      });
      glowTex.dispose();
      renderer.dispose();
    },
  };
}
