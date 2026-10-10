"""The automatic pairing's network scan: only real LAN interfaces, and it backs off while it finds nothing."""
import socket
from types import SimpleNamespace as NS

from frameport.frame import autopair, discovery
from frameport.frame.connection import FrameTarget
from frameport.frame.discovery import is_virtual_interface, scannable_address


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_virtual_and_vpn_interfaces_are_not_scanned():
    for name in ("docker0", "br-3f2a", "veth12ab", "virbr0", "tun0", "tap0", "wg0", "tailscale0", "zt5u4y",
                 "utun3", "vEthernet (WSL (Hyper-V firewall))", "vEthernet (Default Switch)", "Tailscale",
                 "ZeroTier One [8056c2e21c]", "OpenVPN TAP-Windows6", "VirtualBox Host-Only Network", "vmnet8"):
        assert is_virtual_interface(name), name
    for name in ("eth0", "wlan0", "enp3s0", "wlp2s0", "en0", "Wi-Fi", "Ethernet 2", "usb0", "enx0a1b2c3d4e5f"):
        assert not is_virtual_interface(name), name
    for addr in ("192.168.1.20", "10.0.0.7", "10.86.200.234"):
        assert scannable_address(addr), addr
    for addr in ("127.0.0.1", "169.254.3.4", "100.64.0.1", "100.101.102.103", "100.127.255.254", "172.17.0.1",
                 "0.0.0.0", "nonsense"):
        assert not scannable_address(addr), addr
    assert scannable_address("100.128.0.1")  # just outside CGNAT


def test_local_subnets_skip_virtual(monkeypatch):
    import psutil

    addrs = {"wlan0": [NS(family=socket.AF_INET, address="192.168.1.20")],
             "docker0": [NS(family=socket.AF_INET, address="10.9.0.1")],
             "tailscale0": [NS(family=socket.AF_INET, address="100.70.1.2")],
             "eth1": [NS(family=socket.AF_INET, address="100.70.1.3")],  # CGNAT under a plain name
             "eth2": [NS(family=socket.AF_INET, address="10.1.2.3")]}  # down
    stats = {n: NS(isup=n != "eth2") for n in addrs}
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: addrs)
    monkeypatch.setattr(psutil, "net_if_stats", lambda: stats)
    assert [str(n) for n in discovery.local_subnets()] == ["192.168.1.0/24"]


def test_scan_backs_off_after_empty_scans():
    clock = Clock()
    s = autopair.ScanSchedule(clock=clock)
    nets = ("192.168.1.0/24",)
    waits = []
    assert s.due(nets)  # never scanned
    for _ in range(7):
        s.done([])
        start = clock.t
        while not s.due(nets):
            clock.t += 1
        waits.append(clock.t - start)
    assert waits == [30, 60, 120, 240, 300, 300, 300]
    s.done([FrameTarget("192.168.1.9")])  # found something: back to every 30 s
    clock.t += 29
    assert not s.due(nets)
    clock.t += 1
    assert s.due(nets)


def test_scan_backoff_resets():
    clock = Clock()
    s = autopair.ScanSchedule(clock=clock)
    nets = ("192.168.1.0/24",)
    s.due(nets)
    for _ in range(4):
        s.done([])
    assert s.interval() == 240 and not s.due(nets)
    assert s.due(("10.0.0.0/24",)) and s.interval() == 30  # another network: scan now, start over
    s.done([])
    s.done([])
    assert not s.due(("10.0.0.0/24",))
    s.reset()  # the Frame page was opened
    assert s.due(("10.0.0.0/24",)) and s.interval() == 30


def test_rescan_soon_resets_the_shared_schedule(monkeypatch):
    s = autopair.ScanSchedule(empty=4, last=0.0)
    monkeypatch.setattr(autopair, "_schedule", s)
    autopair.rescan_soon()
    assert s.empty == 0 and s.last is None


def test_scan_follows_the_schedule(monkeypatch):
    monkeypatch.setattr(discovery, "local_subnets", lambda: [])
    monkeypatch.setattr(discovery, "local_addresses", lambda: set())
    clock = Clock()
    s = autopair.ScanSchedule(clock=clock)
    assert autopair._scan_devkits(s) == [] and s.empty == 1 and s.last == 0
    clock.t += 5
    autopair._scan_devkits(s)
    assert s.empty == 1  # not due yet: no scan
