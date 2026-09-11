# Edge cases for the IPC plugin: cloning a preselected via, main()'s error
# paths, and degenerate board content. The mirror of
# tests/legacy/test_edge_cases_legacy*.py, which covered all three on the
# legacy backend while the IPC side only had the happy path plus helper unit
# tests.
#
# Nothing here needs a running KiCad: the board, the connection and the dialog
# are all faked, and the fake board from test_geometry.py is reused rather than
# duplicated.
#
# Run with the plugin's own venv interpreter, which already has wx, kipy and
# shapely:
#   "$LOCALAPPDATA/KiCad/10.0/python-environments/com.github.jOaSbA.via-stitching/Scripts/python" tests/ipc/test_edge_cases.py
#
# License: GPL-3.0-or-later

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugins"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wx  # noqa: E402
from kipy.board_types import BoardLayer, PadType, ViaType  # noqa: E402
from kipy.errors import ConnectionError as KiCadConnectionError  # noqa: E402
from kipy.util import from_mm  # noqa: E402

import via_stitching_action as vs  # noqa: E402
from via_stitching_action import (  # noqa: E402
    _fallback_clearances,
    _footprint_keepout_shapes,
    _pad_copper_keepout_shapes,
    _track_keepout_shapes,
    stitch,
)

from test_geometry import _fake_board, _smd_pad  # noqa: E402
from test_stitch_coverage import _board, _pour  # noqa: E402

_APP = wx.App()

# The settings file these touch is the user's real one. Stub both directions
# rather than reading or deleting it from a test run.
vs._load_settings = lambda: {}
vs._clear_settings = lambda: None

MM = from_mm(1.0)

LAYERS = {
    BoardLayer.BL_F_Cu: "F.Cu",
    BoardLayer.BL_In1_Cu: "In1.Cu",
    BoardLayer.BL_In2_Cu: "In2.Cu",
    BoardLayer.BL_B_Cu: "B.Cu",
}

FULL_SPAN = [BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu, BoardLayer.BL_B_Cu]


# --- cloning a preselected via ------------------------------------------------

def _selected_via(via_type=ViaType.VT_MICRO, start=BoardLayer.BL_F_Cu,
                  end=BoardLayer.BL_In1_Cu, net="VCC", dia_mm=0.25, drill_mm=0.1):
    return SimpleNamespace(
        type=via_type,
        net=SimpleNamespace(name=net),
        diameter=from_mm(dia_mm),
        drill_diameter=from_mm(drill_mm),
        padstack=SimpleNamespace(
            drill=SimpleNamespace(start_layer=start, end_layer=end)
        ),
    )


def _dialog(selection=()):
    board = SimpleNamespace(
        get_enabled_layers=lambda: list(LAYERS.keys()),
        get_layer_name=lambda l: LAYERS[l],
        get_selection=lambda kind: list(selection),
    )
    return vs.ViaStitchingDialog(None, ["GND", "VCC"], board)


def test_clone_from_selected_via_fills_the_dialog():
    dlg = _dialog([_selected_via()])
    try:
        values = dlg.values()
        assert values["via_type"] == ViaType.VT_MICRO
        assert values["start_layer"] == BoardLayer.BL_F_Cu
        assert values["end_layer"] == BoardLayer.BL_In1_Cu
        assert values["net_name"] == "VCC"
        assert (values["via_dia_mm"], values["drill_mm"]) == (0.25, 0.1)
        # Spacing is derived from the cloned diameter, not left at the default.
        assert values["spacing_mm"] == 1.0
    finally:
        dlg.Destroy()


def test_clone_from_through_via_locks_the_layer_combos():
    # A through via always crosses the whole board, so its own recorded span is
    # not something to offer as a choice: both combos collapse to one entry.
    via = _selected_via(ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu)
    dlg = _dialog([via])
    try:
        assert dlg.via_type.GetStringSelection() == "Through"
        assert dlg.start_layer.GetCount() == 1
        assert dlg.end_layer.GetCount() == 1
        values = dlg.values()
        assert values["start_layer"] != values["end_layer"]
    finally:
        dlg.Destroy()


def test_clone_from_via_with_no_net_is_caught_by_values_not_a_crash():
    # A via can sit on the board with no net. Cloning it must still build a
    # dialog, and the empty net has to fail as a worded ValueError rather than
    # reaching stitch() and dying there.
    dlg = _dialog([_selected_via(net="")])
    try:
        try:
            dlg.values()
        except ValueError as exc:
            assert "net" in str(exc).lower(), exc
        else:
            raise AssertionError("an empty net should have been rejected")
    finally:
        dlg.Destroy()


def test_reset_overrides_a_cloned_via_selection():
    dlg = _dialog([_selected_via()])
    try:
        assert dlg.values()["via_type"] == ViaType.VT_MICRO
        dlg._on_reset()
        values = dlg.values()
        assert values["via_type"] == ViaType.VT_THROUGH
        assert values["via_dia_mm"] == vs.DEFAULT_VIA_DIAMETER_MM
        assert values["drill_mm"] == vs.DEFAULT_DRILL_MM
        assert values["spacing_mm"] == vs.DEFAULT_SPACING_MM
        assert values["net_name"] == vs.DEFAULT_NET
    finally:
        dlg.Destroy()


def test_hand_typed_size_survives_a_via_type_switch_but_a_default_does_not():
    # Picking Micro swaps in microvia-sized defaults, which must not overwrite a
    # value the user typed or one cloned off a real via.
    dlg = _dialog()
    try:
        assert dlg.via_dia.GetValue() == str(vs.DEFAULT_VIA_DIAMETER_MM)
        dlg.via_type.SetStringSelection("Micro")
        dlg._on_via_type()
        assert dlg.via_dia.GetValue() == str(vs.DEFAULT_MICROVIA_DIAMETER_MM)
    finally:
        dlg.Destroy()

    dlg = _dialog()
    try:
        dlg.via_dia.SetValue("0.45")
        dlg.via_type.SetStringSelection("Micro")
        dlg._on_via_type()
        assert dlg.via_dia.GetValue() == "0.45", "a hand-typed diameter was overwritten"
    finally:
        dlg.Destroy()


# --- main()'s paths -----------------------------------------------------------

class _FakeDialog:
    """Stands in for ViaStitchingDialog inside main(). Records what main() did
    with it rather than putting a real window on screen."""

    def __init__(self, modal=wx.ID_OK, values=None, error=None):
        self._modal = modal
        self._values = values or dict(
            via_type=ViaType.VT_THROUGH,
            start_layer=BoardLayer.BL_F_Cu, end_layer=BoardLayer.BL_B_Cu,
            net_name="GND", via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0,
            pattern="Square", x_offset_mm=0.0, y_offset_mm=0.0,
            avoid_other_zones=False, avoid_footprints=False, avoid_same_net_pads=False,
        )
        self._error = error
        self.destroyed = False

    def ShowModal(self):
        return self._modal

    def values(self):
        if self._error:
            raise self._error
        return dict(self._values)

    def _settings_from_values(self, values):
        return {}

    def CentreOnScreen(self):
        pass

    def Destroy(self):
        self.destroyed = True


def _run_main(dialog=None, stitch_result=(12, True), stitch_error=None, connect_error=None):
    """Drive main() with everything outside it faked. Returns what it reported."""
    reported = SimpleNamespace(messages=[], failures=[], stitched=[], dialog=dialog)

    def fake_stitch(board, **kw):
        reported.stitched.append(kw)
        if stitch_error:
            raise stitch_error
        return stitch_result

    def fake_kicad(*a, **kw):
        if connect_error:
            raise connect_error
        return SimpleNamespace(
            get_board=lambda: _fake_board()[0] if isinstance(_fake_board(), tuple) else _fake_board(),
            get_version=lambda: "10.0.0",
        )

    # main() builds its own wx.App as a local, which is destroyed the moment
    # main() returns and takes every later dialog in this run down with it
    # (PyNoAppError). Hand it the one this module already owns.
    original_app = wx.App
    wx.App = lambda *a, **kw: _APP

    originals = {name: getattr(vs, name) for name in (
        "KiCad", "ViaStitchingDialog", "stitch", "_msg", "_report", "_save_settings",
        "make_tool_window", "attach_to_stage_manager", "prepare_app",
    )}
    try:
        vs.KiCad = fake_kicad
        vs.ViaStitchingDialog = lambda *a, **kw: dialog
        vs.stitch = fake_stitch
        vs._msg = lambda parent, text, style=0: reported.messages.append(text)
        vs._report = lambda parent, text, exc=None: reported.failures.append((text, exc))
        vs._save_settings = lambda values: None
        vs.make_tool_window = lambda dlg: None
        vs.attach_to_stage_manager = lambda dlg: None
        vs.prepare_app = lambda: None
        vs.main()
    finally:
        wx.App = original_app
        for name, original in originals.items():
            setattr(vs, name, original)
    return reported


def test_main_does_nothing_when_the_dialog_is_cancelled():
    dlg = _FakeDialog(modal=wx.ID_CANCEL)
    out = _run_main(dlg)
    assert out.stitched == []
    assert out.messages == [] and out.failures == []
    assert dlg.destroyed, "the dialog must be destroyed even on cancel"


def test_main_shows_a_dialog_value_error_as_a_message():
    out = _run_main(_FakeDialog(error=ValueError("Pick a net to stitch.")))
    assert out.stitched == []
    assert out.messages == ["Pick a net to stitch."]
    assert out.failures == []


def test_main_shows_a_stitch_runtime_error_as_a_message_not_a_crash():
    out = _run_main(_FakeDialog(), stitch_error=RuntimeError("No room for vias."))
    assert out.messages == ["No room for vias."]
    assert out.failures == []


def test_main_surfaces_an_unexpected_exception_without_raising():
    out = _run_main(_FakeDialog(), stitch_error=TypeError("something in kipy moved"))
    assert out.messages == []
    assert len(out.failures) == 1
    text, exc = out.failures[0]
    assert isinstance(exc, TypeError)


def test_main_reports_the_via_count_and_flags_a_failed_grouping():
    out = _run_main(_FakeDialog(), stitch_result=(37, True))
    assert len(out.messages) == 1 and "37" in out.messages[0]
    assert "group" not in out.messages[0].lower()

    out = _run_main(_FakeDialog(), stitch_result=(37, False))
    assert "group" in out.messages[0].lower(), "a failed grouping must be said out loud"


def test_main_explains_a_refused_connection_instead_of_opening_a_dialog():
    out = _run_main(_FakeDialog(), connect_error=KiCadConnectionError("dial failed"))
    assert out.stitched == []
    assert len(out.failures) == 1
    assert out.messages == []


# --- degenerate board content -------------------------------------------------

def test_zero_length_track_does_not_crash():
    track = SimpleNamespace(
        net=SimpleNamespace(name="SIG"), layer=BoardLayer.BL_F_Cu,
        start=SimpleNamespace(x=MM, y=MM), end=SimpleNamespace(x=MM, y=MM),
        width=from_mm(0.2),
    )
    board = SimpleNamespace(get_tracks=lambda: [track])
    shapes = _track_keepout_shapes(board, "GND", from_mm(0.3), _fallback_clearances(), FULL_SPAN)
    # A zero-length track is still a real pad-sized blob of copper, so it has to
    # produce a keepout rather than an empty or invalid geometry.
    assert len(shapes) == 1 and shapes[0].area > 0


def test_zero_size_pad_does_not_crash():
    pad = _smd_pad("SIG", 0, 0)
    pad.position = SimpleNamespace(x=MM, y=MM)
    board = SimpleNamespace(get_pads=lambda: [pad])
    shapes = _pad_copper_keepout_shapes(board, "GND", from_mm(0.3), _fallback_clearances(), FULL_SPAN)
    assert all(s.is_valid for s in shapes)


def test_pad_whose_layers_cannot_be_read_is_treated_as_blocking():
    # "Unknown layers" must fail safe: the pad blocks, rather than being
    # silently ignored because the padstack could not be read.
    pad = _smd_pad("SIG", from_mm(1.0), from_mm(1.0))
    pad.position = SimpleNamespace(x=MM, y=MM)
    pad.padstack = SimpleNamespace(
        layers=property(lambda self: (_ for _ in ()).throw(RuntimeError("no layers"))),
        copper_layers=pad.padstack.copper_layers,
    )
    board = SimpleNamespace(get_pads=lambda: [pad])
    shapes = _pad_copper_keepout_shapes(board, "GND", from_mm(0.3), _fallback_clearances(), FULL_SPAN)
    assert len(shapes) == 1


def test_footprint_with_a_zero_size_bounding_box_does_not_crash():
    box = SimpleNamespace(pos=SimpleNamespace(x=MM, y=MM), size=SimpleNamespace(x=0, y=0))
    board = SimpleNamespace(
        get_footprints=lambda: ["fp"], get_item_bounding_box=lambda items: [box]
    )
    shapes = _footprint_keepout_shapes(board, "GND", from_mm(0.3), _fallback_clearances())
    assert len(shapes) == 1 and shapes[0].area > 0  # grown by the clearance margin


def test_zone_outline_with_an_arc_node_is_still_a_polygon():
    # Arc nodes are rare in fills but legal. They are approximated by their
    # start/mid/end points, so a zone carrying one must still stitch.
    board, zones = _board()
    arc = SimpleNamespace(
        has_point=False, has_arc=True,
        arc=SimpleNamespace(
            start=SimpleNamespace(x=0, y=0),
            mid=SimpleNamespace(x=10 * MM, y=-MM),
            end=SimpleNamespace(x=20 * MM, y=0),
        ),
    )
    outline = zones[0].filled_polygons[BoardLayer.BL_F_Cu][0].outline
    outline.nodes = [arc] + outline.nodes[1:]
    count, _grouped = stitch(
        board, ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, "GND",
        0.6, 0.3, 2.0, "Square", 0.0, 0.0,
    )
    assert count > 0


def test_net_that_is_never_poured_fails_with_a_worded_error():
    board, _zones = _board()
    try:
        stitch(board, ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, "SIG",
               0.6, 0.3, 2.0, "Square", 0.0, 0.0)
    except RuntimeError as exc:
        assert "SIG" in str(exc)
    else:
        raise AssertionError("stitching an unpoured net should have failed")


def test_unicode_net_name_survives_the_round_trip():
    board, zones = _board(nets=("GNDµé", "SIG"))
    zones[0].net = SimpleNamespace(name="GNDµé")
    count, _grouped = stitch(
        board, ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, "GNDµé",
        0.6, 0.3, 2.0, "Square", 0.0, 0.0,
    )
    assert count > 0
    groups = [i for i in board.placed if not hasattr(i, "padstack")]
    assert "GNDµé" in groups[0].proto.name


def test_via_bigger_than_the_pour_has_no_room():
    board, _zones = _board(size_mm=2.0)
    try:
        stitch(board, ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, "GND",
               4.0, 0.3, 2.0, "Square", 0.0, 0.0)
    except RuntimeError as exc:
        assert "room" in str(exc).lower() or "fit" in str(exc).lower(), exc
    else:
        raise AssertionError("a via wider than the pour should have failed")


def test_offset_far_outside_the_region_fails_cleanly():
    board, _zones = _board()
    try:
        stitch(board, ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, "GND",
               0.6, 0.3, 2.0, "Square", 500.0, 500.0)
    except RuntimeError as exc:
        assert "fit" in str(exc).lower() or "room" in str(exc).lower(), exc
    else:
        raise AssertionError("an offset past the pour should have failed")


def test_stitch_itself_does_not_validate_drill_against_diameter():
    # Documents where the boundary is: the dialog rejects drill >= diameter
    # (test_geometry.py::test_dialogs_build), stitch() does not re-check it, so
    # anything calling stitch() directly owns that validation.
    board, _zones = _board()
    count, _grouped = stitch(
        board, ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, "GND",
        0.3, 0.6, 2.0, "Square", 0.0, 0.0,
    )
    assert count > 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
