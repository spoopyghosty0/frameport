// What each install stage says for one catalog game: the page builds the first game with it and "Try another game"
// swaps in another with the same function, so both read the same way.

export interface LogGame {
  title: string;
  package: string;
  engine: string;
  xr: string;
  verified?: string;
  patches: string[];
  version: string;
  app: string;
  note: string;
}

/** A recipe id in a few plain words: "frame.unity_text_input" → "Unity text input", "adapter.focus_hold" → "setting: focus hold". */
export function patchLabel(id: string): string {
  const [kind, name = id] = id.includes('.') ? [id.slice(0, id.indexOf('.')), id.slice(id.indexOf('.') + 1)] : ['', id];
  let words = name.replace(/^patch_/, '').replace(/_/g, ' ').replace(/\bunity\b/i, 'Unity').replace(/\bvk\b/i, 'Vulkan')
    .replace(/\bxr\b/i, 'XR').replace(/\bvrapi\b/i, 'VrApi').replace(/\bovr\b/i, 'OVR').replace(/\bgl\b/i, 'GL')
    .replace(/\bobb\b/i, 'OBB').replace(/\bmsaa\b/i, 'MSAA').replace(/\bsdl\b/i, 'SDL').replace(/\bue\b/i, 'Unreal');
  if (kind === 'adapter') words = `setting: ${words}`;
  else if (kind === 'overport') words = `OVRPort: ${words}`;
  else if (kind === 'device') words = `device: ${words}`;
  return words;
}

export function stageDetails(g: LogGame): Record<string, string> {
  const n = g.patches.length;
  const shown = g.patches.slice(0, 2).map(patchLabel).join(', ');
  return {
    scan: `${g.package || g.title} · APK`,
    analyze: `${g.engine || 'engine?'} · ${g.xr || 'VR?'} · arm64`,
    recipe: g.verified ? `tested ${g.verified} · ${n} ${n === 1 ? 'patch' : 'patches'}` : `${n} ${n === 1 ? 'patch' : 'patches'}`,
    convert: g.xr === 'VrApi' ? 'OVRPort · VrApi → OpenXR' : g.xr === 'OpenXR' ? 'OVRPort · Meta loader → Frame' : `OVRPort · ${g.xr}`,
    patch: n ? `${shown}${n > 2 ? ` +${n - 2}` : ''}` : 'default patches',
    sign: g.package ? `own key · ${g.package.split('.').slice(-1)[0]}` : "the game's own key",
    check: g.version ? `v${g.version} · arm64-v8a` : 'arm64-v8a',
    upload: 'SSH · resumable',
    library: `"${g.title}" + artwork`,
    test: g.app ? `passed · FramePort ${g.app}` : 'launch test',
  };
}
