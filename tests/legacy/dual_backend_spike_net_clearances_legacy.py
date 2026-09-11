# Spike: per-net clearance dict behind _net_clearances, on legacy SWIG.
#
# Real code (via_stitching_action.py:_net_clearances): KiCad resolves the
# clearance between two items to the LARGER of their two netclass values, so
# it precomputes, for every net, max(own_net's_clearance, that_net's_clearance).
# This mirrors that combination step exactly; only the per-netclass lookup
# underneath differs (real code: board.get_netclass_for_nets(nets) via kipy;
# here: board.GetNetClasses().Find(name) per net, verified separately in the
# netclass spike).

from dual_backend_spike_netclass_legacy import swig_net_clearance, FALLBACK_CLEARANCE_NM


def swig_net_clearances(board, net_name):
    """Clearance in nm to hold between a via on net_name and each other net."""
    netclasses = board.GetNetClasses()
    values = {}
    for net in board.GetNetsByNetcode().values():
        name = net.GetNetname()
        nc = netclasses.Find(net.GetNetClassName()) or netclasses.GetDefault()
        values[name] = swig_net_clearance(nc)

    fallback = values.get(net_name, FALLBACK_CLEARANCE_NM)
    own = values.get(net_name, fallback)
    from collections import defaultdict
    clearances = defaultdict(lambda: FALLBACK_CLEARANCE_NM)
    clearances.update({name: max(own, value) for name, value in values.items()})
    return clearances


def demo():
    # Pure-logic check of the max-combination rule, independent of any board:
    # GND has clearance 150k, VCC has clearance 300k -> the entry each net
    # gets in its own dict should be max(own, other), not just "other".
    values = {"GND": 150_000, "VCC": 300_000}
    own = values["GND"]
    combined = {name: max(own, value) for name, value in values.items()}
    assert combined == {"GND": 150_000, "VCC": 300_000}  # max(150k,150k)=150k, max(150k,300k)=300k

    own_vcc = values["VCC"]
    combined_from_vcc = {name: max(own_vcc, value) for name, value in values.items()}
    assert combined_from_vcc == {"GND": 300_000, "VCC": 300_000}  # both floored at VCC's larger value

    print("net-clearance max-combination: correctly floors every entry at the requesting net's own value")


if __name__ == "__main__":
    demo()
