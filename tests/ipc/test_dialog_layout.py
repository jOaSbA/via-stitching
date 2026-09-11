# Dialog rendering/layout tests for the IPC dialog, the mirror of
# tests/legacy/test_dialog_layout_legacy.py.
#
# Geometric, not visual: they read the positions and sizes wx's own layout
# engine computed (GetRect()/GetPosition()), which is what decides whether the
# real rendered dialog looks right. Screenshotting a real wx window was tried
# for the legacy dialog and abandoned: off-screen windows never get composited
# so the capture comes back blank, and an on-screen capture can grab a
# different overlapping window, besides putting a real window on the user's
# desktop. test_geometry.py already covers what the dialog returns; this
# covers where it puts things.
#
# Run with the plugin's own venv interpreter, which already has wx, kipy and
# shapely:
#   "$LOCALAPPDATA/KiCad/10.0/python-environments/com.github.jOaSbA.via-stitching/Scripts/python" tests/ipc/test_dialog_layout.py
#
# License: GPL-3.0-or-later

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugins"))

import wx  # noqa: E402
from kipy.board_types import BoardLayer  # noqa: E402

import via_stitching_action as vs  # noqa: E402
from via_stitching_action import ViaStitchingDialog  # noqa: E402

# One app for the whole run: a function-local wx.App can be torn down the
# moment that function returns, and the next dialog built then dies with
# PyNoAppError.
_APP = wx.App()

# Never _clear_settings() here. That is the user's real settings file, and a
# test suite has no business deleting it just to get a predictable dialog.
vs._load_settings = lambda: {}

LAYERS = {
    BoardLayer.BL_F_Cu: "F.Cu",
    BoardLayer.BL_In1_Cu: "In1.Cu",
    BoardLayer.BL_In2_Cu: "In2.Cu",
    BoardLayer.BL_B_Cu: "B.Cu",
}


def _board():
    return SimpleNamespace(
        get_enabled_layers=lambda: list(LAYERS.keys()),
        get_layer_name=lambda l: LAYERS[l],
        get_selection=lambda kind: [],
        get_items=lambda types: [],
    )


def _dialog():
    return ViaStitchingDialog(None, ["GND", "VCC"], _board())


def _leaf_controls(window):
    """Every control wx actually lays out and paints. wx.StaticBox borders are
    siblings of a group's real children, not containers (StaticBoxSizer never
    reparents anything onto the box), so they overlap by design and are
    skipped. Hidden controls are skipped too: they are never laid out and sit
    at (0, 0)."""
    return [c for c in window.GetChildren()
            if c.IsShown() and not isinstance(c, wx.StaticBox)]


def _rects_overlap(a, b):
    return not (
        a.GetRight() <= b.GetLeft() or b.GetRight() <= a.GetLeft()
        or a.GetBottom() <= b.GetTop() or b.GetBottom() <= a.GetTop()
    )


def test_no_two_controls_overlap():
    dlg = _dialog()
    try:
        controls = _leaf_controls(dlg)
        assert len(controls) > 10, "did not find the real controls"
        overlaps = [
            (type(a).__name__ + " " + a.GetLabel(), type(b).__name__ + " " + b.GetLabel())
            for i, a in enumerate(controls) for b in controls[i + 1:]
            if _rects_overlap(a.GetRect(), b.GetRect())
        ]
        assert not overlaps, f"overlapping controls: {overlaps}"
    finally:
        dlg.Destroy()


def test_all_controls_within_dialog_client_area():
    dlg = _dialog()
    try:
        client_w, client_h = dlg.GetClientSize()
        out = [
            (type(c).__name__, c.GetRect()) for c in _leaf_controls(dlg)
            if c.GetRect().GetLeft() < 0 or c.GetRect().GetTop() < 0
            or c.GetRect().GetRight() > client_w or c.GetRect().GetBottom() > client_h
        ]
        assert not out, f"controls outside the client area {(client_w, client_h)}: {out}"
    finally:
        dlg.Destroy()


def test_groups_stack_net_then_via_then_pattern():
    dlg = _dialog()
    try:
        boxes = [c for c in dlg.GetChildren() if isinstance(c, wx.StaticBox)]
        assert len(boxes) == 3, f"expected 3 group boxes, found {len(boxes)}"
        tops = [b.GetPosition().y for b in boxes]
        assert tops == sorted(tops), "group boxes are not stacked top to bottom"
        assert dlg.net.GetPosition().y < dlg.via_type.GetPosition().y < dlg.pattern.GetPosition().y
    finally:
        dlg.Destroy()


def test_label_and_control_pairs_are_row_aligned():
    """Each (StaticText, control) pair added as one row has to land in the same
    row, or a user sees a label pointing at the wrong field."""
    dlg = _dialog()
    try:
        pairs = [
            (dlg.net, "Net Name:"), (dlg.via_type, "Via Type:"),
            (dlg.start_layer, "Start Layer:"), (dlg.end_layer, "End Layer:"),
            (dlg.via_dia, "Via Diameter (mm):"), (dlg.drill, "Drill (mm):"),
            (dlg.pattern, "Via Pattern:"), (dlg.spacing, "Spacing (mm):"),
            (dlg.x_offset, "X-Offset (mm):"), (dlg.y_offset, "Y-Offset (mm):"),
        ]
        labels = {c.GetLabel(): c for c in dlg.GetChildren() if isinstance(c, wx.StaticText)}
        misaligned = []
        for ctrl, text in pairs:
            label = labels.get(text)
            assert label is not None, f"no StaticText labelled {text!r}"
            ctrl_mid = ctrl.GetRect().GetTop() + ctrl.GetRect().GetHeight() / 2
            label_mid = label.GetRect().GetTop() + label.GetRect().GetHeight() / 2
            if abs(ctrl_mid - label_mid) > 12:  # controls differ in height
                misaligned.append((text, ctrl_mid, label_mid))
        assert not misaligned, f"label/control rows not aligned: {misaligned}"
    finally:
        dlg.Destroy()


def test_advisory_label_grows_the_dialog_instead_of_overlapping():
    dlg = _dialog()
    try:
        assert not dlg.advisory_label.IsShown()
        hidden_h = dlg.GetSize().height

        dlg.via_type.SetStringSelection("Micro")
        dlg._on_via_type()
        dlg.start_layer.SetSelection(1)  # In1.Cu
        dlg.end_layer.SetSelection(2)    # In2.Cu, so neither end is an outer layer
        dlg._update_advisory()
        assert dlg.advisory_label.IsShown(), "an inner-to-inner microvia raises the advisory"
        assert dlg.GetSize().height > hidden_h
    finally:
        dlg.Destroy()


def test_dialog_has_a_sane_size():
    dlg = _dialog()
    try:
        size = dlg.GetSize()
        assert 200 < size.width < 800, f"dialog width {size.width} looks unreasonable"
        assert 300 < size.height < 900, f"dialog height {size.height} looks unreasonable"
    finally:
        dlg.Destroy()


def test_button_row_is_below_every_group_box():
    dlg = _dialog()
    try:
        lowest = max(b.GetRect().GetBottom()
                     for b in dlg.GetChildren() if isinstance(b, wx.StaticBox))
        assert dlg.reset_btn.GetPosition().y > lowest, (
            "the Reset/OK/Cancel row must sit below every group box"
        )
    finally:
        dlg.Destroy()


def test_layer_combo_swatches_are_distinct_colors():
    """The bitmaps behind the layer combo entries must not all be the same
    placeholder color: each layer gets its own swatch."""
    dlg = _dialog()
    try:
        # Through locks the layer combos to a single entry, so there is nothing
        # to compare until a via type that spans a chosen pair is selected.
        dlg.via_type.SetStringSelection("Micro")
        dlg._on_via_type()
        assert dlg.start_layer.GetCount() == 4

        colors = set()
        for i in range(dlg.start_layer.GetCount()):
            bmp = dlg.start_layer.GetItemBitmap(i)
            assert bmp.IsOk()
            img = bmp.ConvertToImage()
            colors.add((img.GetRed(7, 7), img.GetGreen(7, 7), img.GetBlue(7, 7)))
        assert len(colors) == 4, f"expected 4 distinct layer colors, got {colors}"
    finally:
        dlg.Destroy()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
