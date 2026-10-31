import struct

from frameport.patches.frame import unity_mic_ring as M

# FMOD's OpenSL recorder start (Bigscreen's libunity.so, Unity 2020.3), from the buffer-size query to the locators:
#   sub x1, x29, #0x24; sub x2, x29, #0x28; bl getDSPBufferSize; cbnz w0, fail; adrp x8, page; ldr d0, [x8, #0xaa8];
#   ldur w8, [x29, #-0x28]; mov w11, #0x7bd; mov w9, #-1; movk w11, #0x8000, lsl #16
RECORDER = [0xD10093A1, 0xD100A3A2, 0x94000000, 0x35000000, 0x90000008, 0xFD455500, 0xB85D83A8, 0x5280F7AB,
            0x12800009, 0x72B0000B]
# FMOD's OpenSL player: the same buffer-size query and locator, no input device
PLAYER = [0xD10093A1, 0xD100A3A2, 0x94000000, 0x35000000, 0x90000008, 0xFD455500, 0xB85D83A8, 0x5280F7AB,
          0x52800049, 0x72B0000B]


def _blob(*code: list[int], at: int = 0x1000) -> bytes:
    data = bytearray(0x4000)
    for words in code:
        struct.pack_into(f"<{len(words)}I", data, at, *words)
        at += 0x800
    return bytes(data)


def test_widens_the_recorder_ring_only():
    data = _blob(PLAYER, RECORDER)  # player at 0x1000, recorder at 0x1800
    assert M.record_ring(data) == "original"
    out, n = M.widen_record_ring(data)
    assert n == 1
    assert out[0x1000:0x1028] == data[0x1000:0x1028]  # the player's buffers stay Unity's
    words = struct.unpack_from("<4I", out, 0x180C)
    # adrp x8 (one instruction earlier, same page); ldr d0 unchanged; mov w8, #16; stur w8, [x29, #-0x28]
    assert words == (0x90000008, 0xFD455500, 0x52800208, 0xB81D83A8)
    assert M.record_ring(out) == "widened"
    assert M.widen_record_ring(out) == (out, 0)  # idempotent


def test_adrp_is_re_encoded_across_a_page():
    code = list(RECORDER)
    data = bytearray(_blob(code, at=0x1FF4))  # cbnz at 0x2000, adrp at 0x2004: moving it keeps the target page
    struct.pack_into("<I", data, 0x2004, 0x90000008)  # adrp x8, page(0x2004) + 0 = 0x2000
    out, n = M.widen_record_ring(bytes(data))
    assert n == 1
    moved = struct.unpack_from("<I", out, 0x2000)[0]
    assert M._adrp_page(moved, 0x2000) == 0x2000
    data2 = bytearray(_blob(code, at=0x1FF8))  # cbnz at 0x2004... adrp at 0x2008, both in page 0x2000
    out2, _ = M.widen_record_ring(bytes(data2))
    assert struct.unpack_from("<I", out2, 0x2004)[0] == 0x90000008
    data3 = bytearray(_blob(code, at=0x1FF0))  # cbnz at 0x1FFC (page 0x1000), adrp at 0x2000 (page 0x2000)
    out3, _ = M.widen_record_ring(bytes(data3))
    adrp = struct.unpack_from("<I", out3, 0x1FFC)[0]
    assert adrp != 0x90000008 and M._adrp_page(adrp, 0x1FFC) == 0x2000


def test_unrecognised_code_is_left_alone():
    other = list(RECORDER)
    other[6] = 0xB85DC3A8  # ldur w8, [x29, #-0x24]: not the count getDSPBufferSize wrote
    data = _blob(other)
    assert M.record_ring(data) is None
    assert M.widen_record_ring(data) == (data, 0)


def test_suggested_for_unity_games_that_record():
    from types import SimpleNamespace

    p = M.UnityMicRing()
    game = SimpleNamespace(engine="Unity", abis=["arm64-v8a"], extra={"fmod_record_ring": True})
    assert p.applies(game) and p.detect(game).recommended
    assert p.detect(SimpleNamespace(engine="Unity", abis=["arm64-v8a"], extra={})) is None
