// A self-running VR world in any box (the app demo's live view): the same worlds as the hero's Frame side
// (hero/envs.ts), always open, no lap. Loads three.js on first use, renders only while on screen and the tab is
// shown, draws one frame under reduced motion, and can show it as the headset sees it (two eyes, lens edges).
import { hasWebGL } from './hero/types';
import type { EnvKind } from './hero/envs';

export interface VrScene { dispose(): void; setEnv(kind: EnvKind): void }
export interface VrSceneOptions { title: string; env?: EnvKind; interactive?: boolean; stereo?: boolean }

const IPD = 0.32;   // eye distance in world units (the worlds are large: a little exaggerated reads better)

export async function mountVrScene(container: HTMLElement, opts: VrSceneOptions): Promise<VrScene | null> {
  if (!hasWebGL()) return null;
  let THREE: typeof import('three'), envs: typeof import('./hero/envs');
  try {
    [THREE, envs] = await Promise.all([import('three'), import('./hero/envs')]);
  } catch {
    return null;
  }
  if (!container.isConnected) return null;

  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const small = matchMedia('(max-width: 760px), (pointer: coarse)').matches;
  if (getComputedStyle(container).position === 'static') container.style.position = 'relative';

  const canvas = document.createElement('canvas');
  canvas.className = 'vr-canvas';
  canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:pan-y;';
  container.append(canvas);
  let renderer: import('three').WebGLRenderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'low-power' });
  } catch {
    canvas.remove();
    return null;
  }
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(62, 1, 0.1, 800);
  camera.rotation.order = 'YXZ';
  const eye = camera.clone();

  // the lens edge of each eye: a round vignette over each half
  const lenses: HTMLElement[] = [];
  if (opts.stereo) {
    for (const side of ['left', 'right']) {
      const l = document.createElement('div');
      l.className = `vr-lens vr-lens-${side}`;
      l.style.cssText = `position:absolute;top:0;bottom:0;${side}:0;width:50%;pointer-events:none;`
        + 'background:radial-gradient(ellipse 52% 60% at 50% 50%, transparent 62%, rgba(0,0,0,.55) 82%, #000 100%);';
      container.append(l);
      lenses.push(l);
    }
    const gap = document.createElement('div');
    gap.className = 'vr-lens';
    gap.style.cssText = 'position:absolute;top:0;bottom:0;left:50%;width:2px;margin-left:-1px;background:#000;pointer-events:none;';
    container.append(gap);
    lenses.push(gap);
  }

  let env = envs.buildEnv(opts.env ?? envs.envKindFor(opts.title), scene, { small });
  env.setGame(opts.title);

  let w = 1, h = 1;
  const size = () => {
    const r = container.getBoundingClientRect();
    w = Math.max(1, r.width); h = Math.max(1, r.height);
    renderer.setSize(w, h, false);
  };

  // look around: drag (kept), hover (a little, springs back)
  let dragYaw = 0, dragPitch = 0, hoverYaw = 0, hoverPitch = 0, yaw = 0, pitch = 0;
  const t0 = performance.now();
  const draw = (ease: number) => {
    const t = still ? 0 : (performance.now() - t0) / 1000;
    yaw += (dragYaw + hoverYaw - yaw) * ease;
    pitch += (dragPitch + hoverPitch - pitch) * ease;
    env.update(t, { open: 1, reveal: 1, grow: 1, still });
    const c = env.camera;
    camera.position.set(0, c.y, 0);
    camera.fov = c.fov;
    camera.rotation.y = yaw + (still ? 0 : Math.sin(t * 0.13) * 0.08) + 0.06;
    camera.rotation.x = c.pitch + pitch + (still ? 0 : Math.sin(t * 0.17) * 0.015);
    camera.updateMatrixWorld();
    if (!opts.stereo) {
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setScissorTest(false);
      renderer.setViewport(0, 0, w, h);
      renderer.render(scene, camera);
      return;
    }
    // two eyes side by side, each offset along the head's right vector
    const half = w / 2;
    const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion);
    renderer.setScissorTest(true);
    renderer.clear();
    for (const [i, s] of [[0, -1], [1, 1]] as const) {
      eye.copy(camera);
      eye.position.addScaledVector(right, (s * IPD) / 2);
      eye.aspect = half / h;
      eye.updateProjectionMatrix();
      eye.updateMatrixWorld();
      renderer.setViewport(i * half, 0, half, h);
      renderer.setScissor(i * half, 0, half, h);
      renderer.render(scene, eye);
    }
    renderer.setScissorTest(false);
  };

  let visible = false, running = false, disposed = false;
  const loop = () => {
    if (disposed || !visible || document.hidden || still) { running = false; return; }
    running = true;
    draw(0.08);
    requestAnimationFrame(loop);
  };
  const kick = () => {
    if (disposed) return;
    if (still) draw(1);
    else if (!running && visible && !document.hidden) requestAnimationFrame(loop);
  };

  const io = new IntersectionObserver((es) => { visible = es.some((e) => e.isIntersecting); kick(); }, { rootMargin: '80px' });
  io.observe(container);
  const ro = new ResizeObserver(() => { size(); if (still) draw(1); });
  ro.observe(container);
  document.addEventListener('visibilitychange', kick);
  size();

  const listeners: [string, EventListener][] = [];
  if (opts.interactive) {
    canvas.style.cursor = 'grab';
    let down: { x: number; y: number; yaw: number; pitch: number } | null = null;
    const on = (type: string, fn: (e: PointerEvent) => void) => {
      canvas.addEventListener(type, fn as EventListener);
      listeners.push([type, fn as EventListener]);
    };
    on('pointerdown', (e) => {
      down = { x: e.clientX, y: e.clientY, yaw: dragYaw, pitch: dragPitch };
      canvas.setPointerCapture(e.pointerId);
      canvas.style.cursor = 'grabbing';
    });
    on('pointermove', (e) => {
      const r = canvas.getBoundingClientRect();
      if (down) {
        dragYaw = Math.max(-1.2, Math.min(1.2, down.yaw - ((e.clientX - down.x) / r.width) * 1.8));
        dragPitch = Math.max(-0.45, Math.min(0.45, down.pitch - ((e.clientY - down.y) / r.height) * 0.9));
      } else if (e.pointerType === 'mouse') {
        hoverYaw = -((e.clientX - r.left) / r.width - 0.5) * 0.3;
        hoverPitch = -((e.clientY - r.top) / r.height - 0.5) * 0.14;
      }
      if (still) draw(1);
    });
    const up = (e: PointerEvent) => {
      down = null;
      canvas.style.cursor = 'grab';
      if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
    };
    on('pointerup', up);
    on('pointercancel', up);
    on('pointerleave', () => { hoverYaw = 0; hoverPitch = 0; if (still) draw(1); });
  }

  kick();
  return {
    setEnv(kind) {
      if (disposed || kind === env.kind) return;
      env.dispose();
      env = envs.buildEnv(kind, scene, { small });
      env.setGame(opts.title);
      if (still) draw(1);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      io.disconnect();
      ro.disconnect();
      document.removeEventListener('visibilitychange', kick);
      for (const [type, fn] of listeners) canvas.removeEventListener(type, fn);
      env.dispose();
      renderer.dispose();
      canvas.remove();
      lenses.forEach((l) => l.remove());
    },
  };
}
