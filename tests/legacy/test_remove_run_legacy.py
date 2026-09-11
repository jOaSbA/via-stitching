# Tests for the "remove a stitching run" action (ViaStitchingRemoveLegacy)
# and the geometry it stands on: geo.grouped_vias() + stitching_runs().
#
# Why this needs a real board and not a fake: a group cannot be asked for
# its members from Python at all (GetItems() returns a non-iterable
# SwigPyObject, RunOnChildren() has no std::function typemap), so
# grouped_vias() finds them by scanning the board for the GetParentGroup()
# back-link -- and SWIG hands out a fresh Python wrapper on every call, so
# the group match has to be on .this, never identity. Both of those only
# hold, or fail, against the real bindings.

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")

import pcbnew
import wx

wx.DisableAsserts()  # ActionPlugin.register() at import time, see the other suites

import _geometry_legacy as geo  # noqa: E402
import via_stitching_action_legacy as vsl  # noqa: E402

_APP = wx.App()  # one per process, never function-local (see test_dialog_layout_legacy.py)

MM = 1_000_000


def _square_polyset(x0, y0, x1, y1):
    ps = pcbnew.SHAPE_POLY_SET()
    chain = pcbnew.SHAPE_LINE_CHAIN()
    for (x, y) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
        chain.Append(pcbnew.VECTOR2I(int(x), int(y)))
    chain.SetClosed(True)
    ps.AddOutline(chain)
    return ps


def _board(size=10 * MM):
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(gnd)
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        z = pcbnew.ZONE(board)
        z.SetNet(gnd)
        z.SetLayer(layer)
        board.Add(z)
        z.SetFilledPolysList(layer, _square_polyset(0, 0, size, size))
    return board, gnd


def _stitch(board):
    return vsl.stitch(
        board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
        via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
        x_offset_mm=0, y_offset_mm=0,
    )


def _vias(board):
    return [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]


def test_stitch_leaves_a_findable_run():
    board, _ = _board()
    placed, grouped = _stitch(board)
    assert placed > 0 and grouped

    runs = vsl.stitching_runs(board)
    assert len(runs) == 1
    group, vias = runs[0]
    assert group.GetName().startswith(vsl.GROUP_PREFIX)
    # Layer names, not raw layer ids -- the run picker shows this string.
    assert "F.Cu:B.Cu" in group.GetName()
    assert len(vias) == placed


def test_grouped_vias_only_returns_that_groups_vias():
    board, gnd = _board()
    placed, _ = _stitch(board)
    stitch_group, stitch_vias = vsl.stitching_runs(board)[0]

    # A second, unrelated group of vias: neither ours by name nor by parent.
    stray = geo.make_via(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu,
                         600000, 300000, gnd.GetNetCode(), 50 * MM, 50 * MM)
    board.Add(stray)
    other = geo.group_vias(board, [stray], "Some other group")

    assert len(vsl.stitching_runs(board)) == 1, "a non-stitching group must not show up as a run"
    assert len(geo.grouped_vias(board, stitch_group)) == placed
    assert len(geo.grouped_vias(board, other)) == 1
    assert all(v.GetParentGroup().this == stitch_group.this for v in stitch_vias)


def test_removing_a_run_takes_its_vias_and_nothing_else():
    board, gnd = _board()
    placed, _ = _stitch(board)
    stray = geo.make_via(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu,
                         600000, 300000, gnd.GetNetCode(), 50 * MM, 50 * MM)
    board.Add(stray)
    assert len(_vias(board)) == placed + 1

    group, vias = vsl.stitching_runs(board)[0]
    geo.delete_grouped_vias(board, group, vias)

    left = _vias(board)
    assert len(left) == 1, "only the ungrouped stray via should survive"
    assert left[0].GetPosition().x == 50 * MM
    assert vsl.stitching_runs(board) == []
    assert len(board.Groups()) == 0


def test_run_with_no_vias_left_is_not_offered():
    # Deleting the vias by hand (Del in the editor) leaves the group behind;
    # offering that as a removable "run" would be a dead menu entry.
    board, _ = _board()
    _stitch(board)
    for via in _vias(board):
        board.Remove(via)
    assert len(board.Groups()) == 1
    assert vsl.stitching_runs(board) == []


def test_reset_last_run_button_tracks_board_state():
    board, _ = _board()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    try:
        assert not dlg.remove_run_btn.IsEnabled(), "nothing stitched yet"
        assert dlg.remove_run_label.GetLabel() == "No stitching run on this board"
        # It sits above everything else in the dialog. Hidden controls are
        # skipped: the advisory label is Hide()n and never laid out, so it
        # sits at (0, 0) and would otherwise win this comparison.
        top = min(c.GetRect().GetTop() for c in dlg.GetChildren()
                  if c.IsShown() and not isinstance(c, wx.StaticBox))
        assert dlg.remove_run_btn.GetRect().GetTop() == top
    finally:
        dlg.Destroy()

    placed, _ = _stitch(board)
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    try:
        assert dlg.remove_run_btn.IsEnabled()
        assert str(placed) in dlg.remove_run_btn.GetToolTipText()
        # Names the run beside the button, without the internal group prefix.
        assert dlg.remove_run_label.GetLabel() == f"GND F.Cu:B.Cu, {placed} vias"
        assert dlg.remove_run_label.GetRect().GetLeft() > dlg.remove_run_btn.GetRect().GetRight()
    finally:
        dlg.Destroy()


def test_reset_last_run_removes_the_newest_run_only():
    board, gnd = _board()
    # An earlier run, built by hand and added first: stitching twice in one
    # process isn't possible here, the parting refill wipes the zone fill on
    # KiCad 6 headless (see kicad-legacy-swig-api-gotchas). "Last run" means
    # last in board order, which is the most recently added one.
    older = geo.make_via(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu,
                         600000, 300000, gnd.GetNetCode(), 50 * MM, 50 * MM)
    board.Add(older)
    geo.group_vias(board, [older], vsl.GROUP_PREFIX + "GND older")
    placed, _ = _stitch(board)
    assert len(vsl.stitching_runs(board)) == 2

    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    original_msgbox = wx.MessageBox
    # _report opens a real, modal ErrorDialog. If the removal ever throws, an
    # unstubbed one would sit on the desktop waiting for a human to click OK.
    original_report = vsl._report
    reported = []
    try:
        vsl._report = lambda parent, summary, details: reported.append(summary)
        wx.MessageBox = lambda *a, **k: wx.NO
        dlg._on_remove_last_run()
        assert len(_vias(board)) == placed + 1, "answering No must remove nothing"

        wx.MessageBox = lambda *a, **k: wx.YES
        dlg._on_remove_last_run()
        left = _vias(board)
        assert len(left) == 1 and left[0].GetPosition().x == 50 * MM
        assert [g.GetName() for g, _ in vsl.stitching_runs(board)] == [vsl.GROUP_PREFIX + "GND older"]
        assert dlg.remove_run_btn.IsEnabled(), "the older run is still removable"

        dlg._on_remove_last_run()
        assert _vias(board) == []
        assert not dlg.remove_run_btn.IsEnabled()
        assert dlg.remove_run_label.GetLabel() == "No stitching run on this board"
    finally:
        wx.MessageBox = original_msgbox
        vsl._report = original_report
        dlg.Destroy()
    assert not reported, reported


def run():
    tests = [
        test_stitch_leaves_a_findable_run,
        test_grouped_vias_only_returns_that_groups_vias,
        test_removing_a_run_takes_its_vias_and_nothing_else,
        test_run_with_no_vias_left_is_not_offered,
        test_reset_last_run_button_tracks_board_state,
        test_reset_last_run_removes_the_newest_run_only,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print()
    print(f"{len(tests)}/{len(tests)} PASSED")


if __name__ == "__main__":
    run()
