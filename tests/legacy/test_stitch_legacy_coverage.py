# Coverage tests for plugins_legacy/via_stitching_action_legacy.py's real
# stitch() function, run against real KiCad 6.0 boards. Extends
# dual_backend_spike_full_stitch_legacy.py (one realistic multi-layer run)
# with per-feature isolation: via types, patterns, the VIA_COUNT_WARN
# prompt, and all three avoid_* toggles.
#
# Two real findings from building these:
#  - A "Through" via always spans the full F_Cu-B_Cu board regardless of
#    the requested layer pair -- correct KiCad physics, not a bug; an
#    earlier version of this test wrongly asserted otherwise.
#  - avoid_footprints and avoid_same_net_pads must be tested against a
#    footprint/pad that ISN'T also caught by the always-on pad-copper
#    keepout, or the toggle's effect is masked by that separate keepout
#    already excluding the same area. Caught by a first draft of this test
#    using a PAD with an unset (default PTH) attribute, whose copper
#    keepout alone already blocked everything the footprint bbox would
#    have blocked.

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")

import pcbnew
import wx

# Importing via_stitching_action_legacy calls ActionPlugin.register() at
# module level, which can pop a real, modal "wxWidgets Debug Alert" on the
# desktop (PgmOrNull() assert, gitlab.com/kicad/code/kicad/-/issues/12833)
# that blocks this process until a human clicks it. Must run first.
wx.DisableAsserts()

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


def _board(layers, size=10 * MM):
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(layers)
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(gnd)
    layer_ids = [pcbnew.F_Cu, pcbnew.B_Cu] if layers == 2 else [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.B_Cu]
    for layer in layer_ids:
        z = pcbnew.ZONE(board)
        z.SetNet(gnd)
        z.SetLayer(layer)
        board.Add(z)
        z.SetFilledPolysList(layer, _square_polyset(0, 0, size, size))
    return board, gnd


def test_via_types_and_spans():
    expected_span = {
        pcbnew.VIATYPE_THROUGH: (pcbnew.F_Cu, pcbnew.B_Cu),  # always full board
        pcbnew.VIATYPE_BLIND_BURIED: (pcbnew.F_Cu, pcbnew.In1_Cu),
        pcbnew.VIATYPE_MICROVIA: (pcbnew.F_Cu, pcbnew.In1_Cu),
    }
    for via_type, (top, bottom) in expected_span.items():
        board, _ = _board(layers=3)
        placed, _ = vsl.stitch(
            board, via_type, pcbnew.F_Cu, pcbnew.In1_Cu, "GND",
            via_dia_mm=0.25, drill_mm=0.1, spacing_mm=1.0, pattern="Square",
            x_offset_mm=0, y_offset_mm=0,
        )
        vias = [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
        assert placed > 0
        assert all(v.GetViaType() == via_type for v in vias)
        assert all(v.TopLayer() == top and v.BottomLayer() == bottom for v in vias)


def test_all_patterns_place_something():
    for pattern in ["Hexagonal", "Square", "Staggered"]:
        board, _ = _board(layers=2)
        placed, _ = vsl.stitch(
            board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern=pattern,
            x_offset_mm=0, y_offset_mm=0,
        )
        assert placed > 0


def test_via_count_warn_prompt():
    import wx

    app = wx.App()
    original_threshold = vsl.VIA_COUNT_WARN
    original_msgbox = wx.MessageBox
    try:
        vsl.VIA_COUNT_WARN = 100

        wx.MessageBox = lambda *a, **k: wx.NO
        board_no, _ = _board(layers=2, size=200 * MM)
        placed_no, grouped_no = vsl.stitch(
            board_no, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
            x_offset_mm=0, y_offset_mm=0,
        )
        assert (placed_no, grouped_no) == (0, False)
        assert not any(isinstance(t, pcbnew.PCB_VIA) for t in board_no.GetTracks())

        wx.MessageBox = lambda *a, **k: wx.YES
        board_yes, _ = _board(layers=2, size=200 * MM)
        placed_yes, grouped_yes = vsl.stitch(
            board_yes, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
            x_offset_mm=0, y_offset_mm=0,
        )
        assert placed_yes > 100 and grouped_yes is True
    finally:
        wx.MessageBox = original_msgbox
        vsl.VIA_COUNT_WARN = original_threshold


def test_avoid_other_zones_toggle():
    def build():
        board, gnd = _board(layers=2)
        vcc = pcbnew.NETINFO_ITEM(board, "VCC")
        board.Add(vcc)
        z = pcbnew.ZONE(board)
        z.SetNet(vcc)
        z.SetLayer(pcbnew.F_Cu)
        board.Add(z)
        z.SetFilledPolysList(pcbnew.F_Cu, _square_polyset(0, 0, 5 * MM, 10 * MM))
        return board

    placed_off, _ = vsl.stitch(build(), pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                                via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
                                x_offset_mm=0, y_offset_mm=0, avoid_other_zones=False)
    placed_on, _ = vsl.stitch(build(), pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                               via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
                               x_offset_mm=0, y_offset_mm=0, avoid_other_zones=True)
    assert placed_on < placed_off


def test_avoid_footprints_toggle():
    # Small SMD pad (negligible copper keepout) + a large silkscreen body,
    # so the footprint bbox keepout is what's actually being isolated here
    # -- not masked by the always-on pad-copper keepout.
    def build():
        board, _ = _board(layers=2)
        fp = pcbnew.FOOTPRINT(board)
        fp.SetPosition(pcbnew.wxPoint(5 * MM, 5 * MM))
        pad = pcbnew.PAD(fp)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetSize(pcbnew.wxSize(200_000, 200_000))
        pad.SetPosition(pcbnew.wxPoint(int(5 * MM - 1.9 * MM), 5 * MM))
        pad.SetLayerSet(pcbnew.LSET(pcbnew.F_Cu))
        fp.Add(pad)
        body = pcbnew.FP_SHAPE(fp)
        body.SetLayer(pcbnew.F_SilkS)
        body.SetShape(pcbnew.S_RECT)
        body.SetStart(pcbnew.wxPoint(int(5 * MM - 2 * MM), int(5 * MM - 2 * MM)))
        body.SetEnd(pcbnew.wxPoint(int(5 * MM + 2 * MM), int(5 * MM + 2 * MM)))
        fp.Add(body)
        board.Add(fp)
        return board

    placed_off, _ = vsl.stitch(build(), pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                                via_dia_mm=0.6, drill_mm=0.3, spacing_mm=1.0, pattern="Square",
                                x_offset_mm=0, y_offset_mm=0, avoid_footprints=False)
    placed_on, _ = vsl.stitch(build(), pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                               via_dia_mm=0.6, drill_mm=0.3, spacing_mm=1.0, pattern="Square",
                               x_offset_mm=0, y_offset_mm=0, avoid_footprints=True)
    assert placed_on < placed_off


def test_avoid_same_net_pads_toggle():
    def build():
        board, gnd = _board(layers=2)
        fp = pcbnew.FOOTPRINT(board)
        pad = pcbnew.PAD(fp)
        pad.SetSize(pcbnew.wxSize(3 * MM, 3 * MM))
        pad.SetPosition(pcbnew.wxPoint(5 * MM, 5 * MM))
        pad.SetLayerSet(pcbnew.LSET(pcbnew.F_Cu))
        pad.SetNet(gnd)
        fp.Add(pad)
        board.Add(fp)
        return board

    placed_off, _ = vsl.stitch(build(), pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                                via_dia_mm=0.6, drill_mm=0.3, spacing_mm=1.0, pattern="Square",
                                x_offset_mm=0, y_offset_mm=0, avoid_same_net_pads=False)
    placed_on, _ = vsl.stitch(build(), pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                               via_dia_mm=0.6, drill_mm=0.3, spacing_mm=1.0, pattern="Square",
                               x_offset_mm=0, y_offset_mm=0, avoid_same_net_pads=True)
    assert placed_on < placed_off


def run():
    tests = [
        test_via_types_and_spans,
        test_all_patterns_place_something,
        test_via_count_warn_prompt,
        test_avoid_other_zones_toggle,
        test_avoid_footprints_toggle,
        test_avoid_same_net_pads_toggle,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print()
    print(f"ALL {len(tests)} COVERAGE TESTS PASSED")


if __name__ == "__main__":
    run()
