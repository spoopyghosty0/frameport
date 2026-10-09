"""frame.pac_hints (GitHub #83): unpaired paciasp/autiasp in hand-written assembly become NOPs; compiler-protected
libraries (several returns per signed function) and paired ones stay."""
from frameport.analysis.stubgen import build_stub_library
from frameport.patches.frame import pac_hints


def _lib(monkeypatch, pac: int, aut: int, rets: int = 2000) -> bytes:
    head = build_stub_library(["f"], soname="libx.so")  # a real ELF header (is_elf)
    pad = (-len(head)) % 4
    code = (pac_hints.PACIASP * pac + pac_hints.AUTIASP * aut + pac_hints.RET * rets)
    data = head + b"\0" * pad + code
    start = len(head) + pad
    monkeypatch.setattr(pac_hints, "_exec_ranges", lambda d: [(start, len(d))])
    return data


def test_unpaired_counts_are_found_and_paired_left_alone(monkeypatch):
    assert pac_hints.unpaired(_lib(monkeypatch, 1, 1)) is None
    found = pac_hints.unpaired(_lib(monkeypatch, 1, 2))
    assert found and len(found[0]) == 1 and len(found[1]) == 2


def test_compiler_protected_library_is_left_alone(monkeypatch):
    # e.g. OVRPort's loader: 1084 paciasp, 1114 autiasp, 7092 ret (stripping it hung OVRPlugin's start-up)
    assert pac_hints.unpaired(_lib(monkeypatch, 20, 30, rets=100)) is None


def test_triage_suggests_it():
    from frameport.validate import triage

    line = ("10-09 01:36:38.905  1272  1272 F DEBUG   : signal 4 (SIGILL), code 2 (ILL_ILLOPN), fault addr "
            "0xfffe9a0bbe40 (*pc=0xd50323bf)\n")
    hit = next(f for f in triage.triage("", "EXITED", crash=line).findings if f.id == "pac-unpaired")
    assert hit.suggest == ["frame.pac_hints"]
