"""Unity: disable Meta environment depth occlusion (EnvironmentDepthManager).

Steam Frame lacks Meta Quest's physical depth sensor (XR_META_environment_depth), leaving
environment depth textures uninitialized. In shaders using Meta's Depth API (such as Laser Dance's
FX/Laser), the invalid depth causes the shader to treat virtual objects near the player as occluded
by the physical environment, making objects (e.g. laser beams) disappear when the player approaches
them. Rewriting EnvironmentDepthManager methods to return false / None in libil2cpp.so prevents depth
occlusion keywords (HARD_OCCLUSION / SOFT_OCCLUSION) from ever being enabled, keeping virtual objects
visible at all distances.
"""
from __future__ import annotations

import struct

try:
    import lz4.block
except ImportError:
    lz4 = None

from ..base import ApkContext, Suggestion, register
from .unity_text_input import RET_FALSE, Il2cppReturnPatch

RET_ZERO = RET_FALSE  # mov w0, #0 ; ret


def _patch_shader_proximity_fade(data: bytes) -> tuple[bytes | None, int]:
    """Scan asset data for LZ4-compressed shader blobs that contain distance proximity fade
    factors (such as (dist - 2.0) * 0.5 in Laser Dance FX/Laser Beam Hard Edges)
    and neutralise the proximity fade factor so objects/lasers stay visible at all distances.
    """
    if not lz4:
        return None, 0

    c_2_5 = struct.pack('<f', -2.5)  # b'\x00\x00\x20\xc0'
    c_2_0 = struct.pack('<f', -2.0)  # b'\x00\x00\x00\xc0'
    c_10_0 = struct.pack('<f', 10.0) # b'\x00\x00\x20\x41'

    ba = bytearray(data)
    total_patched = 0
    p = 0
    data_len = len(ba)

    while p < data_len - 8:
        decomp_sz, comp_sz = struct.unpack_from('<II', ba, p)
        if (1000 < decomp_sz < 10000000 and 100 < comp_sz < 5000000
                and decomp_sz > comp_sz and p + 8 + comp_sz <= data_len):
            try:
                raw = lz4.block.decompress(ba[p+8:p+8+comp_sz], uncompressed_size=decomp_sz)
                if c_2_5 in raw and c_2_0 in raw:
                    raw_ba = bytearray(raw)
                    count = struct.unpack('<I', raw_ba[:4])[0]
                    if 1 <= count <= 500:
                        entries = [struct.unpack('<III', raw_ba[4+i*12:16+i*12]) for i in range(count)]
                        patched_subs = 0
                        for off, length, _seg in entries:
                            sub = raw_ba[off:off+length]
                            if sub.count(c_2_5) == 1 and sub.count(c_2_0) == 1:
                                p20 = sub.find(c_2_0)
                                raw_ba[off+p20:off+p20+4] = c_10_0
                                patched_subs += 1

                        if patched_subs > 0:
                            # Recompress with LZ4 HC Level 10 and adjust to exact comp_sz
                            c10 = lz4.block.compress(
                                bytes(raw_ba), mode='high_compression', compression=10, store_size=False
                            )
                            idx = 0
                            decomp_acc = bytearray()
                            seqs = []
                            while idx < len(c10):
                                token = c10[idx]
                                idx += 1
                                lit_len = token >> 4
                                if lit_len == 15:
                                    while c10[idx] == 255:
                                        lit_len += 255
                                        idx += 1
                                    lit_len += c10[idx]
                                    idx += 1
                                lits = c10[idx:idx+lit_len]
                                idx += lit_len
                                decomp_acc.extend(lits)
                                if idx >= len(c10):
                                    seqs.append([bytearray(lits), 0, 0, bytearray()])
                                    break
                                offset = c10[idx] | (c10[idx+1] << 8)
                                idx += 2
                                match_len = (token & 0x0f) + 4
                                if (token & 0x0f) == 15:
                                    while c10[idx] == 255:
                                        match_len += 255
                                        idx += 1
                                    match_len += c10[idx]
                                    idx += 1
                                mb = bytearray()
                                for _ in range(match_len):
                                    b = decomp_acc[-offset]
                                    decomp_acc.append(b)
                                    mb.append(b)
                                seqs.append([bytearray(lits), offset, match_len, mb])

                            def encode_seqs(s_list):
                                out = bytearray()
                                for i, s in enumerate(s_list):
                                    is_last = (i == len(s_list) - 1)
                                    lit_len = len(s[0])
                                    mlen = 0 if is_last else s[2]
                                    m_code = 0 if is_last else min(mlen - 4, 15)
                                    out.append((min(lit_len, 15) << 4) | m_code)
                                    if lit_len >= 15:
                                        rem = lit_len - 15
                                        while rem >= 255:
                                            out.append(255)
                                            rem -= 255
                                        out.append(rem)
                                    out.extend(s[0])
                                    if not is_last:
                                        out.append(s[1] & 0xff)
                                        out.append((s[1] >> 8) & 0xff)
                                        if mlen - 4 >= 15:
                                            rem = mlen - 4 - 15
                                            while rem >= 255:
                                                out.append(255)
                                                rem -= 255
                                            out.append(rem)
                                return bytes(out)

                            curr_enc = encode_seqs(seqs)
                            i = 0
                            while len(curr_enc) < comp_sz and i < len(seqs) - 1:
                                s = seqs[i]
                                ns = seqs[i+1]
                                if s[2] == 4 and len(s[0]) < 10 and len(ns[0]) < 10:
                                    ns[0] = s[0] + s[3] + ns[0]
                                    seqs.pop(i)
                                    curr_enc = encode_seqs(seqs)
                                else:
                                    i += 1

                            if len(curr_enc) == comp_sz:
                                verify = lz4.block.decompress(curr_enc, uncompressed_size=decomp_sz)
                                if verify == raw_ba:
                                    ba[p+8:p+8+comp_sz] = curr_enc
                                    total_patched += patched_subs
                                    p = (p + 8 + comp_sz + 3) & ~3
                                    continue
            except Exception:
                pass
        p += 4

    if total_patched > 0:
        return bytes(ba), total_patched
    return None, 0


def _patch_laser_dance_assets(name: str, data: bytes) -> tuple[bytes | None, str | None]:
    """Patch Laser Dance asset files so laser renderers and materials stay active and visible
    at close proximity:
    1. sharedassets0.assets.split0: redirect Material 97 (LaserBeam) shader pointer from 281
       (FX/Laser Beam Hard Edges) to 279 (Custom/UnlitColorInstancedFading).
    2. sharedassets0.assets.split79: redirect LineRenderers 5495 and 5497 (beam_LOD0) material
       pointer from Material 97 to Material 45 (LaserBeam with Shader 279).
    3. sharedassets0.assets.split80: adjust LineLod components (13814 and 13936) distances:
       - 13814: max_d of Rule 0 (LOD0) -> 999.0, min_d of Rule 1 (LOD1) -> 0.0
       - 13936: max_d of Rule 4 (LOD0) -> 9999.0, min_d of Rule 5 (LOD1) -> 0.0
       ensuring laser beam renderers are enabled continuously at all player distances.
    """
    if name.endswith("sharedassets0.assets.split0"):
        ba = bytearray(data)
        off = 0x7b2a0
        if off + 12 <= len(ba) and struct.unpack("<Iq", ba[off : off + 12]) == (0, 281):
            ba[off : off + 12] = struct.pack("<Iq", 0, 279)
            return bytes(ba), "redirected Material 97 shader from 281 to 279"

    elif name.endswith("sharedassets0.assets.split79"):
        ba = bytearray(data)
        patched = 0
        for off in [0x20a38, 0x20d78]:
            if off + 12 <= len(ba) and struct.unpack("<Iq", ba[off : off + 12]) == (0, 97):
                ba[off : off + 12] = struct.pack("<Iq", 0, 45)
                patched += 1
        if patched > 0:
            return bytes(ba), f"redirected {patched} LineRenderer materials from 97 to 45"

    elif name.endswith("sharedassets0.assets.split80"):
        ba = bytearray(data)
        c13814_off = 0x9958
        c13936_off = 0xeb40
        patched = 0
        if c13814_off + 128 <= len(ba):
            if round(struct.unpack("<f", ba[c13814_off + 52 : c13814_off + 56])[0], 2) == 2.30:
                ba[c13814_off + 52 : c13814_off + 56] = struct.pack("<f", 999.0)
                patched += 1
            if round(struct.unpack("<f", ba[c13814_off + 124 : c13814_off + 128])[0], 2) == 2.30:
                ba[c13814_off + 124 : c13814_off + 128] = struct.pack("<f", 0.0)
                patched += 1

        if c13936_off + 436 <= len(ba):
            if round(struct.unpack("<f", ba[c13936_off + 360 : c13936_off + 364])[0], 2) == 2.20:
                ba[c13936_off + 360 : c13936_off + 364] = struct.pack("<f", 9999.0)
                patched += 1
            if round(struct.unpack("<f", ba[c13936_off + 432 : c13936_off + 436])[0], 2) == 2.20:
                ba[c13936_off + 432 : c13936_off + 436] = struct.pack("<f", 0.0)
                patched += 1

        if patched > 0:
            return bytes(ba), f"adjusted {patched} LineLod distance rules to maintain close-range laser visibility"

    return None, None


class MetaDepthOcclusion(Il2cppReturnPatch):
    id = "frame.meta_depth_occlusion"
    title = "Unity: disable Meta depth occlusion"
    description = (
        "Steam Frame lacks Meta Quest's physical depth sensor (XR_META_environment_depth), leaving "
        "environment depth buffers uninitialized. In shaders using Meta's Depth API (such as Laser Dance's "
        "FX/Laser), the invalid depth causes the shader to treat virtual objects near the player as occluded "
        "by the physical environment, making objects (e.g. laser beams) disappear when the player approaches "
        "them. Rewrites EnvironmentDepthManager methods in libil2cpp.so so depth occlusion keywords are never "
        "enabled at runtime, keeping virtual objects visible at all distances."
    )
    order = 48
    revision = 6
    targets = {
        "Meta.XR.EnvironmentDepth/Meta/XR/EnvironmentDepth/EnvironmentDepthManager.cs": {
            "get_IsSupported": RET_ZERO,
            "get_IsDepthAvailable": RET_ZERO,
            "get_OcclusionShadersMode": RET_ZERO,
            "SetOcclusionShaderKeywords": RET_ZERO,
        },
        "Assembly-CSharp/EnvironmentDepthManager.cs": {
            "get_IsSupported": RET_ZERO,
            "get_IsDepthAvailable": RET_ZERO,
            "get_OcclusionShadersMode": RET_ZERO,
            "SetOcclusionShaderKeywords": RET_ZERO,
        },
    }
    check_name = "Meta depth occlusion"

    def applies(self, a):
        return a.engine == "Unity"

    def detect(self, a):
        if a.engine == "Unity" and (a.extra or {}).get("meta_depth_occlusion"):
            return Suggestion(
                True,
                "App uses Meta Depth API occlusion keywords (HARD_OCCLUSION/SOFT_OCCLUSION), which "
                "cause virtual objects to vanish when approached due to missing depth sensor on Steam Frame.",
            )
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        # 1. First apply Il2cppReturnPatch (libil2cpp.so method returns)
        il2cpp_ok = super().apply(ctx)

        is_laser_dance = (
            getattr(ctx, "analysis", None)
            and getattr(ctx.analysis, "package", "") == "com.mrf.tvb.laser_dance"
        )

        # 2. In-place replace HARD_OCCLUSION and SOFT_OCCLUSION across all assets and metadata
        replaced_files = 0
        total_replacements = 0
        patched_shader_files = 0
        total_shader_subs = 0
        laser_dance_asset_patches = 0

        for name in list(ws.names()):
            if name.endswith(".assets") or ".assets.split" in name or name.endswith("global-metadata.dat"):
                data = ws.read(name)
                modified = False

                if is_laser_dance and ".assets.split" in name:
                    new_asset, note = _patch_laser_dance_assets(name, data)
                    if new_asset is not None:
                        data = new_asset
                        laser_dance_asset_patches += 1
                        ctx.notes.append(f"{name}: {note}")
                        modified = True

                # Patch compiled shader proximity fade first while LZ4 blobs are clean
                if name.endswith(".assets") or ".assets.split" in name:
                    new_data, subs = _patch_shader_proximity_fade(data)
                    if new_data is not None and subs > 0:
                        data = new_data
                        patched_shader_files += 1
                        total_shader_subs += subs
                        ctx.notes.append(
                            f"{name}: neutralized distance proximity fade in {subs} shader subprograms"
                        )
                        modified = True

                h_count = data.count(b"HARD_OCCLUSION")
                s_count = data.count(b"SOFT_OCCLUSION")
                if h_count or s_count:
                    data = (
                        data.replace(b"HARD_OCCLUSION", b"NO_OCCLUSION_X")
                        .replace(b"SOFT_OCCLUSION", b"NO_OCCLUSION_Y")
                    )
                    replaced_files += 1
                    total_replacements += (h_count + s_count)
                    ctx.notes.append(
                        f"{name}: disabled {h_count} HARD_OCCLUSION and {s_count} SOFT_OCCLUSION keywords"
                    )
                    modified = True

                if modified:
                    ws.put(name, data)

        if replaced_files > 0:
            ctx.reporter.log(
                f"Meta depth occlusion: neutralised {total_replacements} "
                f"occlusion keywords across {replaced_files} files"
            )
        if patched_shader_files > 0:
            ctx.reporter.log(
                f"Meta depth occlusion: neutralised proximity fade in {total_shader_subs} "
                f"shader subprograms across {patched_shader_files} files"
            )
        if laser_dance_asset_patches > 0:
            ctx.reporter.log(
                f"Meta depth occlusion: applied {laser_dance_asset_patches} Laser Dance laser asset fixes"
            )

        return il2cpp_ok or (replaced_files > 0) or (patched_shader_files > 0) or (laser_dance_asset_patches > 0)


register(MetaDepthOcclusion)

