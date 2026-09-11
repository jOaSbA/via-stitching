# Spike: net clearance lookup behind _net_clearances, on legacy SWIG.
#
# kipy: NetClass.clearance is None when the netclass has no value of its own
# (inherits the board minimum) -- _net_clearances has to catch that itself
# and substitute FALLBACK_CLEARANCE_MM.
#
# pcbnew: NETCLASS.GetClearanceOpt() (std::optional<int>, added later) is the
# unset-vs-set distinction; GetClearance() is the older, always-there method
# that returns an already-resolved value -- confirmed both exist side by side
# in the current NETCLASS docs. Calling the resolved one means the "is this
# unset" branch this spike's IPC side needs doesn't need porting at all: KiCad
# itself already did the resolution before handing the number back.
#
# The real fallback value still has to exist somewhere, though: GetClearance()
# on a genuinely brand-new/default-constructed NETCLASS (never part of a real
# board) would return 0, not FALLBACK_CLEARANCE_MM's mm value -- an edge case
# that can't actually happen against a real board (every board has a "Default"
# netclass with a real clearance), but is worth a comment where this lands for
# real, not silently assumed away.
#
# Real wall found getting a netclass object in the first place: on a real
# KiCad 6.0 board, both net.GetNetClass() and pad.GetEffectiveNetclass()
# return an untyped SwigPyObject with no usable methods at all (no
# GetClearance, nothing) -- a broken typemap in this binding, not a misuse.
# The working path is board.GetNetClasses().Find(net.GetNetClassName()) --
# GetNetClassName() is a plain string (no typemap to break), and NETCLASSES
# (from board.GetNetClasses()) returns a properly-typed NETCLASSPTR from
# both .Find(name) and .GetDefault(). Confirmed against a real board: this
# path resolves the Default netclass's clearance correctly; the direct
# accessors do not.

FALLBACK_CLEARANCE_NM = 200_000  # 0.2 mm, matches FALLBACK_CLEARANCE_MM


def ipc_net_clearance(netclass):
    """kipy shape: None means unset, caller substitutes the fallback."""
    return FALLBACK_CLEARANCE_NM if netclass.clearance is None else netclass.clearance


def swig_net_clearance(netclass):
    """pcbnew shape: GetClearance() is pre-resolved, no None case to handle."""
    return netclass.GetClearance()


# ---- fakes ----

class _FakeIpcNetClass:
    def __init__(self, clearance):
        self.clearance = clearance  # None or an int

class _FakeSwigNetClass:
    def __init__(self, clearance):
        self._clearance = clearance  # always a real int on a real board
    def GetClearance(self):
        return self._clearance


def demo():
    # A netclass with its own explicit clearance: both sides agree.
    assert ipc_net_clearance(_FakeIpcNetClass(150_000)) == 150_000
    assert swig_net_clearance(_FakeSwigNetClass(150_000)) == 150_000

    # A netclass that inherits the board minimum: kipy needs the fallback
    # substituted by hand; pcbnew's GetClearance() already did that upstream,
    # so the fake here is seeded with the resolved value directly, not None --
    # there is no SWIG-side "unset" state to fake in the first place.
    assert ipc_net_clearance(_FakeIpcNetClass(None)) == FALLBACK_CLEARANCE_NM
    assert swig_net_clearance(_FakeSwigNetClass(FALLBACK_CLEARANCE_NM)) == FALLBACK_CLEARANCE_NM

    print("net clearance: kipy needs an explicit None-fallback branch; legacy pcbnew doesn't")


if __name__ == "__main__":
    demo()
