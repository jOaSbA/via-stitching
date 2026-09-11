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

sys.path.insert(0, r"C:/Program Files/KiCad/6.0/bin/Lib/site-packages")
sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")

import wx
wx.DisableAsserts()

import pcbnew
import via_stitching_action_legacy as vsl

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
    netclasses = board.GetNetClasses()
    vcc_class = pcbnew.NETCLASSPTR("VCC_CLASS")
    vcc_class.SetClearance(vcc_clearance_nm)
    netclasses.Add(vcc_class)
    vcc.SetNetClass(vcc_class)

    track = pcbnew.PCB_TRACK(board)
    track.SetNet(vcc)
    track.SetLayer(pcbnew.F_Cu)
    track.SetStart(pcbnew.wxPoint(5 * MM, 0))
    track.SetEnd(pcbnew.wxPoint(5 * MM, 10 * MM))
    track.SetWidth(200_000)
    board.Add(track)

    vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
               via_dia_mm=0.4, drill_mm=0.2, spacing_mm=0.5, pattern="Square",
               x_offset_mm=0, y_offset_mm=0)

    gap = min(
        abs(v.GetPosition().x - 5 * MM)
        for v in board.GetTracks()
        if isinstance(v, pcbnew.PCB_VIA)
    )
    print(gap)


if __name__ == "__main__":
    main(int(sys.argv[1]))
