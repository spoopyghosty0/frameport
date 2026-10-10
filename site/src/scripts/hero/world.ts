// The hero's Frame side: the game arrives as a VR world (envs.ts: a neon grid, islands at dusk or a ringed planet
// in space, picked from the title). It opens from the portal as the card crosses, stays alive, and closes again
// before the next game; the next game opens into a different world than the one before.
import * as THREE from 'three';
import { buildEnv, envKindFor, ENV_KINDS, type Env, type EnvKind } from './envs';
import { smooth, type FrameSide } from './types';

export function create(canvas: HTMLCanvasElement, opts: { still: boolean }): FrameSide {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'low-power' });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(62, 1, 0.1, 800);
  camera.rotation.order = 'YXZ';
  const small = matchMedia('(max-width: 760px), (pointer: coarse)').matches;

  let env: Env | null = null;
  let yaw = 0, pitch = 0;

  function setGame(title: string) {
    // the title's world, but never the same world twice in a row
    let kind: EnvKind = envKindFor(title);
    if (env && kind === env.kind) kind = ENV_KINDS[(ENV_KINDS.indexOf(kind) + 1 + (title.length % 2)) % ENV_KINDS.length];
    if (!env || env.kind !== kind) {
      env?.dispose();
      env = buildEnv(kind, scene, { small });
      canvas.dataset.env = kind;
    }
    env.setGame(title);
  }

  function frame(t: number, lap: number) {
    if (!env) return;
    // open as the card crosses (30–56 % of the lap), close before the next game arrives
    const open = smooth(0.34, 0.56, lap) * (1 - smooth(0.9, 0.985, lap));
    const reveal = smooth(0.36, 0.7, lap);
    const grow = smooth(0.45, 0.66, lap) * (1 - smooth(0.88, 0.97, lap));
    const still = opts.still;
    env.update(still ? 0 : t, { open, reveal, grow, still });
    // step through the portal: the view starts behind it and moves in while the world opens
    const enter = 1 - smooth(0.34, 0.62, lap);
    const c = env.camera;
    camera.position.set(0, c.y + enter * 1.5, enter * 9);
    camera.fov = c.fov + enter * 16;
    camera.updateProjectionMatrix();
    const drift = still ? 0 : Math.sin(t * 0.13) * 0.06;
    camera.rotation.y = yaw + drift + 0.06;
    camera.rotation.x = c.pitch + pitch + (still ? 0 : Math.sin(t * 0.17) * 0.015);
    renderer.render(scene, camera);
  }

  function resize(width: number, height: number) {
    const w = Math.max(1, width), h = Math.max(1, height);
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
      env?.dispose();
      env = null;
      renderer.dispose();
    },
  };
}
