# Broad edge-case sweep for the legacy backend, run against real KiCad 6.0
# bindings. Organized by category: geometry degeneracies, input validation,
# board configuration oddities, keepout interactions, and grouping/deletion
# robustness (the last one directly motivated by a real session where a
# user's Ctrl+Z after deleting plugin-placed vias left visible-but-
# unselectable ghost items in the live UI -- confirmed to be a pure
# in-memory view/connectivity desync, never written to the saved file, but
# worth hardening the plugin's OWN grouping/deletion path against, since
# that path is the one thing that doesn't depend on KiCad's native undo).

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")

import pcbnew
import wx

# Importing via_stitching_action_legacy calls ActionPlugin.register() at
# module level, which can hit a real KiCad bug (PgmOrNull() assert failing
# in ACTION_PLUGINS::register_action() -- gitlab.com/kicad/code/kicad/-/
# issues/12833) when run outside a fully-initialized KiCad app process,
# exactly what every one of these test scripts does. Unhandled, that pops
# a real, modal "wxWidgets Debug Alert" dialog on the actual desktop that
# blocks this process until a human clicks it -- confirmed live, not
# theoretical. DisableAsserts() must run before that import.
wx.DisableAsserts()

import _fixtures as fixtures
import via_stitching_action_legacy as vsl
import _geometry_legacy as geo

# One persistent wx.App for the whole test run, not a local variable inside
# a single test function -- a wx.App created as a function-local goes out
# of scope (and can be torn down) the moment that function returns, so a
# later test's dialog construction can hit "PyNoAppError: The wx.App
# object must be created first!" even though an earlier test's app call
# appeared to succeed.
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


def expect_runtime_error(fn, needle=None):
    try:
        fn()
    except RuntimeError as e:
        if needle:
            assert needle.lower() in str(e).lower(), f"expected {needle!r} in {e!r}"
        return
    raise AssertionError("expected RuntimeError, none raised")


# ---- geometry degeneracies -------------------------------------------------

def test_zone_with_two_disjoint_islands():
    """A net poured as two separate disconnected blobs (e.g. split by a
    slot) -- stitch() should still find candidates in both, independently."""
    board, net = _board(layers=2, size=20 * MM)
    for z in list(board.Zones()):
        board.Remove(z)
    for layer in [pcbnew.F_Cu, pcbnew.B_Cu]:
        z = pcbnew.ZONE(board)
        z.SetNet(net)
        z.SetLayer(layer)
        board.Add(z)
        ps = pcbnew.SHAPE_POLY_SET()
        for (x0, y0, x1, y1) in [(0, 0, 5 * MM, 5 * MM), (15 * MM, 15 * MM, 20 * MM, 20 * MM)]:
            chain = pcbnew.SHAPE_LINE_CHAIN()
            for (x, y) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
                chain.Append(pcbnew.VECTOR2I(int(x), int(y)))
            chain.SetClosed(True)
            ps.AddOutline(chain)
        z.SetFilledPolysList(layer, ps)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=1.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    vias = [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    xs = sorted(v.GetPosition().x for v in vias)
    assert placed > 0
    assert xs[0] < 5 * MM and xs[-1] > 15 * MM  # candidates landed in both islands


def test_zone_with_hole_excludes_the_hole():
    """A donut-shaped pour (hole in the middle) -- no via should land
    inside the hole even though it's well within the outer bounds."""
    board, net = _board(layers=2, size=10 * MM)
    for z in list(board.Zones()):
        board.Remove(z)
    for layer in [pcbnew.F_Cu, pcbnew.B_Cu]:
        z = pcbnew.ZONE(board)
        z.SetNet(net)
        z.SetLayer(layer)
        board.Add(z)
        ps = pcbnew.SHAPE_POLY_SET()
        outer = pcbnew.SHAPE_LINE_CHAIN()
        for (x, y) in [(0, 0), (10 * MM, 0), (10 * MM, 10 * MM), (0, 10 * MM)]:
            outer.Append(pcbnew.VECTOR2I(int(x), int(y)))
        outer.SetClosed(True)
        ps.AddOutline(outer)
        hole = pcbnew.SHAPE_LINE_CHAIN()
        for (x, y) in [(3 * MM, 3 * MM), (7 * MM, 3 * MM), (7 * MM, 7 * MM), (3 * MM, 7 * MM)]:
            hole.Append(pcbnew.VECTOR2I(int(x), int(y)))
        hole.SetClosed(True)
        ps.AddHole(hole, 0)
        z.SetFilledPolysList(layer, ps)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.4, drill_mm=0.2, spacing_mm=0.5, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    vias = [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    assert placed > 0
    for v in vias:
        pos = v.GetPosition()
        assert not (3 * MM < pos.x < 7 * MM and 3 * MM < pos.y < 7 * MM), "a via landed inside the hole"


def test_rule_area_covering_entire_region_blocks_everything():
    board, net = _board(layers=2, size=5 * MM)
    rule = pcbnew.ZONE(board)
    rule.SetIsRuleArea(True)
    rule.SetDoNotAllowVias(True)
    rule.SetLayer(pcbnew.F_Cu)
    board.Add(rule)
    outline = pcbnew.SHAPE_LINE_CHAIN()
    for (x, y) in [(-1 * MM, -1 * MM), (6 * MM, -1 * MM), (6 * MM, 6 * MM), (-1 * MM, 6 * MM)]:
        outline.Append(pcbnew.VECTOR2I(int(x), int(y)))
    outline.SetClosed(True)
    rule.Outline().RemoveAllContours()
    rule.Outline().AddOutline(outline)
    expect_runtime_error(
        lambda: vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=1.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0),
        needle="blocked",
    )


def test_via_diameter_bigger_than_zone_raises_no_room():
    board, net = _board(layers=2, size=1 * MM)  # tiny zone
    expect_runtime_error(
        lambda: vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=5.0, drill_mm=2.0, spacing_mm=1.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0),
        needle="no room",
    )


def test_start_end_layers_swapped_is_equivalent():
    """User picks End before Start in the layer order -- span_layers already
    sorts them, so the result should be identical either way."""
    board, net = _board(layers=3)
    board2, net2 = _board(layers=3)
    placed_normal, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.In1_Cu, "GND",
                                   via_dia_mm=0.4, drill_mm=0.2, spacing_mm=1.0, pattern="Square",
                                   x_offset_mm=0, y_offset_mm=0)
    placed_swapped, _ = vsl.stitch(board2, pcbnew.VIATYPE_THROUGH, pcbnew.In1_Cu, pcbnew.F_Cu, "GND",
                                    via_dia_mm=0.4, drill_mm=0.2, spacing_mm=1.0, pattern="Square",
                                    x_offset_mm=0, y_offset_mm=0)
    assert placed_normal == placed_swapped


# ---- board configuration oddities ------------------------------------------

def test_board_with_only_the_implicit_no_net():
    """A board with zero real nets (just the implicit netcode-0 entry) --
    stitching any real net name should fail cleanly, not crash."""
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    expect_runtime_error(
        lambda: vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=1.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0),
        needle="not found",
    )


def test_net_exists_but_never_poured():
    board, net = _board(layers=2)
    unpoured = pcbnew.NETINFO_ITEM(board, "UNPOURED")
    board.Add(unpoured)
    expect_runtime_error(
        lambda: vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "UNPOURED",
                            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=1.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0),
        needle="filled copper",
    )


def test_unicode_net_name():
    board, net = _board(layers=2, net_name="GNDé_🜂")
    placed, grouped = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GNDé_🜂",
                                  via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
                                  x_offset_mm=0, y_offset_mm=0)
    assert placed > 0 and grouped


def test_stitch_itself_does_not_validate_start_equals_end_layer():
    """Known, deliberate gap -- confirmed the real IPC stitch() has the same
    shape: only its dialog's values() checks start_layer == end_layer
    (via_stitching_action.py:1383); stitch() itself does not. Calling
    stitch() directly with the same layer twice silently creates
    zero-span "vias" (TopLayer == BottomLayer) for anything but Through
    (which always forces a full span regardless, masking the issue) --
    real and reproducible, but a caller-contract gap shared with the IPC
    version, not a legacy-only regression. The dialog's values() already
    guards against this in normal use; this test documents what happens
    if that guard is ever bypassed."""
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(1)
    net = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(net)
    z = pcbnew.ZONE(board)
    z.SetNet(net)
    z.SetLayer(pcbnew.F_Cu)
    board.Add(z)
    z.SetFilledPolysList(pcbnew.F_Cu, _square_polyset(0, 0, 10 * MM, 10 * MM))
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_BLIND_BURIED, pcbnew.F_Cu, pcbnew.F_Cu, "GND",
                            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=1.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    vias = [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    assert placed > 0
    assert all(v.TopLayer() == v.BottomLayer() for v in vias)  # confirmed: silently degenerate


# ---- keepout interactions ---------------------------------------------------

def test_existing_via_exactly_on_a_grid_point_gets_nudged_or_dropped():
    """A real SWIG gotcha caught building this test, not the plugin: SWIG
    returns a fresh Python wrapper object from every GetTracks() call, so
    `t is not existing` never excludes anything -- `t.this == existing.this`
    (or, here, a plain position check) is the correct identity comparison."""
    board, net = _board(layers=2, size=10 * MM)
    existing = geo.make_via(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu,
                             600_000, 300_000, net.GetNetCode(), 4 * MM, 4 * MM)
    board.Add(existing)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    existing_pos = (existing.GetPosition().x, existing.GetPosition().y)
    new_vias = [
        t for t in board.GetTracks()
        if isinstance(t, pcbnew.PCB_VIA) and (t.GetPosition().x, t.GetPosition().y) != existing_pos
    ]
    for v in new_vias:
        dx = v.GetPosition().x - existing.GetPosition().x
        dy = v.GetPosition().y - existing.GetPosition().y
        assert (dx * dx + dy * dy) ** 0.5 > 300_000, "a new via landed on top of the existing one"


def test_zero_length_track_does_not_crash():
    board, net = _board(layers=2, size=10 * MM)
    vcc = pcbnew.NETINFO_ITEM(board, "VCC")
    board.Add(vcc)
    track = pcbnew.PCB_TRACK(board)
    track.SetNet(vcc)
    track.SetLayer(pcbnew.F_Cu)
    track.SetStart(geo.point(5 * MM, 5 * MM))
    track.SetEnd(geo.point(5 * MM, 5 * MM))  # zero length
    track.SetWidth(200_000)
    board.Add(track)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    assert placed > 0  # just needs to not crash


def test_degenerate_arc_does_not_crash():
    board, net = _board(layers=2, size=10 * MM)
    vcc = pcbnew.NETINFO_ITEM(board, "VCC")
    board.Add(vcc)
    arc = pcbnew.PCB_ARC(board)
    arc.SetNet(vcc)
    arc.SetLayer(pcbnew.F_Cu)
    p = geo.point(5 * MM, 5 * MM)
    arc.SetStart(p)
    arc.SetMid(p)
    arc.SetEnd(p)  # all three points identical
    arc.SetWidth(200_000)
    board.Add(arc)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    assert placed > 0


def test_zero_size_pad_does_not_crash():
    board, net = _board(layers=2, size=10 * MM)
    fp = pcbnew.FOOTPRINT(board)
    pad = pcbnew.PAD(fp)
    pad.SetSize(geo.size(0, 0))
    pad.SetPosition(geo.point(5 * MM, 5 * MM))
    pad.SetLayerSet(fixtures.layer_set(pcbnew.F_Cu))
    fp.Add(pad)
    board.Add(fp)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    assert placed > 0


def test_footprint_with_no_pads_does_not_crash():
    board, net = _board(layers=2, size=10 * MM)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetPosition(geo.point(5 * MM, 5 * MM))
    board.Add(fp)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0, avoid_footprints=True)
    assert placed > 0


# ---- input validation (dialog values(), not stitch() itself) --------------

def test_dialog_rejects_drill_bigger_than_diameter():
    board, net = _board(layers=2)
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    dlg.via_dia.SetValue("0.3")
    dlg.drill.SetValue("0.6")
    try:
        dlg.values()
        raise AssertionError("expected ValueError for drill >= diameter")
    except ValueError as e:
        assert "smaller than the via diameter" in str(e)
    dlg.Destroy()
    vsl._clear_settings()


def test_dialog_rejects_zero_and_negative_values():
    board, net = _board(layers=2)
    vsl._clear_settings()
    for bad in ("0", "-1", "-0.5"):
        dlg = vsl.ViaStitchingDialogLegacy(None, board)
        dlg.via_dia.SetValue(bad)
        try:
            dlg.values()
            raise AssertionError(f"expected ValueError for via_dia={bad!r}")
        except ValueError as e:
            assert "greater than zero" in str(e)
        dlg.Destroy()
    vsl._clear_settings()


def test_dialog_rejects_non_numeric_input():
    board, net = _board(layers=2)
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    dlg.spacing.SetValue("not a number")
    try:
        dlg.values()
        raise AssertionError("expected ValueError for non-numeric spacing")
    except ValueError as e:
        assert "numbers" in str(e)
    dlg.Destroy()
    vsl._clear_settings()


def test_dialog_rejects_empty_net():
    board, net = _board(layers=2)
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    dlg.net.SetValue("   ")
    try:
        dlg.values()
        raise AssertionError("expected ValueError for blank net")
    except ValueError as e:
        assert "net" in str(e).lower()
    dlg.Destroy()
    vsl._clear_settings()


# ---- known, documented (not "fixed") behavior ------------------------------

def test_stitch_itself_does_not_validate_drill_vs_diameter():
    """stitch() trusts its caller for size sanity, same as the IPC version --
    validation is the dialog's job (values()), not stitch()'s. Documented
    here so this stays a deliberate design choice, not an accidental gap."""
    board, net = _board(layers=2)
    placed, _ = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                            via_dia_mm=0.3, drill_mm=0.6, spacing_mm=2.0, pattern="Square",
                            x_offset_mm=0, y_offset_mm=0)
    vias = [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    assert placed > 0
    assert any(v.GetDrillValue() > v.GetWidth() for v in vias)  # confirmed: silently accepted


# ---- grouping / deletion robustness ----------------------------------------

def test_delete_grouped_vias_on_a_large_run_leaves_nothing():
    board, net = _board(layers=2, size=50 * MM)
    placed, grouped = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                                  via_dia_mm=0.4, drill_mm=0.2, spacing_mm=1.0, pattern="Square",
                                  x_offset_mm=0, y_offset_mm=0)
    assert placed > 500 and grouped
    vias = [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    group = vias[0].GetParentGroup()
    geo.delete_grouped_vias(board, group, vias)
    assert not any(isinstance(t, pcbnew.PCB_VIA) for t in board.GetTracks())
    assert len(list(board.Groups())) == 0


def test_delete_grouped_vias_survives_a_manually_removed_member():
    """If one via in a group was already removed by something else (e.g. the
    user manually deleted just that one), re-running the plugin's own
    cleanup on the original list must not crash on the already-gone item."""
    board, net = _board(layers=2)
    placed, grouped = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                                  via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
                                  x_offset_mm=0, y_offset_mm=0)
    assert placed > 1
    vias = [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    group = vias[0].GetParentGroup()
    board.Remove(vias[0])  # simulate an out-of-band removal
    geo.delete_grouped_vias(board, group, vias)  # still passes the FULL original list
    assert not any(isinstance(t, pcbnew.PCB_VIA) for t in board.GetTracks())


def test_save_reload_after_delete_has_zero_vias():
    """The actual real-world check: place, delete via the plugin's own safe
    path, save to a real file, reload, and confirm zero vias/groups survive
    -- the same check that would have caught (or rather, confirmed absent)
    the ghost-via issue seen in a live KiCad 6 session, since that turned
    out to be a pure in-memory artifact never written to disk."""
    import tempfile, os

    board, net = _board(layers=2)
    placed, grouped = vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
                                  via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
                                  x_offset_mm=0, y_offset_mm=0)
    vias = [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    group = vias[0].GetParentGroup()
    geo.delete_grouped_vias(board, group, vias)

    path = os.path.join(tempfile.gettempdir(), "via_stitching_edge_case_test.kicad_pcb")
    pcbnew.SaveBoard(path, board)
    reloaded = pcbnew.LoadBoard(path)
    assert not any(isinstance(t, pcbnew.PCB_VIA) for t in reloaded.GetTracks())
    assert len(list(reloaded.Groups())) == 0
    os.remove(path)


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
