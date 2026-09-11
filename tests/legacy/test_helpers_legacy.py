# The other half of the coverage gap: the helper-level tests the IPC suite has
# had all along (grid maths, the nudge, the blocked predicate, keepout sizing,
# settings persistence, config readers), run against the legacy backend's own
# copies of those helpers.
#
# They are separate implementations, not shared code, so "the IPC one is
# tested" says nothing about these. Everything board-touching runs against a
# real KiCad 6.0.11 board, like the rest of tests/legacy.
#
# Run with KiCad's own bundled interpreter:
#   "C:/Program Files/KiCad/6.0/bin/python.exe" tests/legacy/test_helpers_legacy.py

import math
import os
import sys
import tempfile

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")

import pcbnew
import wx

wx.DisableAsserts()  # see test_edge_cases_legacy.py for why

import _geometry_legacy as geo  # noqa: E402
import _fixtures as fixtures
import via_stitching_action_legacy as vsl  # noqa: E402

_APP = wx.App()

MM = 1_000_000
BOX = (0, 0, 10 * MM, 10 * MM)


def test_copper_layer_order_is_stackup_order_not_id_order():
    """KiCad 9 renumbered copper: B_Cu became 2 and the inner layers start at
    4, so ordering by layer id puts the back layer above every inner one. Every
    via span, the outer-layer test behind the advisory, and the dialog's own
    layer list are all built from this order, so getting it wrong silently
    stitches the wrong layers rather than failing."""
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    order = geo.copper_layer_order(board)
    names = [board.GetLayerName(l) for l in order]
    assert names == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], names

    outer_span = [board.GetLayerName(l) for l in geo.span_layers(board, order[0], order[-1])]
    assert outer_span == names, outer_span
    # A span stopping at an inner layer must not swallow the back layer.
    inner_span = [board.GetLayerName(l) for l in geo.span_layers(board, order[0], order[1])]
    assert inner_span == ["F.Cu", "In1.Cu"], inner_span


def test_square_grid():
    pts = list(vsl._grid_points(BOX, 2 * MM, "Square"))
    assert len(pts) == 36, len(pts)  # 6 x 6, both edges included
    assert all(x % (2 * MM) == 0 and y % (2 * MM) == 0 for x, y in pts)
    assert len(set(pts)) == len(pts), "duplicate grid points"


def test_hexagonal_grid_row_pitch_and_stagger():
    spacing = 2 * MM
    pts = list(vsl._grid_points(BOX, spacing, "Hexagonal"))
    rows = sorted({y for _, y in pts})
    # round(), not int(): truncating biases the row pitch short, and over a
    # full board that walks the last row off the pour.
    assert rows[1] - rows[0] == round(spacing * math.sqrt(3) / 2)
    # Alternate rows are offset by half the spacing, which is what makes it
    # hexagonal rather than a rectangular grid with a tighter row pitch.
    first = sorted(x for x, y in pts if y == rows[0])
    second = sorted(x for x, y in pts if y == rows[1])
    assert first[0] != second[0]
    assert abs((second[0] - first[0]) - spacing // 2) <= 1


def test_staggered_grid_is_not_the_square_grid():
    square = set(vsl._grid_points(BOX, 2 * MM, "Square"))
    staggered = set(vsl._grid_points(BOX, 2 * MM, "Staggered"))
    assert staggered and staggered != square


def test_zero_spacing_terminates():
    # A zero or negative spacing must not spin forever building an infinite
    # grid: it yields nothing and lets the caller fail with its own message.
    assert list(vsl._grid_points(BOX, 0, "Square")) == []
    assert list(vsl._grid_points(BOX, -1, "Square")) == []


def test_blocked_predicate_without_shapes_blocks_nothing():
    blocked = vsl._blocked_predicate([])
    assert not blocked(0, 0)
    assert not blocked(5 * MM, 5 * MM)


def test_nudge_radius_keeps_hole_margin_between_neighbours():
    # The nudge may never walk a via so far that its hole ends up closer to the
    # neighbouring grid position than the hole-to-hole margin allows.
    spacing, drill = 2 * MM, 300_000
    r = vsl._nudge_radius(spacing, drill)
    assert r > 0
    assert 2 * r + drill + pcbnew.FromMM(vsl.HOLE_MARGIN_MM) <= spacing


def test_nudged_finds_a_spot_beside_a_keepout():
    from shapely.geometry import Point, box
    from shapely.prepared import prep

    region = prep(box(0, 0, 10 * MM, 10 * MM))
    allowed = lambda pt: region.contains(pt)  # noqa: E731
    here = (5 * MM, 5 * MM)
    nudge_r = vsl._nudge_radius(2 * MM, 300_000)

    small = vsl._blocked_predicate([Point(*here).buffer(200_000)])
    moved = vsl._nudged(here[0], here[1], nudge_r, allowed, small)
    assert moved is not None
    assert allowed(Point(*moved)) and not small(*moved)

    # A keepout swallowing the whole ring drops the candidate instead of
    # walking it across the board.
    big = vsl._blocked_predicate([Point(*here).buffer(2 * MM)])
    assert vsl._nudged(here[0], here[1], nudge_r, allowed, big) is None

    # No slack in the grid: nothing moves.
    assert vsl._nudged(here[0], here[1], 0, allowed, small) is None


def test_via_keepout_is_sized_off_the_drill_not_the_copper():
    # Sizing hole-to-hole clearance off the annular ring instead of the drill
    # comes out far too generous, and quietly thins the array.
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    via = geo.make_via(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu,
                       2 * MM, 300_000, 0, 0, 0)  # big copper, small drill
    board.Add(via)

    span = set(range(pcbnew.F_Cu, pcbnew.B_Cu + 1))
    margin = pcbnew.FromMM(vsl.HOLE_MARGIN_MM)
    shapes = geo.via_keepout_shapes(board, 150_000, span, margin)
    assert len(shapes) == 1
    minx, miny, maxx, maxy = shapes[0].bounds
    expected = 150_000 + 150_000 + margin  # via radius + drill radius + margin
    assert abs(maxx - expected) < 10_000, (maxx, expected)


def test_footprint_keepout_covers_the_whole_bounding_box():
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(gnd)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetPosition(geo.point(5 * MM, 5 * MM))
    body = fixtures.fp_shape(fp)
    body.SetLayer(pcbnew.F_SilkS)
    body.SetShape(fixtures.rectangle())
    body.SetStart(geo.point(4 * MM, 4 * MM))
    body.SetEnd(geo.point(6 * MM, 6 * MM))
    fp.Add(body)
    board.Add(fp)

    clearances = geo.net_clearances(board, "GND")
    shapes = geo.footprint_keepout_shapes(board, "GND", 300_000, clearances)
    assert shapes
    blocked = vsl._blocked_predicate(shapes)
    assert blocked(5 * MM, 5 * MM), "the middle of the footprint must be blocked"
    assert blocked(4 * MM, 6 * MM), "and so must its corners"
    assert not blocked(0, 0)


def test_settings_persist_across_dialogs_and_reset_clears_them():
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    board.Add(pcbnew.NETINFO_ITEM(board, "GND"))

    # Never the real settings file: this writes and deletes it.
    tmp = os.path.join(tempfile.mkdtemp(), "via_stitching_settings_legacy.json")
    original_path = vsl._settings_path
    vsl._settings_path = lambda: tmp
    try:
        dlg = vsl.ViaStitchingDialogLegacy(None, board)
        try:
            dlg.spacing.SetValue("3.75")
            dlg.net.SetValue("GND")
            dlg.save_current_as_settings()
        finally:
            dlg.Destroy()
        assert os.path.exists(tmp)

        again = vsl.ViaStitchingDialogLegacy(None, board)
        try:
            assert again.spacing.GetValue() == "3.75", "the saved spacing did not come back"
            again._on_reset()
            assert again.spacing.GetValue() == str(vsl.DEFAULT_SPACING_MM)
            assert not os.path.exists(tmp), "Reset settings must delete the saved file"
        finally:
            again.Destroy()
    finally:
        vsl._settings_path = original_path


def test_kicad_config_readers_do_not_raise():
    from _kicad_config_legacy import kicad_config_dirs

    dirs = kicad_config_dirs()
    assert isinstance(dirs, list)
    assert all(isinstance(d, str) for d in dirs)
    # Colors come from whatever theme this machine has, or from the built-in
    # fallbacks. Either way it answers with layers, and never raises.
    colors = vsl._layer_colors()
    assert isinstance(colors, dict)
    for rgb in colors.values():
        assert len(rgb) == 3 and all(0 <= c <= 255 for c in rgb)


def test_translation_catalogs_cover_every_wrapped_string():
    """Every _() call's literal has to exist in all three catalogs, or a user
    running KiCad in Dutch sees a half-translated dialog. The catalogs are
    shared with the IPC build, so this also catches wording drifting apart
    between the two backends."""
    import ast
    import json

    import _i18n_legacy

    plugin_source = os.path.join(
        os.path.dirname(_i18n_legacy.__file__), "via_stitching_action_legacy.py"
    )
    with open(plugin_source, encoding="utf-8") as fh:
        source = fh.read()
    wrapped = [
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
        and node.args and isinstance(node.args[0], ast.Constant)
    ]
    assert len(wrapped) > 40, "expected the dialog and messages to be wrapped"

    for code in ("nl", "de", "fr"):
        catalog = _i18n_legacy._catalog(code)
        assert catalog, "no {} catalog found".format(code)
        missing = sorted(set(text for text in wrapped if text not in catalog))
        assert not missing, "{}: {} strings missing: {}".format(code, len(missing), missing[:3])


def test_translation_falls_back_to_the_source_text():
    import _i18n_legacy

    original = _i18n_legacy._active_catalog
    try:
        _i18n_legacy._active_catalog = {"Via Stitching": "Via Stikken"}
        assert _i18n_legacy._("Via Stitching") == "Via Stikken"
        # Anything the catalog does not carry comes back unchanged, which is
        # what makes English work without a catalog of its own.
        assert _i18n_legacy._("not in any catalog") == "not in any catalog"
    finally:
        _i18n_legacy._active_catalog = original


def test_error_dialog_carries_the_traceback():
    """The point of the dialog over a message box is that the text can be
    selected and copied into a bug report, so the traceback has to be in a
    text control rather than a label."""
    details = chr(10).join(["Traceback (most recent call last):",
                            "  File nowhere, line 1", "Boom: it broke"])
    dlg = vsl.ErrorDialog(None, "Via Stitching hit an unexpected error.", details)
    try:
        texts = [c for c in dlg.GetChildren() if isinstance(c, wx.TextCtrl)]
        assert len(texts) == 1, texts
        assert texts[0].GetValue() == details
        assert not texts[0].IsEditable(), "read-only, but still selectable"
        assert dlg.GetTitle()
    finally:
        dlg.Destroy()


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print()
    print(f"{len(tests)}/{len(tests)} PASSED")


if __name__ == "__main__":
    run()
