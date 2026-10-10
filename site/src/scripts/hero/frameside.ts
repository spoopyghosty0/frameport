// Drives the hero's Frame side: loads three.js only once the hero is on screen, renders only while it is visible,
// keeps the scene in step with the APK card's CSS lap (it reads that animation's progress every frame, so a click
// that restarts the lap restarts the scene too), and turns pointer drags, hover and phone tilt into a look around.
import { hasWebGL, type FrameSide } from './types';

export type Mode = 'world' | 'particles';
const KEY = 'frameport.hero.frameside';  // TEMPORARY: remembers the comparison toggle's choice
const STILL_LAP = 0.75;                  // reduced motion: the moment the game has fully arrived

export interface Controller { setGame(title: string): void; setMode(mode: Mode): void; mode(): Mode; dragged(): boolean }

export function savedMode(): Mode {
  try { return localStorage.getItem(KEY) === 'particles' ? 'particles' : 'world'; } catch { return 'world'; }
}

export function initFrameSide(stage: HTMLElement, host: HTMLElement, firstTitle: string): Controller {
  const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const lapAnim = () => stage.querySelector('.lane-pc .track')?.getAnimations()[0];
  const lap = () => {
    if (still) return STILL_LAP;
    const p = lapAnim()?.effect?.getComputedTiming().progress;
    return typeof p === 'number' ? p : STILL_LAP;
  };

  let mode: Mode = savedMode();
  let title = firstTitle;
  let side: FrameSide | null = null;
  let canvas: HTMLCanvasElement | null = null;
  let visible = false, running = false, loading = 0;
  // where the viewer looks: drag (kept), hover (springs back), tilt (phones); eased toward the target
  let dragYaw = 0, dragPitch = 0, hoverYaw = 0, hoverPitch = 0, tiltYaw = 0, tiltPitch = 0, yaw = 0, pitch = 0;

  const size = () => {
    if (!side || !canvas) return;
    const r = host.getBoundingClientRect();
    side.resize(r.width, r.height);
  };
  const drawOnce = () => { if (side) { yaw = dragYaw + hoverYaw + tiltYaw; pitch = dragPitch + hoverPitch + tiltPitch; side.look(yaw, pitch); side.frame(performance.now() / 1000, lap()); } };

  const loop = () => {
    if (!side || !visible || document.hidden || still) { running = false; return; }
    running = true;
    const ty = dragYaw + hoverYaw + tiltYaw, tp = dragPitch + hoverPitch + tiltPitch;
    yaw += (ty - yaw) * 0.08;
    pitch += (tp - pitch) * 0.08;
    side.look(yaw, pitch);
    side.frame(performance.now() / 1000, lap());
    requestAnimationFrame(loop);
  };
  const kick = () => { if (still) drawOnce(); else if (!running && visible && side) requestAnimationFrame(loop); };

  async function start(m: Mode) {
    const ticket = ++loading;
    side?.dispose();
    side = null;
    canvas?.remove();
    host.classList.remove('live');
    try {
      const mod = m === 'world' ? await import('./world') : await import('./particles');
      if (ticket !== loading) return;                   // switched again while loading
      canvas = document.createElement('canvas');
      canvas.className = 'fs-canvas';
      host.append(canvas);
      side = mod.create(canvas, { still });
      side.setGame(title);
      size();
      host.classList.add('live');
      host.dataset.mode = m;
      kick();
    } catch {
      // no WebGL (or the module failed): the CSS scene underneath stays
      canvas?.remove();
      canvas = null;
      side = null;
    }
  }

  const webgl = hasWebGL();
  new IntersectionObserver((es) => {
    visible = es.some((e) => e.isIntersecting);
    if (visible && !side && !loading && webgl) start(mode);
    kick();
  }, { rootMargin: '120px' }).observe(stage);
  document.addEventListener('visibilitychange', kick);
  new ResizeObserver(() => { size(); if (still) drawOnce(); }).observe(host);

  // look around: drag on the Frame side (a drag is not a click: the stage's "next game" ignores it), hover tilts a
  // little, phones follow the device's tilt
  let down: { x: number; y: number; yaw: number; pitch: number } | null = null;
  let moved = 0, lastDrag = 0;
  const box = () => host.getBoundingClientRect();
  host.addEventListener('pointerdown', (e) => {
    down = { x: e.clientX, y: e.clientY, yaw: dragYaw, pitch: dragPitch };
    moved = 0;
    host.setPointerCapture(e.pointerId);
    askTilt();
  });
  host.addEventListener('pointermove', (e) => {
    const r = box();
    const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
    side?.pointer(x, y, true);
    if (down) {
      moved = Math.max(moved, Math.hypot(e.clientX - down.x, e.clientY - down.y));
      dragYaw = Math.max(-1.1, Math.min(1.1, down.yaw - ((e.clientX - down.x) / r.width) * 1.8));
      dragPitch = Math.max(-0.4, Math.min(0.4, down.pitch - ((e.clientY - down.y) / r.height) * 0.9));
    } else if (e.pointerType === 'mouse') {
      hoverYaw = -(x - 0.6) * 0.3;
      hoverPitch = -(y - 0.5) * 0.14;
    }
    if (still) drawOnce();
  });
  const up = (e: PointerEvent) => {
    if (down && moved > 6) lastDrag = performance.now();
    down = null;
    if (host.hasPointerCapture(e.pointerId)) host.releasePointerCapture(e.pointerId);
  };
  host.addEventListener('pointerup', up);
  host.addEventListener('pointercancel', up);
  host.addEventListener('pointerleave', () => { hoverYaw = 0; hoverPitch = 0; side?.pointer(0, 0, false); if (still) drawOnce(); });

  let tiltAsked = false, base: { b: number; g: number } | null = null;
  const onTilt = (e: DeviceOrientationEvent) => {
    if (e.beta === null || e.gamma === null) return;
    base ??= { b: e.beta, g: e.gamma };
    tiltYaw = -Math.max(-1, Math.min(1, (e.gamma - base.g) / 40)) * 0.6;
    tiltPitch = Math.max(-1, Math.min(1, (e.beta - base.b) / 40)) * 0.25;
    if (still) drawOnce();
  };
  function askTilt() {
    if (tiltAsked || !matchMedia('(pointer: coarse)').matches || typeof DeviceOrientationEvent === 'undefined') return;
    tiltAsked = true;
    const req = (DeviceOrientationEvent as unknown as { requestPermission?: () => Promise<string> }).requestPermission;
    if (typeof req === 'function') {
      req().then((s) => { if (s === 'granted') addEventListener('deviceorientation', onTilt); }).catch(() => {});
    } else {
      addEventListener('deviceorientation', onTilt);
    }
  }
  // Android and others need no permission: follow the tilt from the start
  if (matchMedia('(pointer: coarse)').matches && typeof DeviceOrientationEvent !== 'undefined'
    && typeof (DeviceOrientationEvent as unknown as { requestPermission?: unknown }).requestPermission !== 'function') {
    tiltAsked = true;
    addEventListener('deviceorientation', onTilt);
  }

  return {
    setGame(t) { title = t; side?.setGame(t); if (still) drawOnce(); },
    setMode(m) {
      if (m === mode) return;
      mode = m;
      try { localStorage.setItem(KEY, m); } catch { /* private mode */ }
      if (!webgl) return;
      if (visible) start(m); else { side?.dispose(); side = null; canvas?.remove(); canvas = null; loading = 0; }
    },
    mode: () => mode,
    dragged: () => performance.now() - lastDrag < 400,
  };
}
