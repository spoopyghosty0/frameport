// What the VR worlds (envs.ts, envs-more.ts) share: colours, textures, freeing, sky and noise shaders, geometry helpers.
import * as THREE from 'three';

export const hsl = (h: number, s: number, l: number) => new THREE.Color().setHSL(((h % 360) + 360) % 360 / 360, s, l);

export function canvasTexture(size: number, draw: (g: CanvasRenderingContext2D, n: number) => void) {
  const c = document.createElement('canvas');
  c.width = c.height = size;
  draw(c.getContext('2d')!, size);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

export const glowTexture = () => canvasTexture(128, (g, n) => {
  const r = g.createRadialGradient(n / 2, n / 2, 0, n / 2, n / 2, n / 2);
  r.addColorStop(0, 'rgba(255,255,255,1)');
  r.addColorStop(0.22, 'rgba(255,255,255,0.85)');
  r.addColorStop(0.5, 'rgba(255,255,255,0.18)');
  r.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = r;
  g.fillRect(0, 0, n, n);
});

/** Everything under `root`, and the extra textures, freed. */
export function disposeAll(root: THREE.Object3D, textures: THREE.Texture[]) {
  root.traverse((o) => {
    const m = o as THREE.Mesh;
    m.geometry?.dispose();
    const mat = m.material as THREE.Material | THREE.Material[] | undefined;
    if (Array.isArray(mat)) mat.forEach((x) => x.dispose()); else mat?.dispose?.();
  });
  root.parent?.remove(root);
  textures.forEach((t) => t.dispose());
}

export const SKY_VERT = /* glsl */ `
  varying vec3 vDir;
  void main() {
    vDir = normalize(position);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }`;

export const NOISE = /* glsl */ `
  float hash(vec3 p) { p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
  float noise(vec3 x) {
    vec3 i = floor(x), f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(mix(hash(i), hash(i + vec3(1,0,0)), f.x), mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
               mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x), mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z);
  }
  float fbm(vec3 p) { float a = 0.5, s = 0.0; for (int i = 0; i < 5; i++) { s += a * noise(p); p *= 2.03; a *= 0.5; } return s; }`;

export function mergeGeometries(parts: THREE.BufferGeometry[]) {
  const pos: number[] = [];
  for (const g of parts) {
    const p = (g.index ? g.toNonIndexed() : g).attributes.position as THREE.BufferAttribute;
    for (let i = 0; i < p.count; i++) pos.push(p.getX(i), p.getY(i), p.getZ(i));
    g.dispose();
  }
  const out = new THREE.BufferGeometry();
  out.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  return out;
}

/** Moves each corner the same way on every face that shares it. three's polyhedra are non-indexed (each face has its
 * own copy of a corner), so moving the copies one by one tears the surface open into loose triangles. */
export function moveCorners(geo: THREE.BufferGeometry, move: (x: number, y: number, z: number) => [number, number, number]) {
  const p = geo.attributes.position as THREE.BufferAttribute;
  const key = (v: number) => Math.round(v * 1000) + 0;   // + 0: -0 and 0 are the same corner
  const moved = new Map<string, [number, number, number]>();
  for (let i = 0; i < p.count; i++) {
    const x = p.getX(i), y = p.getY(i), z = p.getZ(i);
    const k = `${key(x)},${key(y)},${key(z)}`;
    let v = moved.get(k);
    if (!v) { v = move(x, y, z); moved.set(k, v); }
    p.setXYZ(i, v[0], v[1], v[2]);
  }
  p.needsUpdate = true;
  return geo;
}
