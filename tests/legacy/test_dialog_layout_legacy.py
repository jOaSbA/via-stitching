# Dialog rendering/layout tests -- geometric, not visual.
#
# Tried actually screenshotting the dialog first (PrintWindow / BitBlt via
# ctypes, off-screen and on-screen). Off-screen: only the title bar
# rendered, the client area came back solid black -- PrintWindow doesn't
# force a real paint on a window Windows never actually composited.
# On-screen: showed a genuinely different overlapping window's content
# instead of the dialog (occlusion), and put a real, if brief, window on
# the real desktop -- unsafe and unreliable, abandoned.
#
# This is the robust replacement: read the real positions/sizes wx's own
# layout engine computed via GetRect()/GetPosition(), which is exactly
# what determines whether the real rendered dialog looks right, without
# depending on screen capture completeness or touching the user's screen
# at all.

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")

import pcbnew
import wx

wx.DisableAsserts()  # see test_edge_cases_legacy.py for why

import via_stitching_action_legacy as vsl

_APP = wx.App()

MM = 1_000_000


def _board(layers=4):
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(layers)
    net = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(net)
    return board


def _leaf_controls(window):
    """Every control wx actually lays out and paints, excluding the
    decorative wx.StaticBox borders themselves (those are SIBLINGS of the
    group's real children, not containers -- StaticBoxSizer never
    reparents anything onto the box) and the dialog window itself."""
    out = []
    for child in window.GetChildren():
        if isinstance(child, wx.StaticBox):
            continue
        out.append(child)
    return out


def _rects_overlap(a, b):
    return not (
        a.GetRight() <= b.GetLeft() or b.GetRight() <= a.GetLeft()
        or a.GetBottom() <= b.GetTop() or b.GetBottom() <= a.GetTop()
    )


def test_no_two_controls_overlap():
    board = _board()
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    controls = _leaf_controls(dlg)
    assert len(controls) > 10  # sanity: did we actually find the real controls
    overlaps = []
    for i, a in enumerate(controls):
        for b in controls[i + 1:]:
            if _rects_overlap(a.GetRect(), b.GetRect()):
                overlaps.append((a.GetLabel() if hasattr(a, "GetLabel") else type(a).__name__,
                                  b.GetLabel() if hasattr(b, "GetLabel") else type(b).__name__))
    assert not overlaps, f"overlapping controls: {overlaps}"
    dlg.Destroy()
    vsl._clear_settings()


def test_all_controls_within_dialog_client_area():
    board = _board()
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    client_w, client_h = dlg.GetClientSize()
    out_of_bounds = []
    for c in _leaf_controls(dlg):
        r = c.GetRect()
        if r.GetLeft() < 0 or r.GetTop() < 0 or r.GetRight() > client_w or r.GetBottom() > client_h:
            out_of_bounds.append((type(c).__name__, r, (client_w, client_h)))
    assert not out_of_bounds, f"controls outside the dialog's client area: {out_of_bounds}"
    dlg.Destroy()
    vsl._clear_settings()


def test_groups_stack_top_to_bottom_in_ipc_matching_order():
    """Net Name group, then Via Type/Layers/Size group, then Pattern/
    Placement group -- matching the real IPC dialog's order exactly (this
    was a real bug fixed earlier: an initial version put Net Name last,
    matching a stale reference screenshot instead of the current shipped
    IPC source)."""
    board = _board()
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    boxes = [c for c in dlg.GetChildren() if isinstance(c, wx.StaticBox)]
    assert len(boxes) == 3, f"expected 3 group boxes, found {len(boxes)}"
    tops = [b.GetPosition().y for b in boxes]
    assert tops == sorted(tops), "group boxes are not stacked strictly top to bottom"

    net_top = dlg.net.GetPosition().y
    via_type_top = dlg.via_type.GetPosition().y
    pattern_top = dlg.pattern.GetPosition().y
    assert net_top < via_type_top < pattern_top, (
        "Net Name must come first, then Via Type/Layers/Size, then Pattern/Placement"
    )
    dlg.Destroy()
    vsl._clear_settings()


def test_label_and_control_pairs_are_row_aligned():
    """Every (StaticText, control) pair added by _make_group as one row
    must actually land in the same row -- their vertical centers should be
    close, or a real user would see a label pointing at the wrong field."""
    board = _board()
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    pairs = [
        (dlg.via_type, "Via Type:"), (dlg.start_layer, "Start Layer:"),
        (dlg.end_layer, "End Layer:"), (dlg.via_dia, "Via Diameter (mm):"),
        (dlg.drill, "Drill (mm):"), (dlg.pattern, "Via Pattern:"),
        (dlg.spacing, "Spacing (mm):"), (dlg.x_offset, "X-Offset (mm):"),
        (dlg.y_offset, "Y-Offset (mm):"), (dlg.net, "Net Name:"),
    ]
    labels_by_text = {
        c.GetLabel(): c for c in dlg.GetChildren() if isinstance(c, wx.StaticText)
    }
    misaligned = []
    for ctrl, label_text in pairs:
        label = labels_by_text.get(label_text)
        assert label is not None, f"no StaticText found with label {label_text!r}"
        ctrl_center = ctrl.GetRect().GetTop() + ctrl.GetRect().GetHeight() / 2
        label_center = label.GetRect().GetTop() + label.GetRect().GetHeight() / 2
        if abs(ctrl_center - label_center) > 12:  # generous: different controls have different heights
            misaligned.append((label_text, ctrl_center, label_center))
    assert not misaligned, f"label/control rows not aligned: {misaligned}"
    dlg.Destroy()
    vsl._clear_settings()


def test_advisory_label_changes_dialog_height_when_shown():
    board = _board()
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    height_hidden = dlg.GetSize().height
    assert not dlg.advisory_label.IsShown()

    dlg.via_type.SetStringSelection("Micro")
    dlg._on_via_type()
    dlg.start_layer.SetSelection(1)  # a real board's In1_Cu, not an outer layer
    dlg.end_layer.SetSelection(2)    # In2_Cu -- neither outer -> triggers the advisory
    dlg._update_advisory()
    assert dlg.advisory_label.IsShown()
    height_shown = dlg.GetSize().height

    assert height_shown > height_hidden, "showing the advisory should grow the dialog, not overlap it"
    dlg.Destroy()
    vsl._clear_settings()


def test_dialog_has_a_sane_minimum_size():
    board = _board()
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    size = dlg.GetSize()
    assert 200 < size.width < 800, f"dialog width {size.width} looks unreasonable"
    assert 300 < size.height < 900, f"dialog height {size.height} looks unreasonable"
    dlg.Destroy()
    vsl._clear_settings()


def test_button_row_is_below_every_group_box():
    board = _board()
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    boxes = [c for c in dlg.GetChildren() if isinstance(c, wx.StaticBox)]
    lowest_box_bottom = max(b.GetRect().GetBottom() for b in boxes)
    reset_top = dlg.reset_btn.GetPosition().y
    assert reset_top > lowest_box_bottom, "the Reset/OK/Cancel row must sit below every group box"
    dlg.Destroy()
    vsl._clear_settings()


def test_layer_combo_swatches_are_actually_distinct_colors():
    """Confirms the bitmaps behind the layer combo entries aren't all the
    same placeholder color -- each layer should get its own swatch."""
    board = _board(layers=4)
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    dlg.via_type.SetStringSelection("Micro")  # unlocks all 4 layers in the combo
    dlg._on_via_type()
    colors = set()
    for i in range(dlg.start_layer.GetCount()):
        bmp = dlg.start_layer.GetItemBitmap(i)
        assert bmp.IsOk()
        img = bmp.ConvertToImage()
        colors.add((img.GetRed(7, 7), img.GetGreen(7, 7), img.GetBlue(7, 7)))
    assert len(colors) == 4, f"expected 4 distinct layer colors, got {colors}"
    dlg.Destroy()


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
