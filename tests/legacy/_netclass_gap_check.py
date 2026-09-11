# Standalone helper, run as its own subprocess (never imported) by
# test_full_pipeline_respects_higher_netclass_clearance.
#
# Building two boards with programmatically-added NETCLASSPTR objects in
# the SAME Python process was confirmed to corrupt netclass lookups for
# BOTH boards -- deterministic across three separate repeated runs, with
# or without the "canonical" NetNames()+SynchronizeNetsAndNetClasses()
# assignment path, not fixed by holding extra Python references to dodge
# a GC theory. This never affects the real plugin (it only ever reads
# netclasses KiCad already loaded from a project file; it never
# constructs one), so it's a test-isolation problem, not a plugin bug --
# solved here by giving each clearance value its own fresh process.

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")

import wx
wx.DisableAsserts()

import pcbnew
import _geometry_legacy as geo
import via_stitching_action_legacy as vsl


def _assign_netclass(board, net, name, clearance_nm):
    """Give `net` a netclass with this clearance, or return False.

    Netclasses are the one thing the real plugin never builds, only reads, and
    every KiCad version builds them differently: 6 has NETCLASSPTR plus
    NETCLASSES.Add(), 7 dropped NETCLASSPTR and keeps a std::map behind
    m_NetSettings. Rather than guess whether the wiring took, assign and then
    ask the net what its class is called."""
    ctor = getattr(pcbnew, "NETCLASSPTR", None) or getattr(pcbnew, "NETCLASS", None)
    if ctor is None:
        return False
    netclass = ctor(name)
    netclass.SetClearance(clearance_nm)

    settings = getattr(board.GetDesignSettings(), "m_NetSettings", None)
    try:
        if settings is not None:
            settings.m_NetClasses[name] = netclass
        else:
            board.GetDesignSettings().GetNetClasses().Add(netclass)
        net.SetNetClass(netclass)
    except Exception:
        return False
    return net.GetNetClassName() == name

MM = 1_000_000


def _square_polyset(x0, y0, x1, y1):
    ps = pcbnew.SHAPE_POLY_SET()
    chain = pcbnew.SHAPE_LINE_CHAIN()
    for (x, y) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
        chain.Append(pcbnew.VECTOR2I(int(x), int(y)))
    chain.SetClosed(True)
    ps.AddOutline(chain)
    return ps


def main(vcc_clearance_nm):
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(gnd)
    for layer in [pcbnew.F_Cu, pcbnew.B_Cu]:
        z = pcbnew.ZONE(board)
        z.SetNet(gnd)
        z.SetLayer(layer)
        board.Add(z)
        z.SetFilledPolysList(layer, _square_polyset(0, 0, 10 * MM, 10 * MM))

    vcc = pcbnew.NETINFO_ITEM(board, "VCC")
    board.Add(vcc)
    if not _assign_netclass(board, vcc, "VCC_CLASS", vcc_clearance_nm):
        print("SKIP")
        return

    track = pcbnew.PCB_TRACK(board)
    track.SetNet(vcc)
    track.SetLayer(pcbnew.F_Cu)
    track.SetStart(geo.point(5 * MM, 0))
    track.SetEnd(geo.point(5 * MM, 10 * MM))
    track.SetWidth(200_000)
    board.Add(track)

    vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
               via_dia_mm=0.4, drill_mm=0.2, spacing_mm=0.5, pattern="Square",
               x_offset_mm=0, y_offset_mm=0)

    # What the plugin's own resolver made of the netclass we just built. On a
    # build where the clearance never reaches net_clearances, the gap comes out
    # the same for every value and the caller needs to see why.
    clearances = geo.net_clearances(board, "GND")
    print(f"VCC clearance resolved to {clearances['VCC']} nm "
          f"(asked for {vcc_clearance_nm}), netclass name {vcc.GetNetClassName()}",
          file=sys.stderr)

    gap = min(
        abs(v.GetPosition().x - 5 * MM)
        for v in board.GetTracks()
        if isinstance(v, pcbnew.PCB_VIA)
    )
    print(gap)


if __name__ == "__main__":
    main(int(sys.argv[1]))
