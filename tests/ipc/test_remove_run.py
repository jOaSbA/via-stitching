# Tests for "Reset last run" on the IPC backend, the mirror of
# tests/legacy/test_remove_run_legacy.py.
#
# Offline against a fake board, unlike the legacy suite, which needs the real
# SWIG bindings because a group there cannot be asked for its members at all.
# Here it can: a Group carries its member KIIDs, so the only thing worth
# faking is how KiCad answers when it is asked about them -- including the
# refusal that made board.get_groups() unusable in the first place.
#
# Run with the plugin's own venv interpreter, which already has wx, kipy and
# shapely:
#   "$LOCALAPPDATA/KiCad/10.0/python-environments/com.github.jOaSbA.via-stitching/Scripts/python" tests/ipc/test_remove_run.py
#
# License: GPL-3.0-or-later

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugins"))

import wx  # noqa: E402
from kipy.board_types import BoardLayer, Group, Via  # noqa: E402
from kipy.errors import ApiError  # noqa: E402
from kipy.proto.common.types import KiCadObjectType  # noqa: E402

import via_stitching_action as vs  # noqa: E402
from via_stitching_action import GROUP_PREFIX, ViaStitchingDialog, stitching_runs  # noqa: E402

_APP = wx.App()  # one per process, see test_dialog_layout.py

vs._load_settings = lambda: {}  # never read the user's real settings file

LAYERS = {BoardLayer.BL_F_Cu: "F.Cu", BoardLayer.BL_B_Cu: "B.Cu"}


def _via(uuid):
    via = Via()
    via.proto.id.value = uuid
    return via


def _group(name, vias):
    group = Group()
    group.proto.id.value = "group-" + name
    group.proto.name = name
    group.items = vias
    return group


class FakeBoard:
    """Just the four calls stitching_runs() and the removal make."""

    def __init__(self, groups=(), broken=()):
        self.groups = list(groups)
        self.broken = set(broken)  # group names KiCad refuses to unwrap
        self.items = {v.id.value: v for g in self.groups for v in g.items}
        self.refilled = 0

    # -- the board API the plugin uses --
    def get_items(self, types):
        assert types == KiCadObjectType.KOT_PCB_GROUP, types
        return list(self.groups)

    def get_items_by_id(self, ids):
        for group in self.groups:
            if group.proto.name in self.broken and list(group.proto.items) == list(ids):
                raise ApiError("dangling member")
        return [self.items[i.value] for i in ids if i.value in self.items]

    def remove_items(self, items):
        for item in items:
            self.items.pop(item.id.value, None)
            self.groups = [g for g in self.groups if g.id.value != item.id.value]

    def refill_zones(self, block=True):
        self.refilled += 1

    # -- the rest of what building the dialog needs --
    def get_enabled_layers(self):
        return list(LAYERS)

    def get_layer_name(self, layer):
        return LAYERS[layer]

    def get_selection(self, kind):
        return []


def _run_board(count=3, extra=()):
    vias = [_via("v%d" % n) for n in range(count)]
    groups = [_group(GROUP_PREFIX + "GND F.Cu:B.Cu", vias)] + list(extra)
    return FakeBoard(groups)


def _dialog(board):
    return ViaStitchingDialog(None, ["GND"], board)


def test_only_this_plugins_groups_count_as_runs():
    board = _run_board(extra=[_group("Panel outline", [_via("other")])])
    runs = stitching_runs(board)
    assert len(runs) == 1, [g.proto.name for g, _ in runs]
    group, vias = runs[0]
    assert group.proto.name == GROUP_PREFIX + "GND F.Cu:B.Cu"
    assert len(vias) == 3


def test_run_with_no_vias_left_is_not_offered():
    # Deleting the vias by hand leaves the group behind. Offering that as a
    # removable run would be a button that does nothing.
    board = _run_board()
    board.items.clear()
    assert stitching_runs(board) == []


def test_one_unreadable_group_does_not_hide_the_rest():
    # The reason this does not use board.get_groups(): that unwraps every
    # group in one call, so a single group with a dangling member takes the
    # whole list down with it.
    broken = _group(GROUP_PREFIX + "GND broken", [_via("gone")])
    board = FakeBoard([broken] + _run_board().groups, broken=[broken.proto.name])
    names = [g.proto.name for g, _ in stitching_runs(board)]
    assert names == [GROUP_PREFIX + "GND F.Cu:B.Cu"], names


def test_button_tracks_board_state():
    dlg = _dialog(FakeBoard())
    try:
        assert not dlg.remove_run_btn.IsEnabled(), "nothing stitched yet"
        assert dlg.remove_run_label.GetLabel() == "No stitching run on this board"
        # It sits above everything else. Hidden controls are skipped: the
        # advisory label is Hide()n, never laid out, and sits at (0, 0).
        top = min(c.GetRect().GetTop() for c in dlg.GetChildren()
                  if c.IsShown() and not isinstance(c, wx.StaticBox))
        assert dlg.remove_run_btn.GetRect().GetTop() == top
    finally:
        dlg.Destroy()

    dlg = _dialog(_run_board())
    try:
        assert dlg.remove_run_btn.IsEnabled()
        assert "3" in dlg.remove_run_btn.GetToolTipText()
        # Names the run beside the button, without the internal group prefix.
        assert dlg.remove_run_label.GetLabel() == "GND F.Cu:B.Cu, 3 vias"
        assert dlg.remove_run_label.GetRect().GetLeft() > dlg.remove_run_btn.GetRect().GetRight()
    finally:
        dlg.Destroy()


def test_removing_takes_the_run_its_group_and_nothing_else():
    stray = _group("Panel outline", [_via("other")])
    board = _run_board(extra=[stray])
    dlg = _dialog(board)

    original_msgbox = wx.MessageBox
    # _report opens a real modal ErrorDialog. Unstubbed, a failure in here
    # would leave one sitting on the desktop waiting for a human.
    original_report = vs._report
    reported = []
    try:
        vs._report = lambda parent, summary, exc: reported.append(summary)

        wx.MessageBox = lambda *a, **k: wx.NO
        dlg._on_remove_last_run()
        assert len(board.items) == 4, "answering No must remove nothing"
        assert board.refilled == 0

        wx.MessageBox = lambda *a, **k: wx.YES
        dlg._on_remove_last_run()
        assert list(board.items) == ["other"], board.items
        assert [g.proto.name for g in board.groups] == ["Panel outline"], "the empty group goes too"
        assert board.refilled == 1, "the zones have holes in them until they are refilled"

        assert not dlg.remove_run_btn.IsEnabled()
        assert dlg.remove_run_label.GetLabel() == "No stitching run on this board"
    finally:
        wx.MessageBox = original_msgbox
        vs._report = original_report
        dlg.Destroy()
    assert not reported, reported


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
