# Offline self-check for the grid and via construction. No KiCad needed.
#
# Run with the plugin's own venv interpreter, which already has wx, kipy and shapely:
#   "$LOCALAPPDATA/KiCad/10.0/python-environments/com.github.jOaSbA.via-stitching/Scripts/python" tests/test_geometry.py
#
# License: GPL-3.0-or-later

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins"))

from kipy.board_types import BoardLayer, Net, PSS_CIRCLE, ViaType  # noqa: E402
from kipy.errors import ConnectionError as KiCadConnectionError  # noqa: E402
from kipy.util import from_mm  # noqa: E402

from via_stitching_action import _grid_points, _keepout_region, _make_via  # noqa: E402

MM = from_mm(1.0)
BOX = (0, 0, 10 * MM, 10 * MM)


def test_square_grid():
    pts = list(_grid_points(BOX, from_mm(2.0), "Square"))
    assert len(pts) == 36, len(pts)  # 6 x 6 inclusive of both edges
    assert all(x % from_mm(2.0) == 0 and y % from_mm(2.0) == 0 for x, y in pts)


def test_hexagonal_grid():
    spacing = from_mm(2.0)
    pts = list(_grid_points(BOX, spacing, "Hexagonal"))
    rows = sorted({y for _, y in pts})
    assert rows[1] - rows[0] == int(spacing * math.sqrt(3) / 2)
    # Odd rows are offset by half a pitch, which is what makes hex denser.
    odd_row_xs = {x for x, y in pts if y == rows[1]}
    assert min(odd_row_xs) == spacing // 2

    # Hex fits ~15% more vias per area at the same centre-to-centre distance. Only
    # visible on a box big enough that the lost half-column on odd rows stops
    # dominating, which on a 10 mm box it does (33 hex vs 36 square).
    big = (0, 0, 100 * MM, 100 * MM)
    assert len(list(_grid_points(big, spacing, "Hexagonal"))) > len(
        list(_grid_points(big, spacing, "Square"))
    )


def test_zero_spacing_terminates():
    # A regression here is an infinite loop, not a wrong answer.
    assert list(_grid_points(BOX, 0, "Square")) == []
    assert list(_grid_points(BOX, 0, "Hexagonal")) == []


def test_make_via():
    via = _make_via(1000, 2000, from_mm(0.6), from_mm(0.3), Net())
    layers = via.padstack.copper_layers
    assert len(layers) == 1
    assert layers[0].layer == BoardLayer.BL_F_Cu
    assert layers[0].shape == PSS_CIRCLE
    # Zero size or an unset shape is the failure mode that looks like "invisible vias".
    assert layers[0].size.x == 600000 and layers[0].size.y == 600000
    assert via.drill_diameter == 300000
    assert via.type == ViaType.VT_THROUGH
    assert via.padstack.drill.start_layer == BoardLayer.BL_F_Cu
    assert via.padstack.drill.end_layer == BoardLayer.BL_B_Cu
    assert (via.position.x, via.position.y) == (1000, 2000)


def test_keepout_region_uses_drill_not_copper():
    # Regression: _keepout_region must size clearance off the hole (drill), not the
    # copper pad/annular ring, or hole-to-hole spacing comes out far too generous.
    from types import SimpleNamespace

    from kipy.board_types import PadType

    via = SimpleNamespace(
        position=SimpleNamespace(x=0, y=0),
        diameter=from_mm(2.0),  # large copper diameter
        drill_diameter=from_mm(0.3),  # small drill
    )
    board = SimpleNamespace(get_vias=lambda: [via], get_pads=lambda: [])
    region = _keepout_region(board, from_mm(0.15))
    # bounds is a square [-r, -r, r, r]; r == via_radius + drill_radius + margin
    minx, miny, maxx, maxy = region.bounds
    expected_r = from_mm(0.15) + from_mm(0.15) + from_mm(0.25)
    assert abs(maxx - expected_r) < from_mm(0.01), (maxx, expected_r)

    pad = SimpleNamespace(
        pad_type=PadType.PT_PTH,
        position=SimpleNamespace(x=0, y=0),
        padstack=SimpleNamespace(drill=SimpleNamespace(diameter=SimpleNamespace(x=from_mm(0.3)))),
    )
    board_pad_only = SimpleNamespace(get_vias=lambda: [], get_pads=lambda: [pad])
    region2 = _keepout_region(board_pad_only, from_mm(0.15))
    _, _, maxx2, _ = region2.bounds
    assert abs(maxx2 - expected_r) < from_mm(0.01), (maxx2, expected_r)


def test_connection_help():
    from via_stitching_action import _api_enabled_in_config, _connection_help

    off = _connection_help(False)
    on = _connection_help(True)
    unknown = _connection_help(None)
    assert off != on != unknown != off

    # The whole point: never tell someone to just restart when the setting is off.
    assert "switched off" in off
    assert "Enable KiCad API" in off
    # "restart" may appear in the off case, but only after enabling it.
    assert off.index("Enable KiCad API") < off.index("restart")

    # Only the enabled case leads with restarting.
    assert "enabled but not listening" in on
    assert on.lstrip().startswith("KiCad's API server is enabled")

    # A timeout is not evidence the server is down, so it never says "restart".
    for enabled in (True, False, None):
        busy = _connection_help(enabled, dial_failed=False)
        assert "restart" not in busy.lower()
        assert "busy" in busy

    # Pin the two messages kipy really builds, straight from its own source. If a
    # kipy bump rewords them, this fails instead of the advice silently degrading.
    from via_stitching_action import _is_dial_failure

    assert _is_dial_failure(KiCadConnectionError(
        "Failed to connect to KiCad: Connection refused"))
    assert not _is_dial_failure(KiCadConnectionError(
        "Error receiving reply from KiCad: Timed out"))

    # Reading the real config must not raise, whatever is installed.
    assert _api_enabled_in_config() in (True, False, None)


def test_dialogs_build():
    # The error dialog is the one thing that must never fail, so build it for real.
    import wx

    from via_stitching_action import ErrorDialog, ViaStitchingDialog

    app = wx.App()  # noqa: F841

    err = ErrorDialog(None, "summary line", "line one\nline two")
    try:
        assert err.GetTitle() == "Via Stitching Error"
    finally:
        err.Destroy()

    dlg = ViaStitchingDialog(None, ["GND", "VCC"])
    try:
        assert dlg.values()["net_name"] == "GND"

        for field, bad in (
            (dlg.spacing, "0"),
            (dlg.spacing, "-1"),
            (dlg.spacing, "abc"),
            (dlg.drill, "0.6"),  # drill >= via diameter
        ):
            good = field.GetValue()
            field.SetValue(bad)
            try:
                dlg.values()
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad!r} should have been rejected")
            field.SetValue(good)
    finally:
        dlg.Destroy()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
