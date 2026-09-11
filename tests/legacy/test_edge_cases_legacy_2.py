# Second batch of edge-case tests, continuing the sweep in
# test_edge_cases_legacy.py. Same conventions: real KiCad 6.0 bindings,
# categorized, run to completion collecting all failures rather than
# stopping at the first.

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")

import pcbnew
import wx

# See test_edge_cases_legacy.py for why this must run before importing
# via_stitching_action_legacy: that import calls ActionPlugin.register(),
# which can pop a real, modal "wxWidgets Debug Alert" on the desktop
# (PgmOrNull() assert, gitlab.com/kicad/code/kicad/-/issues/12833) that
# blocks the process until a human clicks it.
wx.DisableAsserts()

import _fixtures as fixtures
import via_stitching_action_legacy as vsl
import _geometry_legacy as geo

_APP = wx.App()

MM = 1_000_000


def _square_polyset(x0, y0, x1, y1):
    ps = pcbnew.SHAPE_POLY_SET()
    chain = pcbnew.SHAPE_LINE_CHAIN()
    for (x, y) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
        chain.Append(pcbnew.VECTOR2I(int(x), int(y)))
    chain.SetClosed(True)
    ps.AddOutline(chain)
    return ps


def _board(layers=2, size=10 * MM, net_name="GND"):
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(layers)
    net = pcbnew.NETINFO_ITEM(board, net_name)
    board.Add(net)
    layer_ids = [pcbnew.F_Cu, pcbnew.B_Cu] if layers == 2 else [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.B_Cu]
    for layer in layer_ids:
        z = pcbnew.ZONE(board)
        z.SetNet(net)
        z.SetLayer(layer)
        board.Add(z)
        z.SetFilledPolysList(layer, _square_polyset(0, 0, size, size))
    return board, net


def _via_positions(board):
    return {(v.GetPosition().x, v.GetPosition().y) for v in board.GetTracks() if isinstance(v, pcbnew.PCB_VIA)}


# ---- track/zone/rule-area layer and net targeting --------------------------

def test_same_net_track_is_not_a_keepout():
    board, net = _board(layers=2)
    track = pcbnew.PCB_TRACK(board)
    track.SetNet(net)  # same net as the stitch target
    track.SetLayer(pcbnew.F_Cu)
    track.SetStart(geo.point(5 * MM, 0))
    track.SetEnd(geo.point(5 * MM, 10 * MM))
    track.SetWidth(500_000)
    board.Add(track)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.4, drill_mm=0.2, spacing_mm=1.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    positions = _via_positions(board)
    assert any(abs(x - 5 * MM) < 300_000 for (x, y) in positions), (
        "a same-net track wrongly excluded vias near it"
    )


def test_track_outside_span_is_not_a_keepout():
    """A blind via F.Cu<->In1.Cu should ignore a VCC track that only exists
    on B.Cu -- outside its span entirely."""
    board, net = _board(layers=3)
    vcc = pcbnew.NETINFO_ITEM(board, "VCC")
    board.Add(vcc)
    track = pcbnew.PCB_TRACK(board)
    track.SetNet(vcc)
    track.SetLayer(pcbnew.B_Cu)  # outside the F_Cu<->In1_Cu span
    track.SetStart(geo.point(5 * MM, 0))
    track.SetEnd(geo.point(5 * MM, 10 * MM))
    track.SetWidth(2 * MM)  # deliberately huge, so if it DID apply it would block everything
    board.Add(track)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_BLIND_BURIED, pcbnew.F_Cu, pcbnew.In1_Cu, "GND",
                            via_dia_mm=0.4, drill_mm=0.2, spacing_mm=1.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    positions = _via_positions(board)
    assert any(abs(x - 5 * MM) < 300_000 for (x, y) in positions), (
        "an out-of-span track wrongly excluded vias near it"
    )


def test_rule_area_on_wrong_layer_does_not_block():
    board, net = _board(layers=3)
    rule = pcbnew.ZONE(board)
    rule.SetIsRuleArea(True)
    rule.SetDoNotAllowVias(True)
    rule.SetLayer(pcbnew.B_Cu)  # outside the F_Cu<->In1_Cu span
    board.Add(rule)
    outline = pcbnew.SHAPE_LINE_CHAIN()
    for (x, y) in [(-1 * MM, -1 * MM), (11 * MM, -1 * MM), (11 * MM, 11 * MM), (-1 * MM, 11 * MM)]:
        outline.Append(pcbnew.VECTOR2I(int(x), int(y)))
    outline.SetClosed(True)
    rule.Outline().RemoveAllContours()
    rule.Outline().AddOutline(outline)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_BLIND_BURIED, pcbnew.F_Cu, pcbnew.In1_Cu, "GND",
                            via_dia_mm=0.4, drill_mm=0.2, spacing_mm=1.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    assert placed > 0, "a rule area on an out-of-span layer wrongly blocked everything"


def test_rule_area_with_a_net_assigned_is_still_treated_as_keepout():
    """A rule area can carry a net assignment in the file format even
    though it's meaningless for a keepout -- must not be treated as regular
    copper (excluded from layer_region) yet still function as a keepout."""
    board, net = _board(layers=2)
    rule = pcbnew.ZONE(board)
    rule.SetNet(net)  # net assigned, but...
    rule.SetIsRuleArea(True)  # ...it's a rule area, not copper
    rule.SetDoNotAllowVias(True)
    rule.SetLayer(pcbnew.F_Cu)
    board.Add(rule)
    outline = pcbnew.SHAPE_LINE_CHAIN()
    for (x, y) in [(4 * MM, 4 * MM), (6 * MM, 4 * MM), (6 * MM, 6 * MM), (4 * MM, 6 * MM)]:
        outline.Append(pcbnew.VECTOR2I(int(x), int(y)))
    outline.SetClosed(True)
    rule.Outline().RemoveAllContours()
    rule.Outline().AddOutline(outline)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.4, drill_mm=0.2, spacing_mm=0.5, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    positions = _via_positions(board)
    assert placed > 0
    for (x, y) in positions:
        assert not (4 * MM < x < 6 * MM and 4 * MM < y < 6 * MM), "via landed inside a net-tagged rule area"


# ---- same-net PTH pad: drill keepout still applies, copper keepout does not

def test_same_net_pth_pad_drill_keepout_still_applies():
    """avoid_same_net_pads only gates the COPPER keepout (pad_copper_keepout_shapes).
    A same-net PTH pad's hole-to-hole DRILL keepout (pad_drill_keepout_shapes)
    has no same-net exemption at all in the real code or this port -- a via
    still cannot physically overlap another hole regardless of net. Confirms
    that asymmetry is real and intentional, not a leftover gap."""
    board, net = _board(layers=2)
    fp = pcbnew.FOOTPRINT(board)
    pad = pcbnew.PAD(fp)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
    pad.SetSize(geo.size(1 * MM, 1 * MM))
    pad.SetDrillSize(geo.size(600_000, 600_000))
    pad.SetPosition(geo.point(5 * MM, 5 * MM))
    pad.SetLayerSet(fixtures.layer_set(pcbnew.F_Cu))
    pad.SetNet(net)  # same net as the stitch target
    fp.Add(pad)
    board.Add(fp)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.4, drill_mm=0.2, spacing_mm=0.3, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0, avoid_same_net_pads=False)
    positions = _via_positions(board)
    assert placed > 0
    for (x, y) in positions:
        dist = ((x - 5 * MM) ** 2 + (y - 5 * MM) ** 2) ** 0.5
        assert dist > 450_000, "a via overlapped a same-net PTH pad's drill hole"


# ---- offsets ----------------------------------------------------------------

def test_x_offset_actually_shifts_the_grid():
    """Comparing raw min(x) between the two runs doesn't work: corner
    rounding on the inset region means a different set of edge points
    survives the allowed() filter in each run, so the two runs' minimums
    aren't the same grid phase and can differ by more or less than the
    real offset. Comparing x mod spacing (the grid's phase) is the
    correct, corner-rounding-proof check -- confirmed by hand to shift by
    exactly the requested 1.0mm before writing this the second way."""
    board1, _ = _board(layers=2, size=20 * MM)
    board2, _ = _board(layers=2, size=20 * MM)
    vsl.stitch(board1, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
               via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
               x_offset_mm=0, y_offset_mm=0)
    vsl.stitch(board2, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
               via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
               x_offset_mm=1.0, y_offset_mm=0)
    spacing_nm = 2 * MM
    phase1 = {x % spacing_nm for (x, y) in _via_positions(board1)}
    phase2 = {x % spacing_nm for (x, y) in _via_positions(board2)}
    assert len(phase1) == 1 and len(phase2) == 1, "grid points should share one common phase"
    shift = (phase2.pop() - phase1.pop()) % spacing_nm
    assert abs(shift - 1 * MM) < 10_000  # within 0.01mm of the requested 1mm


def test_large_offset_pushing_grid_outside_region_raises_cleanly():
    board, net = _board(layers=2, size=5 * MM)
    try:
        vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                   via_dia_mm=0.4, drill_mm=0.2, spacing_mm=1.0, pattern="Square",
                   x_offset_mm=500, y_offset_mm=500)  # absurdly large offset
        raise AssertionError("expected a clean RuntimeError, got none")
    except RuntimeError:
        pass  # correct: "no candidates" style error, not a crash


# ---- VIA_COUNT_WARN boundary -------------------------------------------------

def test_via_count_warn_boundary_exact():
    """Exactly VIA_COUNT_WARN vias should NOT trigger the prompt (the real
    code's check is a strict '>', not '>='); one more should."""
    orig_threshold = vsl.VIA_COUNT_WARN
    orig_msgbox = wx.MessageBox
    prompted = []
    wx.MessageBox = lambda *a, **k: prompted.append(True) or wx.YES
    try:
        vsl.VIA_COUNT_WARN = 10
        board, net = _board(layers=2, size=50 * MM)
        vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                   via_dia_mm=0.2, drill_mm=0.1, spacing_mm=20.0, pattern="Square",
                   x_offset_mm=0, y_offset_mm=0)
        # whatever this placed, just confirm the boundary logic direction is sane:
        # re-run with a threshold guaranteed below the actual count and confirm
        # the prompt DOES fire, and with one guaranteed above and confirm it doesn't.
        placed_count = len(_via_positions(board))
        assert placed_count > 0

        prompted.clear()
        vsl.VIA_COUNT_WARN = placed_count - 1 if placed_count > 1 else 0
        board2, _ = _board(layers=2, size=50 * MM)
        vsl.stitch(board2, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                   via_dia_mm=0.2, drill_mm=0.1, spacing_mm=20.0, pattern="Square",
                   x_offset_mm=0, y_offset_mm=0)
        assert prompted, "threshold below the actual count should have prompted"

        prompted.clear()
        vsl.VIA_COUNT_WARN = placed_count
        board3, _ = _board(layers=2, size=50 * MM)
        vsl.stitch(board3, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                   via_dia_mm=0.2, drill_mm=0.1, spacing_mm=20.0, pattern="Square",
                   x_offset_mm=0, y_offset_mm=0)
        assert not prompted, "threshold exactly equal to the count should NOT have prompted (> not >=)"
    finally:
        wx.MessageBox = orig_msgbox
        vsl.VIA_COUNT_WARN = orig_threshold


# ---- pad with no layers set at all ------------------------------------------

def test_pad_with_empty_layerset_does_not_crash():
    board, net = _board(layers=2)
    fp = pcbnew.FOOTPRINT(board)
    pad = pcbnew.PAD(fp)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetSize(geo.size(1 * MM, 1 * MM))
    pad.SetPosition(geo.point(5 * MM, 5 * MM))
    pad.SetLayerSet(pcbnew.LSET())  # empty: on no layers at all
    fp.Add(pad)
    board.Add(fp)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.4, drill_mm=0.2, spacing_mm=1.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    assert placed > 0


# ---- multiple independent stitch runs on the same board --------------------

def test_two_nets_stitched_independently_avoid_each_other():
    """stitch()'s own parting refill calls Fill() on every zone on the
    board, and a headless Fill() (confirmed much earlier this session, on
    a bare BOARD(), CreateEmptyBoard(), and even a real loaded .kicad_pcb
    file) silently produces zero output outside a live KiCad GUI process.
    So the FIRST stitch() call here wipes the VCC zones' manually-injected
    fill as a side effect -- not a plugin bug (inside a real session,
    Fill() actually works, and refilling every zone on every run is the
    same thing the shipped IPC plugin already does on purpose), just a
    headless-test artifact that means the fill has to be re-injected
    before the second call."""
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(gnd)
    vcc = pcbnew.NETINFO_ITEM(board, "VCC")
    board.Add(vcc)
    vcc_zones = []
    for layer in [pcbnew.F_Cu, pcbnew.B_Cu]:
        zg = pcbnew.ZONE(board)
        zg.SetNet(gnd)
        zg.SetLayer(layer)
        board.Add(zg)
        zg.SetFilledPolysList(layer, _square_polyset(0, 0, 5 * MM, 10 * MM))
        zv = pcbnew.ZONE(board)
        zv.SetNet(vcc)
        zv.SetLayer(layer)
        board.Add(zv)
        zv.SetFilledPolysList(layer, _square_polyset(5 * MM, 0, 10 * MM, 10 * MM))
        vcc_zones.append((zv, layer))

    placed_gnd, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                                via_dia_mm=0.4, drill_mm=0.2, spacing_mm=1.0, pattern="Square",
                                x_offset_mm=0, y_offset_mm=0)

    for zv, layer in vcc_zones:
        zv.SetFilledPolysList(layer, _square_polyset(5 * MM, 0, 10 * MM, 10 * MM))

    placed_vcc, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "VCC",
                                via_dia_mm=0.4, drill_mm=0.2, spacing_mm=1.0, pattern="Square",
                                x_offset_mm=0, y_offset_mm=0)
    assert placed_gnd > 0 and placed_vcc > 0
    gnd_vias = [v for v in board.GetTracks() if isinstance(v, pcbnew.PCB_VIA) and v.GetNetname() == "GND"]
    vcc_vias = [v for v in board.GetTracks() if isinstance(v, pcbnew.PCB_VIA) and v.GetNetname() == "VCC"]
    assert all(v.GetPosition().x < 5 * MM for v in gnd_vias)
    assert all(v.GetPosition().x > 5 * MM for v in vcc_vias)
    # the second run's keepouts should include the first run's vias (other-net
    # drill keepout is unconditional, not gated by avoid_other_zones)
    for gv in gnd_vias:
        for vv in vcc_vias:
            dx = gv.GetPosition().x - vv.GetPosition().x
            dy = gv.GetPosition().y - vv.GetPosition().y
            assert (dx * dx + dy * dy) ** 0.5 > 300_000


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
        except Exception as e:
            failures.append((test.__name__, e))
            print(f"FAIL: {test.__name__}: {e!r}")
    print()
    print(f"{len(tests) - len(failures)}/{len(tests)} PASSED")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
