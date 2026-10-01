# Tests for the live stitching preview: plugins/_preview.py, shared by both
# builds, and how the IPC dialog feeds it.
#
# The canvas is checked by drawing into a bitmap and reading pixels back, not
# by screenshotting a window (see test_dialog_layout.py for why that was
# abandoned). The plan is built by hand, so which via counts as moved is
# decided here rather than by wherever the nudge happens to land.
#
# Run with the plugin's own venv interpreter, which already has wx, kipy and
# shapely:
#   "$LOCALAPPDATA/KiCad/10.0/python-environments/com.github.jOaSbA.via-stitching/Scripts/python" tests/ipc/test_preview.py
#
# License: GPL-3.0-or-later

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugins"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wx  # noqa: E402
from shapely.geometry import box  # noqa: E402

import _preview  # noqa: E402
import via_stitching_action as vs  # noqa: E402

from test_stitch_coverage import _board  # noqa: E402

_APP = wx.App()
vs._load_settings = lambda: {}  # never read or touch the user's real settings

MM = 1_000_000
ON_GRID = (2 * MM, 2 * MM)
MOVED = (5 * MM + MM // 2, 5 * MM)
RED, GREEN = (200, 52, 52), (52, 200, 52)
STYLES = {"F": ("F.Cu", RED), "B": ("B.Cu", GREEN)}


def _plan(size=10 * MM):
    copper = box(0, 0, size, size)
    return vs.Plan(
        points=[ON_GRID, MOVED],
        candidates=[ON_GRID, (5 * MM, 5 * MM)],
        region=copper,
        net=None,
        layers=[("F", copper), ("B", copper)],
    )


def _canvas(size=200):
    frame = wx.Frame(None)
    canvas = _preview.PreviewCanvas(frame)
    canvas.SetSize((size, size))
    canvas.set_plan(_plan(), 0.6 * MM, STYLES)
    return frame, canvas


def _render(canvas, size=200):
    bmp = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bmp)
    canvas.draw(dc)
    dc.SelectObject(wx.NullBitmap)
    img = bmp.ConvertToImage()
    return lambda x, y: (img.GetRed(int(x), int(y)), img.GetGreen(int(x), int(y)),
                         img.GetBlue(int(x), int(y)))


# ---- canvas ------------------------------------------------------------------

def test_vias_are_drawn_where_they_are_in_their_colors():
    frame, canvas = _canvas()
    pixel = _render(canvas)
    assert pixel(*canvas.to_screen(*ON_GRID)) == _preview.ON_GRID_RGB
    assert pixel(*canvas.to_screen(*MOVED)) == _preview.MOVED_RGB
    copper = pixel(*canvas.to_screen(8 * MM, 8 * MM))
    assert copper not in (_preview.BACKGROUND_RGB, _preview.ON_GRID_RGB, _preview.MOVED_RGB), copper
    assert pixel(1, 1) == _preview.BACKGROUND_RGB, "outside the copper is background"
    frame.Destroy()


def test_each_layer_is_drawn_in_its_own_color():
    # F.Cu over the left two thirds, B.Cu over the right two thirds: each
    # layer's own copper shows in its own color, not just the overlap in one.
    front, back = box(0, 0, 10 * MM, 10 * MM), box(5 * MM, 0, 15 * MM, 10 * MM)
    plan = vs.Plan([], [], front.intersection(back), None, [("F", front), ("B", back)])
    frame = wx.Frame(None)
    canvas = _preview.PreviewCanvas(frame)
    canvas.SetSize((200, 200))
    canvas.set_plan(plan, 0.6 * MM, STYLES)
    pixel = _render(canvas)
    r, g, _b = pixel(*canvas.to_screen(2 * MM, 5 * MM))
    assert r > g, ("front-only copper should be red", r, g)
    r, g, _b = pixel(*canvas.to_screen(13 * MM, 5 * MM))
    assert g > r, ("back-only copper should be green", r, g)
    frame.Destroy()


def test_an_empty_canvas_draws_just_the_background():
    frame = wx.Frame(None)
    canvas = _preview.PreviewCanvas(frame)
    canvas.SetSize((50, 50))
    pixel = _render(canvas, 50)
    assert pixel(25, 25) == _preview.BACKGROUND_RGB
    frame.Destroy()


def test_fit_shows_all_of_the_copper():
    frame, canvas = _canvas()
    w, h = canvas.GetClientSize()
    for corner in ((0, 0), (10 * MM, 10 * MM)):
        sx, sy = canvas.to_screen(*corner)
        assert 0 <= sx <= w and 0 <= sy <= h, (corner, sx, sy)
    frame.Destroy()


def test_zoom_keeps_the_point_under_the_cursor():
    frame, canvas = _canvas()
    before = canvas.to_screen(*MOVED)
    scale = canvas.scale
    canvas.zoom_at(before[0], before[1], 2.0)
    after = canvas.to_screen(*MOVED)
    assert abs(after[0] - before[0]) < 1e-6 and abs(after[1] - before[1]) < 1e-6
    assert canvas.scale == scale * 2.0
    frame.Destroy()


def test_the_view_survives_a_new_plan_on_the_same_copper():
    # Zoom in, then change something that leaves the copper alone (spacing,
    # say): the view stays put. New copper refits.
    frame, canvas = _canvas()
    canvas.zoom_at(100, 100, 3.0)
    zoomed = canvas.scale
    canvas.set_plan(_plan()._replace(points=[ON_GRID]), 0.6 * MM, STYLES)
    assert canvas.scale == zoomed
    canvas.set_plan(_plan(size=20 * MM), 0.6 * MM, STYLES)
    assert canvas.scale < zoomed / 3
    frame.Destroy()


# ---- panel -------------------------------------------------------------------

def _panel(compute):
    frame = wx.Frame(None)
    return frame, _preview.PreviewPanel(frame, compute, lambda s: s)


def test_the_summary_counts_the_moved_vias():
    frame, panel = _panel(lambda: (_plan(), 0.6 * MM, STYLES))
    panel.update()
    text = panel.summary.GetLabel()
    assert text.startswith("2 vias") and "1 moved" in text, text

    panel._compute = lambda: (_plan()._replace(points=[ON_GRID]), 0.6 * MM, STYLES)
    panel.update()
    assert panel.summary.GetLabel() == "1 vias"
    frame.Destroy()


def test_the_legend_names_the_layers_drawn():
    frame, panel = _panel(lambda: (_plan(), 0.6 * MM, STYLES))
    panel.update()
    labels = [w.GetLabel() for w in panel.GetChildren() if isinstance(w, wx.StaticText)]
    assert "F.Cu" in labels and "B.Cu" in labels, labels
    frame.Destroy()


def test_an_error_is_shown_and_the_last_good_drawing_stays():
    frame, panel = _panel(lambda: (_plan(), 0.6 * MM, STYLES))
    panel.update()
    good = panel.canvas.plan

    def fails():
        raise ValueError("The drill must be smaller than the via diameter.")

    panel._compute = fails
    panel.update()
    assert panel.summary.GetLabel().startswith("The drill"), panel.summary.GetLabel()
    assert panel.canvas.plan is good

    def breaks():
        raise KeyError("boom")

    panel._compute = breaks
    panel.update()
    assert "boom" in panel.summary.GetLabel(), "an unexpected error is still shown, not raised"
    frame.Destroy()


def test_unticking_auto_update_stops_scheduling():
    frame, panel = _panel(lambda: (_plan(), 0.6 * MM, STYLES))
    panel.auto.SetValue(False)
    panel.schedule()
    assert panel._timer is None
    panel.auto.SetValue(True)
    panel.schedule()
    assert panel._timer.IsRunning()
    panel._timer.Stop()
    frame.Destroy()


# ---- the dialog --------------------------------------------------------------

def _dialog():
    board, _zones = _board()
    board.get_selection = lambda kind: []
    board.get_items = lambda types: []
    dlg = vs.ViaStitchingDialog(None, ["GND", "SIG"], board)
    return board, dlg


def test_the_dialog_previews_its_own_settings_without_placing_anything():
    board, dlg = _dialog()
    dlg.preview.update()
    shown = dlg.preview.canvas.plan
    assert shown is not None and len(shown.points) > 0
    assert board.placed == []

    # OK then places exactly what was shown.
    count, _grouped = vs.stitch(board, **dlg.values())
    assert count == len(shown.points)
    dlg.Destroy()


def test_micro_vias_between_two_inner_layers_preview_those_layers():
    # Only In1.Cu and In2.Cu poured, stitched with micro vias: the preview
    # draws and names those two, not the outer layers.
    from kipy.board_types import BoardLayer

    board, zones = _board(inner_layer=True)
    fills = zones[0].filled_polygons
    fills[BoardLayer.BL_In2_Cu] = fills[BoardLayer.BL_In1_Cu]
    del fills[BoardLayer.BL_F_Cu], fills[BoardLayer.BL_B_Cu]
    board.get_enabled_layers = lambda: [
        BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu, BoardLayer.BL_In2_Cu, BoardLayer.BL_B_Cu,
    ]
    board.get_selection = lambda kind: []
    board.get_items = lambda types: []
    dlg = vs.ViaStitchingDialog(None, ["GND", "SIG"], board)
    dlg.via_type.SetStringSelection("Micro")
    dlg._on_via_type()
    dlg.start_layer.SetStringSelection("In1.Cu")
    dlg.end_layer.SetStringSelection("In2.Cu")
    dlg.preview.update()

    shown = dlg.preview.canvas.plan
    assert shown is not None, dlg.preview.summary.GetLabel()
    assert [l for l, _c in shown.layers] == [BoardLayer.BL_In1_Cu, BoardLayer.BL_In2_Cu]
    labels = [w.GetLabel() for w in dlg.preview.GetChildren() if isinstance(w, wx.StaticText)]
    assert "In1.Cu" in labels and "In2.Cu" in labels and "F.Cu" not in labels, labels
    dlg.Destroy()


def test_changing_a_setting_queues_an_update():
    board, dlg = _dialog()
    dlg.preview._timer.Stop()  # the one queued on opening
    dlg.spacing.SetValue("1.0")  # SetValue, unlike ChangeValue, sends EVT_TEXT
    assert dlg.preview._timer.IsRunning()
    dlg.preview._timer.Stop()
    dlg.preview.update()
    assert len(dlg.preview.canvas.plan.points) > 0
    dlg.Destroy()


def test_bad_input_is_shown_in_the_preview_not_a_popup():
    board, dlg = _dialog()
    dlg.preview.update()
    good = dlg.preview.canvas.plan
    dlg.drill.SetValue("5")  # wider than the via
    dlg.preview.update()
    assert "drill" in dlg.preview.summary.GetLabel().lower()
    assert dlg.preview.canvas.plan is good
    dlg.net.SetValue("NO_SUCH_NET")
    dlg.drill.SetValue("0.3")
    dlg.preview.update()
    assert "NO_SUCH_NET" in dlg.preview.summary.GetLabel()
    dlg.Destroy()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
