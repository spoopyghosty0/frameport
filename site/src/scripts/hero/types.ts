// What the hero's Frame side draws: the game, arrived through the portal (world.ts, with the worlds from envs.ts);
// frameside.ts drives it from the CSS lap of the APK card.
import { hue, seed } from '../cover';

export interface FrameSide {
  /** The game now crossing (colours and shapes come from its title). */
  setGame(title: string): void;
  /** One frame: t in seconds, lap = 0..1 progress of the card's 9 s lap. */
  frame(t: number, lap: number): void;
  resize(width: number, height: number): void;
  /** Where the viewer looks (radians, already eased). */
  look(yaw: number, pitch: number): void;
  /** The pointer over the canvas: x, y in 0..1 (canvas box), active false when it left. */
  pointer(x: number, y: number, active: boolean): void;
  dispose(): void;
}

/** The game's colours, as the generated covers pick them (cover.coverVars): hue, second hue, a seed for shapes. */
export function palette(title: string) {
  const h = hue(title);
  const s = seed(title);
  return { h, h2: (h + 40 + (s % 80)) % 360, s };
}

/** The canvas sits right of the tilted seam: at the top the seam is this far into the canvas, at the bottom at 0. */
export const SEAM_TOP = 0.3918;

/** A seeded random number generator (mulberry32), so a game always gets the same world. */
export function rng(seedValue: number) {
  let a = seedValue >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** WebGL is there (checked without three.js, which logs an error when it can't get a context). */
export function hasWebGL(): boolean {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch { return false; }
}

export const smooth =(a: number, b: number, x: number) => {
  const t = Math.min(1, Math.max(0, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
};
