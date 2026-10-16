"""Data-driven FEX game compatibility. No entitlement/runtime-signature changes; stdlib only on the Frame."""
import hashlib
import os
import re
import shlex
import struct

STATUSES = {"verified", "likely", "untested", "blocked"}
GRAPHICS = {"d3d11", "d3d9", "d3d12", "opengl", "vulkan"}


def exe_facts(path):
    """Read just the PE headers. A 32-bit client still needs GE's WoW64 and a working 32-bit VR/graphics bridge."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if head[:2] != b"MZ" or len(head) < 64:
                return {}
            f.seek(struct.unpack_from("<I", head, 0x3c)[0])
            pe = f.read(26)
        if len(pe) < 26 or pe[:4] != b"PE\0\0":
            return {}
        machine = struct.unpack_from("<H", pe, 4)[0]
        return {"exe_32bit": machine == 0x14c, "exe_machine": hex(machine)}
    except (OSError, struct.error):
        return {}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def match_profile(games, app, path):
    """Exact canonical name OR SHA-256 OR (exe basename AND byte size). No fuzzy title matches: different store
    builds can need different routes. Multiple matches are refused rather than silently picking one."""
    hits, digest = [], None
    for key, profile in games.items():
        match = profile.get("match", {"canonical_names": [key]})
        hit = app in match.get("canonical_names", [])
        if not hit and path and os.path.isfile(path):
            hit = any(os.path.basename(path).lower() == e["name"].lower() and os.path.getsize(path) == e["size"]
                      for e in match.get("executables", []))
            if not hit and match.get("sha256"):
                digest = digest or sha256(path)
                hit = digest in match["sha256"]
        if hit:
            hits.append((key, profile))
    if len(hits) > 1:
        raise ValueError("ambiguous FEX compatibility profiles: " + ", ".join(k for k, _ in hits))
    return hits[0] if hits else (None, None)


def graphics_lines(route):
    if route not in GRAPHICS:
        raise ValueError(f"unknown FEX graphics route: {route}")
    # Proton owns DLL selection/copying: DXVK for D3D9/11, vkd3d-proton for D3D12. Do not override DLLs or copy
    # anything into the game. Clearing inherited flags also keeps a prior game's wined3d choice from leaking in.
    lines = ["unset PROTON_USE_WINED3D PROTON_NO_D3D12 PROTON_NO_D3D11"]
    if route == "opengl":
        lines.append("export PROTON_USE_WINED3D=1")
    lines.append(f"echo 'FramePort FEX: graphics={route}'")
    return lines


def register(core):
    original_find = core.find_ime_target

    def find_ime_target(bins):
        # Before ApplicationCore was split out, FWindowsApplication (including its deferral CVar) lived in Core.
        # Robo Recall's modular UE4 build has the SAME CVar and constructor instruction there. Search that module
        # with the existing anchored finder; never infer a member offset from bAllowedToDeferMessageProcessing.
        extra = []
        for folder in sorted({os.path.dirname(b) for b in bins}):
            extra += [os.path.join(folder, n) for n in sorted(os.listdir(folder))
                      if re.search(r"(?:^|-)core(?:-win(?:32|64)-shipping)?\.dll$", n, re.I)]
        return original_find(list(dict.fromkeys(list(bins) + extra)))

    def game_info(app, info):
        path = os.path.join(info.get("game_dir") or os.path.join(core.META_DIR, "Software", "Software", app),
                            info.get("exe", ""))
        key, profile = match_profile(core.GAMES, app, path)
        if profile is None:
            return
        status = profile.get("status", "verified")     # backward compatible with First Contact's original data
        if status not in STATUSES:
            raise ValueError(f"invalid FEX profile status: {status}")
        info.update({k: v for k, v in profile.items() if k != "match"})
        info.update(profile=key, status=status)

    def launcher_env(app, info):
        lines = graphics_lines(info.get("graphics", "d3d11"))
        if info.get("fp_mem") == "enabled":
            if info.get("status") != "verified":
                raise ValueError("fp_mem requires a verified per-game opt-in")
            lines.append("export FP_MEM=1")
            lines.append("export FP_BIGCACHE_MB=" + str(int(info.get("bigcache_mb", 256))))
        # candidate is informational: never enable the unzeroed cache merely because the engine uses Binned2.
        for k, v in info.get("env", {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", k) or not isinstance(v, str):
                raise ValueError("invalid FEX profile environment")
        # with the perf extension loaded, perf_prepare writes the game's one dxvk.conf (profile lines + its settings)
        if info.get("dxvk") and "perf_prepare" not in core.COMMANDS:
            lines += ["printf '%s\\n' " + " ".join(shlex.quote(line) for line in info["dxvk"]) + ' > "$A/dxvk.conf"',
                      'export DXVK_CONFIG_FILE="$A/dxvk.conf"']
        return lines

    def checks(app, info, result):
        facts = exe_facts(result["exe"])
        facts.update(profile=info.get("profile"), profile_status=info.get("status", "untested"),
                     graphics=info.get("graphics", "d3d11"), fp_mem=info.get("fp_mem", "off"))
        # frameport_agent forwards checks to the PC and deployment.json; top-level helper warnings aren't forwarded.
        if result.get("warnings"):
            facts["profile_warnings"] = result["warnings"]
        if facts.get("exe_32bit"):
            facts["wow64"] = "untested: requires GE WoW64, 32-bit graphics and SteamVR vrclient bridge under FEX"
        if facts["graphics"] == "opengl":
            facts["opengl"] = "untested: no FEX GL thunk configured; rootfs GL may use software rendering or Zink"
        if facts["graphics"] == "d3d12":
            # GE versions have used both split lib/lib64 and architecture-qualified Wine directories. Report what
            # is actually installed; the Proton script remains responsible for selecting its own layout.
            paths = [os.path.join(core.GE, "files", lib, "wine", "vkd3d-proton", tail, "d3d12.dll")
                     for lib in ("lib", "lib64") for tail in ("", "x86_64-windows", "i386-windows")]
            facts["vkd3d_dlls"] = [os.path.relpath(p, core.GE) for p in paths if os.path.isfile(p)]
        return facts

    core.GAME_INFO_HOOKS.append(game_info)
    core.find_ime_target = find_ime_target
    core.LAUNCHER_ENV_HOOKS.append(launcher_env)
    core.INSTALL_CHECK_HOOKS.append(checks)
