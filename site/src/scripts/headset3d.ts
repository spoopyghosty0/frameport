// The Steam Frame as a 3D model you can turn: the logo's visor outline (nose notch included) extruded into a body,
// dark with a rim light in the portal's colours (blue on the left, orange on the right), two glowing orange lenses
// on the front and a strap behind. Drag to turn it (it keeps spinning a little after you let go), it tilts toward
// the pointer, follows the phone's tilt, and turns slowly on its own when left alone. three.js loads only when a
// model is on screen; without WebGL the flat logo drawing stays.
type Three = typeof import('three');

const VISOR = 'M35.5 21h17.5a8 8 0 0 1 8 8v5a8 8 0 0 1-8 8H48.65a3 3 0 0 1-2.6-1.5l-1-1.7a.9.9 0 0 0-1.6 0l-1 1.7a3 3 0 0 1-2.6 1.5H35.5a8 8 0 0 1-8-8v-5a8 8 0 0 1 8-8z';
const CX = 44.25, CY = 31.5, UNIT = 17;                 // the visor's centre and half width in the logo's units

const BODY_VERT = /* glsl */ `
  varying vec3 vN; varying vec3 vObj; varying vec3 vView;
  void main() {
    vObj = position;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vView = -mv.xyz;
    vN = normalize(normalMatrix * normal);
    gl_Position = projectionMatrix * mv;
  }`;
const BODY_FRAG = /* glsl */ `
  uniform vec3 uBlue; uniform vec3 uOrange; uniform vec3 uBase;
  varying vec3 vN; varying vec3 vObj; varying vec3 vView;
  void main() {
    vec3 n = normalize(vN), v = normalize(vView);
    vec3 l = normalize(vec3(0.35, 0.7, 0.6));
    float diff = max(dot(n, l), 0.0);
    float spec = pow(max(dot(reflect(-l, n), v), 0.0), 40.0);
    float fres = pow(1.0 - max(dot(n, v), 0.0), 2.6);
    vec3 rim = mix(uBlue, uOrange, smoothstep(-0.8, 0.8, vObj.x));
    vec3 c = uBase * (0.55 + diff * 1.3) + rim * fres * 1.25 + vec3(spec) * 0.35;
    gl_FragColor = vec4(c, 1.0);
    #include <colorspace_fragment>
  }`;
const LENS_FRAG = /* glsl */ `
  uniform vec3 uColor; uniform float uPulse;
  varying vec2 vUv;
  void main() {
    float r = length(vUv - 0.5) * 2.0;
    vec3 c = mix(vec3(1.0, 0.93, 0.8), uColor, smoothstep(0.0, 0.55, r)) * (1.1 + uPulse * 0.25);
    c *= 1.0 - smoothstep(0.85, 1.0, r) * 0.6;
    gl_FragColor = vec4(c, 1.0);
    #include <colorspace_fragment>
  }`;
const UV_VERT = /* glsl */ `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;

function glow(THREE: Three, color: string) {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d')!;
  const r = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  r.addColorStop(0, color);
  r.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = r;
  g.fillRect(0, 0, 128, 128);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function hasWebGL(): boolean {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch { return false; }
}

async function mount(host: HTMLElement) {
  if (!hasWebGL()) return;                               // the drawing stays (and three.js never loads)
  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const THREE = await import('three');
  const { SVGLoader } = await import('three/examples/jsm/loaders/SVGLoader.js');
  const canvas = document.createElement('canvas');
  canvas.className = 'hm-canvas';
  let renderer: import('three').WebGLRenderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'low-power' });
  } catch {
    return;                                              // no WebGL: the drawing stays
  }
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  host.append(canvas);
  host.classList.add('live');

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 50);
  camera.position.set(0, 0.35, 5.2);
  camera.lookAt(0, 0, 0);

  const model = new THREE.Group();
  scene.add(model);

  // the body: the visor outline, extruded and bevelled
  const svg = new SVGLoader().parse(`<svg xmlns="http://www.w3.org/2000/svg"><path d="${VISOR}"/></svg>`);
  // (createShapes is marked deprecated in the types, but path.toShapes() gives this outline the wrong winding)
  const shapes = svg.paths.flatMap((p) => SVGLoader.createShapes(p));
  const body = new THREE.ExtrudeGeometry(shapes, { depth: 10, bevelEnabled: true, bevelThickness: 2, bevelSize: 1.4,
    bevelSegments: 8, curveSegments: 40 });
  body.translate(-CX, -CY, -5);
  body.rotateX(Math.PI);                                // SVG y points down: turn it over (a mirror would turn the faces inside out)
  body.scale(1 / UNIT, 1 / UNIT, 1 / UNIT);
  const bodyMat = new THREE.ShaderMaterial({ vertexShader: BODY_VERT, fragmentShader: BODY_FRAG,
    uniforms: { uBlue: { value: new THREE.Color('#3AA8FF') }, uOrange: { value: new THREE.Color('#FF8A1F') },
      uBase: { value: new THREE.Color('#14161c') } } });
  model.add(new THREE.Mesh(body, bodyMat));
  const front = (5 + 2) / UNIT;                          // half depth + bevel

  // the lenses, where the logo draws them
  const lensMat = new THREE.ShaderMaterial({ vertexShader: UV_VERT, fragmentShader: LENS_FRAG,
    uniforms: { uColor: { value: new THREE.Color('#FF8A1F') }, uPulse: { value: 0 } } });
  const halo = glow(THREE, 'rgba(255,138,31,0.85)');
  for (const x of [39.5, 49]) {
    const lens = new THREE.Mesh(new THREE.CircleGeometry(1, 48), lensMat);
    lens.scale.set(4.2 / UNIT, 3.8 / UNIT, 1);
    lens.position.set((x - CX) / UNIT, (CY - 30.5) / UNIT, front + 0.004);
    const h = new THREE.Sprite(new THREE.SpriteMaterial({ map: halo, transparent: true, blending: THREE.AdditiveBlending,
      depthWrite: false, opacity: 0.6 }));
    h.scale.setScalar(0.75);
    h.position.copy(lens.position).add(new THREE.Vector3(0, 0, 0.02));
    model.add(lens, h);
  }

  // the strap: half a ring from one temple round the back to the other
  const strap = new THREE.Mesh(new THREE.TorusGeometry(1, 0.06, 12, 64, Math.PI),
    new THREE.ShaderMaterial({ vertexShader: BODY_VERT, fragmentShader: BODY_FRAG,
      uniforms: { uBlue: { value: new THREE.Color('#3AA8FF') }, uOrange: { value: new THREE.Color('#FF8A1F') },
        uBase: { value: new THREE.Color('#0f1116') } } }));
  strap.rotation.x = -Math.PI / 2;
  strap.scale.set(1.08, 1.5, 1);
  model.add(strap);
  // the logo's strap stubs at the temples
  const stubGeo = new THREE.CylinderGeometry(0.06, 0.06, 0.16, 16);
  for (const s of [-1, 1]) {
    const stub = new THREE.Mesh(stubGeo, strap.material);
    stub.rotation.z = Math.PI / 2;
    stub.position.set(s * 1.06, 0, 0);
    model.add(stub);
  }

  // turning: drag (with inertia), hover tilt, phone tilt, slow idle spin
  let yaw = still ? -0.62 : -0.5, pitch = 0.16, vel = 0, hoverY = 0, hoverP = 0, tiltY = 0, tiltP = 0;
  let dragging = false, lastX = 0, lastY = 0, lastTouch = -1e9;
  host.addEventListener('pointerdown', (e) => {
    dragging = true; lastX = e.clientX; lastY = e.clientY; vel = 0; lastTouch = performance.now();
    host.setPointerCapture(e.pointerId);
    host.classList.add('turned');
    askTilt();
  });
  host.addEventListener('pointermove', (e) => {
    const r = host.getBoundingClientRect();
    if (dragging) {
      const dx = e.clientX - lastX, dy = e.clientY - lastY;
      vel = dx * 0.009;
      yaw += vel;
      pitch = Math.max(-0.6, Math.min(0.7, pitch + dy * 0.006));
      lastX = e.clientX; lastY = e.clientY; lastTouch = performance.now();
    } else if (e.pointerType === 'mouse') {
      hoverY = ((e.clientX - r.left) / r.width - 0.5) * 0.35;
      hoverP = ((e.clientY - r.top) / r.height - 0.5) * 0.25;
    }
    if (still) draw();
  });
  const release = (e: PointerEvent) => { dragging = false; if (host.hasPointerCapture(e.pointerId)) host.releasePointerCapture(e.pointerId); };
  host.addEventListener('pointerup', release);
  host.addEventListener('pointercancel', release);
  host.addEventListener('pointerleave', () => { hoverY = 0; hoverP = 0; if (still) draw(); });

  let tiltAsked = false, base: { b: number; g: number } | null = null;
  const onTilt = (e: DeviceOrientationEvent) => {
    if (e.beta === null || e.gamma === null) return;
    base ??= { b: e.beta, g: e.gamma };
    tiltY = Math.max(-1, Math.min(1, (e.gamma - base.g) / 35)) * 0.7;
    tiltP = Math.max(-1, Math.min(1, (e.beta - base.b) / 35)) * 0.35;
  };
  const DOE = typeof DeviceOrientationEvent === 'undefined' ? null
    : (DeviceOrientationEvent as unknown as { requestPermission?: () => Promise<string> });
  function askTilt() {
    if (tiltAsked || !DOE || !matchMedia('(pointer: coarse)').matches) return;
    tiltAsked = true;
    if (typeof DOE.requestPermission === 'function') {
      DOE.requestPermission().then((s) => { if (s === 'granted') addEventListener('deviceorientation', onTilt); }).catch(() => {});
    } else addEventListener('deviceorientation', onTilt);
  }
  if (DOE && typeof DOE.requestPermission !== 'function' && matchMedia('(pointer: coarse)').matches) {
    tiltAsked = true;
    addEventListener('deviceorientation', onTilt);
  }

  let ey = yaw, ep = pitch, prev = performance.now();
  function draw() {
    const now = performance.now();
    const dt = Math.min(0.05, (now - prev) / 1000);
    prev = now;
    if (!still && !dragging) {
      yaw += vel;
      vel *= Math.pow(0.04, dt);                          // inertia: most of the spin is gone after a second
      if (Math.abs(vel) < 0.0005 && now - lastTouch > 1800) yaw += dt * 0.35;
    }
    const ty = yaw + hoverY + tiltY, tp = pitch + hoverP + tiltP;
    ey += (ty - ey) * (still ? 1 : 0.18);
    ep += (tp - ep) * (still ? 1 : 0.12);
    model.rotation.set(ep, ey, 0);
    lensMat.uniforms.uPulse.value = still ? 0 : Math.sin(now / 900) * 0.5 + 0.5;
    renderer.render(scene, camera);
  }

  const size = () => {
    const r = host.getBoundingClientRect();
    renderer.setSize(Math.max(1, r.width), Math.max(1, r.height), false);
    camera.aspect = r.width / Math.max(1, r.height);
    // keep the whole headset in view whatever the shape of the box
    camera.position.z = camera.aspect < 1.1 ? 5.2 * (1.1 / camera.aspect) : 5.2;
    camera.updateProjectionMatrix();
    draw();
  };
  new ResizeObserver(size).observe(host);
  size();

  let visible = true, running = false;
  const loop = () => {
    if (!visible || document.hidden || still) { running = false; return; }
    running = true;
    draw();
    requestAnimationFrame(loop);
  };
  const kick = () => { if (!running && visible && !still) requestAnimationFrame(loop); };
  new IntersectionObserver((es) => { visible = es.some((x) => x.isIntersecting); kick(); }).observe(host);
  document.addEventListener('visibilitychange', kick);
  kick();
}

/** Every `[data-headset3d]` on the page gets its model once it comes near the screen. */
export function initHeadsets() {
  const io = new IntersectionObserver((es) => {
    for (const e of es) {
      if (!e.isIntersecting) continue;
      io.unobserve(e.target);
      mount(e.target as HTMLElement).catch(() => { /* the drawing stays */ });
    }
  }, { rootMargin: '200px' });
  document.querySelectorAll<HTMLElement>('[data-headset3d]:not([data-mounted])').forEach((el) => {
    el.dataset.mounted = '';
    io.observe(el);
  });
}
