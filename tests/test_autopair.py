"""Frames in Developer Mode pair themselves (frame/autopair.py): connect one that knows FramePort, keep offering the key
to one that doesn't until "Pair new host" is open and it's approved, back off after an unanswered request."""
from frameport.frame import autopair
from frameport.frame.autopair import AutoPairer
from frameport.frame.connection import FrameTarget
from frameport.frame.devkit import PairingRefused

NOT_OPEN = PairingRefused("The Frame didn't pair: on the Frame open Settings → Developer → Pair new host first, "
                          "then click Connect again")
TIMED_OUT = PairingRefused("The Frame didn't pair: it wasn't approved in the headset within 30 seconds")


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def make(frames, logins, register_results, active=True):
    ready, statuses, asked, clock = [], [], [], Clock()

    def register(host):
        asked.append(host)
        r = register_results.pop(0) if register_results else NOT_OPEN
        if isinstance(r, Exception):
            raise r

    def try_login(t):
        r = logins[t.host]
        if isinstance(r, Exception):
            raise r
        return r
    p = AutoPairer(lambda: list(frames), try_login, register, ready.append, lambda: active,
                   lambda kind, detail: statuses.append((kind, detail)), clock=clock)
    return p, ready, statuses, asked, clock


def test_a_frame_that_knows_framePort_is_connected_without_asking():
    f = FrameTarget("10.0.0.5", name="steamframe")
    p, ready, _, asked, _ = make([f], {"10.0.0.5": True}, [])
    p.step()
    assert ready == [f] and asked == []


def test_waits_for_pair_new_host_then_pairs():
    f = FrameTarget("10.0.0.5", name="steamframe")
    p, ready, statuses, asked, clock = make([f], {"10.0.0.5": False}, [NOT_OPEN, None])
    p.step()  # not open yet: refused at once, says what to do
    assert ready == [] and statuses == [("waiting", "steamframe")]
    p.step()  # too soon: no new request
    assert len(asked) == 1
    clock.t += autopair.RETRY_REFUSED
    p.step()  # open now and approved in the headset
    assert ready == [f] and len(asked) == 2 and p.candidates == {}


def test_backs_off_after_an_unanswered_request():
    f = FrameTarget("10.0.0.5", name="steamframe")
    p, ready, statuses, asked, clock = make([f], {"10.0.0.5": False}, [TIMED_OUT])
    p.step()
    assert statuses[-1][0] == "refused"
    clock.t += autopair.RETRY_REFUSED
    p.step()
    assert len(asked) == 1  # still backing off
    clock.t += autopair.RETRY_IGNORED
    p.step()
    assert len(asked) == 2


def test_inactive_or_gone():
    f = FrameTarget("10.0.0.5")
    p, ready, _, asked, _ = make([f], {"10.0.0.5": True}, [], active=False)
    p.step()
    assert ready == [] and asked == []  # a Frame is connected (or the setting is off): nothing happens
    p, ready, _, asked, _ = make([f], {"10.0.0.5": OSError("gone")}, [])
    p.step()
    assert ready == [] and asked == [] and p.candidates == {}


def test_the_login_check_runs_once_per_frame():
    f = FrameTarget("10.0.0.5")
    calls = []
    p, ready, _, asked, clock = make([f], {"10.0.0.5": False}, [NOT_OPEN, NOT_OPEN])
    real = p.try_login
    p.try_login = lambda t: (calls.append(t), real(t))[1]
    p.step()
    clock.t += autopair.RETRY_REFUSED
    p.step()
    assert len(calls) == 1 and len(asked) == 2
