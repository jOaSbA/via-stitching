# The live stitching preview on the SWIG build: a real plan() off a real
# KiCad board, drawn by plugins/_preview.py with the wxPython KiCad itself
# ships, which is older than the IPC venv's and is the one this build runs
# on. tests/ipc/test_preview.py covers the canvas maths and the panel; this
# covers that it works here at all, and how the legacy dialog feeds it.
#
# Run with KiCad's own interpreter:
#   "C:/Program Files/KiCad/6.0/bin/python.exe" tests/legacy/test_preview_legacy.py

import os
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pcbnew
import wx

wx.DisableAsserts()  # see test_edge_cases_legacy.py for why

import _geometry_legacy as geo
import _fixtures as fixtures
import via_stitching_action_legacy as vsl
import _preview  # resolved through the plugin's own fallback to ../plugins

from test_stitch_legacy_coverage import _board

_APP = wx.App()
vsl._load_settings = lambda: {}  # the dialog starts from defaults, not the user's file
MM = 1_000_000


def _board_with_a_blocker():
    """GND poured on both sides, plus a small VCC pad just off one grid
    position, close enough to block it and far enough that the nudge clears."""
    board, _gnd = _board(layers=2)
    vcc = pcbnew.NETINFO_ITEM(board, "VCC")
    board.Add(vcc)
    fp = pcbnew.FOOTPRINT(board)
    pad = pcbnew.PAD(fp)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetSize(geo.size(200_000, 200_000))
    # The grid starts at the inset edge, 0.35 mm in for a 0.6 mm via.
    pad.SetPosition(geo.point(4_350_000 + 300_000, 4_350_000))
    pad.SetLayerSet(fixtures.layer_set(pcbnew.F_Cu))
    pad.SetNet(vcc)
    fp.Add(pad)
    board.Add(fp)
    return board


def test_a_real_plan_draws_with_kicads_own_wx():
    p = vsl.plan(
        _board_with_a_blocker(), pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
        via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
        x_offset_mm=0, y_offset_mm=0,
    )
    frame = wx.Frame(None)
    canvas = _preview.PreviewCanvas(frame)
    canvas.SetSize((300, 300))
    canvas.set_plan(p, 0.6 * MM, {l: (str(l), (200, 52, 52)) for l, _c in p.layers})
    assert canvas.moved and canvas.on_grid, (len(canvas.moved), len(canvas.on_grid))

    bmp = wx.Bitmap(300, 300)
    dc = wx.MemoryDC(bmp)
    canvas.draw(dc)
    dc.SelectObject(wx.NullBitmap)
    img = bmp.ConvertToImage()

    def pixel(pt):
        x, y = (int(v) for v in canvas.to_screen(*pt))
        return img.GetRed(x, y), img.GetGreen(x, y), img.GetBlue(x, y)

    assert pixel(canvas.moved[0]) == _preview.MOVED_RGB, pixel(canvas.moved[0])
    assert pixel(canvas.on_grid[0]) == _preview.ON_GRID_RGB, pixel(canvas.on_grid[0])
    frame.Destroy()


def test_the_dialog_previews_what_ok_then_places():
    board = _board_with_a_blocker()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    dlg.net.SetValue("GND")
    dlg.preview.update()
    shown = dlg.preview.canvas.plan
    assert shown is not None and len(shown.points) > 0, dlg.preview.summary.GetLabel()
    assert not any(isinstance(t, pcbnew.PCB_VIA) for t in board.GetTracks())

    placed, _grouped = vsl.stitch(board, **dlg.values())
    assert placed == len(shown.points)
    dlg.Destroy()


def test_micro_vias_between_two_inner_layers_preview_those_layers():
    # A four-layer board with GND poured only on In1.Cu and In2.Cu, stitched
    # with micro vias between them: the preview draws and names those two.
    board, _gnd = _board(layers=4, poured=[pcbnew.In1_Cu, pcbnew.In2_Cu])
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    dlg.net.SetValue("GND")
    dlg.via_type.SetStringSelection("Micro")
    dlg._on_via_type()
    dlg.start_layer.SetStringSelection("L2 - In1.Cu")
    dlg.end_layer.SetStringSelection("L3 - In2.Cu")
    dlg.preview.update()

    shown = dlg.preview.canvas.plan
    assert shown is not None, dlg.preview.summary.GetLabel()
    assert [l for l, _c in shown.layers] == [pcbnew.In1_Cu, pcbnew.In2_Cu]
    labels = [w.GetLabel() for w in dlg.preview.GetChildren() if isinstance(w, wx.StaticText)]
    assert "In1.Cu" in labels and "In2.Cu" in labels and "F.Cu" not in labels, labels
    dlg.Destroy()


def test_changing_a_setting_queues_an_update():
    dlg = vsl.ViaStitchingDialogLegacy(None, _board_with_a_blocker())
    dlg.preview._timer.Stop()  # the one queued on opening
    dlg.spacing.SetValue("1.0")
    assert dlg.preview._timer.IsRunning()
    dlg.preview._timer.Stop()
    dlg.Destroy()


def run():
    tests = [
        test_a_real_plan_draws_with_kicads_own_wx,
        test_the_dialog_previews_what_ok_then_places,
        test_micro_vias_between_two_inner_layers_preview_those_layers,
        test_changing_a_setting_queues_an_update,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print()
    print(f"ALL {len(tests)} PREVIEW TESTS PASSED")


if __name__ == "__main__":
    run()
