# End-to-end coverage for the IPC stitch(), the mirror of
# tests/legacy/test_stitch_legacy_coverage.py. test_geometry.py drives one
# realistic run and then tests the helpers in isolation; this file covers the
# per-feature paths through stitch() itself that nothing else reaches: via
# types and their spans, every pattern, the VIA_COUNT_WARN prompt, the two
# avoid_* toggles that need board content to show any effect, grouping, and
# the copper check that only a through via is exempt from.
#
# Offline, like the rest of tests/: the fake board from test_geometry.py is
# reused and extended rather than duplicated.
#
# Run with the plugin's own venv interpreter, which already has wx, kipy and
# shapely:
#   "$LOCALAPPDATA/KiCad/10.0/python-environments/com.github.jOaSbA.via-stitching/Scripts/python" tests/ipc/test_stitch_coverage.py
#
# License: GPL-3.0-or-later

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugins"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wx  # noqa: E402
from kipy.board_types import BoardLayer, ViaType  # noqa: E402
from kipy.util import from_mm  # noqa: E402

import via_stitching_action as vs  # noqa: E402
from via_stitching_action import stitch  # noqa: E402

from test_geometry import _fake_board  # noqa: E402

MM = from_mm(1.0)

LAYER_NAMES = {
    BoardLayer.BL_F_Cu: "F.Cu",
    BoardLayer.BL_In1_Cu: "In1.Cu",
    BoardLayer.BL_In2_Cu: "In2.Cu",
    BoardLayer.BL_B_Cu: "B.Cu",
}


def _pour(x0, y0, x1, y1):
    def node(x, y):
        return SimpleNamespace(has_point=True, has_arc=False, point=SimpleNamespace(x=x, y=y))

    outline = SimpleNamespace(
        nodes=[node(x0, y0), node(x1, y0), node(x1, y1), node(x0, y1)]
    )
    return [SimpleNamespace(outline=outline, holes=[])]


def _board(inner_layer=False, **kw):
    """_fake_board plus the bits stitch() only touches on some paths: an
    optional inner copper layer poured on the stitched net, other nets' zones,
    and footprints with bounding boxes."""
    board = _fake_board(**kw)
    zones = list(board.get_zones())
    if inner_layer:
        zones[0].filled_polygons[BoardLayer.BL_In1_Cu] = zones[0].filled_polygons[BoardLayer.BL_F_Cu]
        layers = [BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu, BoardLayer.BL_B_Cu]
    else:
        layers = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]
    board.get_enabled_layers = lambda: layers
    board.get_layer_name = lambda l: LAYER_NAMES[l]
    board.get_zones = lambda: zones
    board.get_footprints = lambda: []
    board.get_item_bounding_box = lambda items: []
    return board, zones


def _add_other_net_zone(zones, x0, y0, x1, y1, net="SIG"):
    zones.append(SimpleNamespace(
        net=SimpleNamespace(name=net),
        is_rule_area=lambda: False,
        filled_polygons={BoardLayer.BL_F_Cu: _pour(x0, y0, x1, y1)},
    ))


def _add_footprint(board, x0, y0, w, h):
    box = SimpleNamespace(pos=SimpleNamespace(x=x0, y=y0), size=SimpleNamespace(x=w, y=h))
    board.get_footprints = lambda: ["fp"]
    board.get_item_bounding_box = lambda items: [box]


def _run(board, via_type=ViaType.VT_THROUGH, start=BoardLayer.BL_F_Cu,
         end=BoardLayer.BL_B_Cu, dia=0.6, drill=0.3, spacing=2.0,
         pattern="Square", **kw):
    return stitch(board, via_type, start, end, "GND", dia, drill, spacing,
                  pattern, 0.0, 0.0, **kw)


def _vias(board):
    return [i for i in board.placed if hasattr(i, "padstack")]


def test_via_types_keep_the_span_they_were_asked_for():
    cases = [
        (ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, False),
        (ViaType.VT_MICRO, BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu, True),
        (ViaType.VT_BLIND_BURIED, BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu, True),
    ]
    for via_type, start, end, inner in cases:
        board, _ = _board(inner_layer=inner)
        count, _grouped = _run(board, via_type, start, end, dia=0.25, drill=0.1, spacing=1.0)
        vias = _vias(board)
        assert count > 0 and len(vias) == count
        assert all(v.type == via_type for v in vias), via_type
        assert all(v.padstack.drill.start_layer == start for v in vias), via_type
        assert all(v.padstack.drill.end_layer == end for v in vias), via_type


def test_a_through_via_needs_two_poured_layers_not_both_ends():
    # The barrel crosses every layer, so a through via can stitch any two
    # layers the net is poured on. Here the back is unpoured.
    board, zones = _board(inner_layer=True)
    del zones[0].filled_polygons[BoardLayer.BL_B_Cu]
    count, _grouped = _run(board)
    assert count > 0
    assert all(v.padstack.drill.start_layer == BoardLayer.BL_F_Cu for v in _vias(board))
    assert all(v.padstack.drill.end_layer == BoardLayer.BL_B_Cu for v in _vias(board))


def test_a_through_via_stitches_two_inner_planes():
    # Neither outer layer poured at all, which is the usual four-layer board
    # with its planes on the inside.
    board, zones = _board(inner_layer=True)
    fills = zones[0].filled_polygons
    fills[BoardLayer.BL_In2_Cu] = fills[BoardLayer.BL_In1_Cu]
    del fills[BoardLayer.BL_F_Cu], fills[BoardLayer.BL_B_Cu]
    board.get_enabled_layers = lambda: [
        BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu, BoardLayer.BL_In2_Cu, BoardLayer.BL_B_Cu,
    ]
    count, _grouped = _run(board)
    assert count > 0


def test_a_through_via_on_one_poured_layer_fails():
    # One poured layer gives the via nothing to stitch it to, so every via
    # would be dangling. The error names the unpoured ends.
    for inner_layer, unpoured, named in (
        (False, [BoardLayer.BL_B_Cu], ["B.Cu"]),
        (True, [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu], ["F.Cu", "B.Cu"]),
    ):
        board, zones = _board(inner_layer=inner_layer)
        for layer in unpoured:
            del zones[0].filled_polygons[layer]
        try:
            _run(board)
        except RuntimeError as exc:
            assert all(n in str(exc) for n in named), exc
        else:
            raise AssertionError(f"one poured layer should have failed ({named})")


def test_only_through_vias_are_exempt_from_the_copper_check():
    # Micro and blind/buried vias end inside the board, so both of their ends
    # have to land on the net's copper.
    for via_type in (ViaType.VT_MICRO, ViaType.VT_BLIND_BURIED):
        board, zones = _board(inner_layer=True)
        del zones[0].filled_polygons[BoardLayer.BL_In1_Cu]
        try:
            _run(board, via_type, BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu)
        except RuntimeError as exc:
            assert "In1.Cu" in str(exc), exc
        else:
            raise AssertionError(f"{via_type} must still land on copper at both ends")


def test_all_patterns_place_something():
    counts = {}
    for pattern in vs.PATTERNS:
        board, _ = _board()
        count, _grouped = _run(board, pattern=pattern)
        assert count > 0, pattern
        counts[pattern] = count
    assert len(set(counts.values())) > 1, f"every pattern placed the same count: {counts}"


def test_via_count_warn_prompt_is_obeyed():
    original_threshold = vs.VIA_COUNT_WARN
    original_msgbox = wx.MessageBox
    asked = []
    try:
        vs.VIA_COUNT_WARN = 10

        wx.MessageBox = lambda *a, **k: asked.append(a[0]) or wx.NO
        board, _ = _board()
        assert _run(board) == (0, False), "answering No must place nothing"
        assert board.placed == []
        assert asked and "10" not in asked[0], "the prompt names the via count, not the threshold"

        wx.MessageBox = lambda *a, **k: wx.YES
        board, _ = _board()
        count, _grouped = _run(board)
        assert count > vs.VIA_COUNT_WARN
    finally:
        vs.VIA_COUNT_WARN = original_threshold
        wx.MessageBox = original_msgbox


def test_avoid_other_zones_toggle():
    # A SIG pour over the middle of the board. Off by default, so it only costs
    # vias once the toggle is on.
    board, zones = _board()
    _add_other_net_zone(zones, 5 * MM, 5 * MM, 15 * MM, 15 * MM)
    off, _g = _run(board)

    board, zones = _board()
    _add_other_net_zone(zones, 5 * MM, 5 * MM, 15 * MM, 15 * MM)
    on, _g = _run(board, avoid_other_zones=True)

    assert on < off, f"avoid_other_zones changed nothing ({on} vs {off})"


def test_avoid_footprints_toggle():
    board, _ = _board()
    _add_footprint(board, 5 * MM, 5 * MM, 10 * MM, 10 * MM)
    off, _g = _run(board)

    board, _ = _board()
    _add_footprint(board, 5 * MM, 5 * MM, 10 * MM, 10 * MM)
    on, _g = _run(board, avoid_footprints=True)

    assert on < off, f"avoid_footprints changed nothing ({on} vs {off})"


def test_the_run_is_grouped_under_one_readable_name():
    # Grouping is what lets a whole run be selected and deleted as a set, so the
    # group has to actually be created, and named after what it holds.
    board, _ = _board()
    count, grouped = _run(board)
    assert grouped
    groups = [i for i in board.placed if hasattr(i, "proto") and not hasattr(i, "padstack")]
    assert len(groups) == 1, groups
    # Layer names, not the raw BoardLayer enum values this used to use: the
    # name is what "Reset last run" puts in front of the user, and the SWIG
    # backend names the same group the same way.
    assert groups[0].proto.name == "ViaStitching GND F.Cu:B.Cu"
    assert len(groups[0].items) == count


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
