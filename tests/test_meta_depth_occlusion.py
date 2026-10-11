import os
import struct

import lz4.block

from frameport.patches.frame.meta_depth_occlusion import (
    _patch_laser_dance_assets,
    _patch_shader_proximity_fade,
)


def test_patch_shader_proximity_fade_synthetic():
    c_2_5 = struct.pack('<f', -2.5)
    c_2_0 = struct.pack('<f', -2.0)
    c_10_0 = struct.pack('<f', 10.0)

    # Construct synthetic raw blob
    # Header: count=1
    # Entry 0: off=16, length=64, seg=0
    # subprogram at offset 16 contains c_2_5 and c_2_0
    sub = b'SMOL' + b'\x00' * 10 + c_2_5 + b'\x00' * 4 + c_2_0 + b'\x00' * 38
    assert len(sub) == 64

    entry0 = struct.pack('<III', 16, len(sub), 0)
    raw = struct.pack('<I', 1) + entry0 + sub
    # Pad raw with pseudo-random repetitive data to > 60000 bytes so comp_sz > 5000
    noise = (os.urandom(256) * 300)[:60000]
    raw = raw + noise
    decomp_sz = len(raw)

    comp = lz4.block.compress(raw, mode='high_compression', compression=10, store_size=False)
    comp_sz = len(comp)

    # Wrap in asset data: decomp_sz (uint32), comp_sz (uint32), comp_bytes
    asset_data = struct.pack('<II', decomp_sz, comp_sz) + comp

    new_data, subs = _patch_shader_proximity_fade(asset_data)
    assert new_data is not None
    assert subs == 1
    assert len(new_data) == len(asset_data)

    # Decompress new_data and verify c_10_0 is present and c_2_0 is replaced
    new_comp = new_data[8:8+comp_sz]
    decomp = lz4.block.decompress(new_comp, uncompressed_size=decomp_sz)
    assert c_10_0 in decomp
    assert c_2_0 not in decomp[:80] # In subprogram area


def test_patch_laser_dance_assets():
    # Test split0: Material 97 shader redirection (281 -> 279)
    split0_data = bytearray(0x7b2a0 + 32)
    split0_data[0x7b2a0 : 0x7b2a0 + 12] = struct.pack("<Iq", 0, 281)
    new_split0, note0 = _patch_laser_dance_assets("sharedassets0.assets.split0", bytes(split0_data))
    assert new_split0 is not None
    assert struct.unpack("<Iq", new_split0[0x7b2a0 : 0x7b2a0 + 12]) == (0, 279)
    assert "Material 97" in note0

    # Test split79: LineRenderer materials redirection (97 -> 45)
    split79_data = bytearray(0x20d78 + 32)
    split79_data[0x20a38 : 0x20a38 + 12] = struct.pack("<Iq", 0, 97)
    split79_data[0x20d78 : 0x20d78 + 12] = struct.pack("<Iq", 0, 97)
    new_split79, note79 = _patch_laser_dance_assets("sharedassets0.assets.split79", bytes(split79_data))
    assert new_split79 is not None
    assert struct.unpack("<Iq", new_split79[0x20a38 : 0x20a38 + 12]) == (0, 45)
    assert struct.unpack("<Iq", new_split79[0x20d78 : 0x20d78 + 12]) == (0, 45)
    assert "LineRenderer" in note79

    # Test split80: LineLod distance rules
    split80_data = bytearray(0xeb40 + 500)
    split80_data[0x9958 + 52 : 0x9958 + 56] = struct.pack("<f", 2.30)
    split80_data[0x9958 + 124 : 0x9958 + 128] = struct.pack("<f", 2.30)
    split80_data[0xeb40 + 360 : 0xeb40 + 364] = struct.pack("<f", 2.20)
    split80_data[0xeb40 + 432 : 0xeb40 + 436] = struct.pack("<f", 2.20)
    new_split80, note80 = _patch_laser_dance_assets("sharedassets0.assets.split80", bytes(split80_data))
    assert new_split80 is not None
    assert round(struct.unpack("<f", new_split80[0x9958 + 52 : 0x9958 + 56])[0], 1) == 999.0
    assert struct.unpack("<f", new_split80[0x9958 + 124 : 0x9958 + 128])[0] == 0.0
    assert round(struct.unpack("<f", new_split80[0xeb40 + 360 : 0xeb40 + 364])[0], 1) == 9999.0
    assert struct.unpack("<f", new_split80[0xeb40 + 432 : 0xeb40 + 436])[0] == 0.0
    assert "LineLod" in note80

