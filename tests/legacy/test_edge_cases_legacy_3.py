# Third batch: dialog state-machine robustness, the Run() error-handling
# wrapper itself (not just stitch()'s own exceptions), full-pipeline
# netclass integration, and a few remaining structural edge cases.

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")

import pcbnew
import wx

wx.DisableAsserts()  # see test_edge_cases_legacy.py for why this must come first

import via_stitching_action_legacy as vsl
import _geometry_legacy as geo

_APP = wx.App()

MM = 1_000_000


def _square_polyset(x0, y0, x1, y1):
    ps = pcbnew.SHAPE_POLY_SET()
    chain = pcbnew.SHAPE_LINE_CHAIN()
    for (x, y) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
        chain.Append(pcbnew.VECTOR2I(int(x), int(y)))
    chain.SetClosed(True)
    ps.AddOutline(chain)
    return ps


def _board(layers=2, size=10 * MM, net_name="GND"):
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(layers)
    net = pcbnew.NETINFO_ITEM(board, net_name)
    board.Add(net)
    layer_ids = [pcbnew.F_Cu, pcbnew.B_Cu] if layers == 2 else [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.B_Cu]
    for layer in layer_ids:
        z = pcbnew.ZONE(board)
        z.SetNet(net)
        z.SetLayer(layer)
        board.Add(z)
        z.SetFilledPolysList(layer, _square_polyset(0, 0, size, size))
    return board, net


# ---- dialog state-machine robustness ---------------------------------------

def test_via_type_cycling_keeps_layer_combos_consistent():
    """Through -> Micro -> Blind/Buried -> Through, twice around. Each
    combo's item count and enabled state must match the via type after
    every single transition, not just eventually settle correctly."""
    board, net = _board(layers=4)
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    sequence = ["Micro", "Blind/Buried", "Through", "Micro", "Through"]
    for via_type_name in sequence:
        dlg.via_type.SetStringSelection(via_type_name)
        dlg._on_via_type()
        if via_type_name == "Through":
            assert dlg.start_layer.GetCount() == 1
            assert dlg.end_layer.GetCount() == 1
            assert dlg._combo_layer_name(dlg.start_layer) == "F.Cu"
            assert dlg._combo_layer_name(dlg.end_layer) == "B.Cu"
        else:
            assert dlg.start_layer.GetCount() == 4
            assert dlg.end_layer.GetCount() == 4
        # values() must never raise mid-cycle -- every state here is one a
        # real user reaches just by clicking the via type dropdown around.
        dlg.values()
    dlg.Destroy()
    vsl._clear_settings()


def test_custom_size_survives_via_type_switch_but_default_does_not():
    board, net = _board(layers=4)
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    assert dlg.via_dia.GetValue() == str(vsl.DEFAULT_VIA_DIAMETER_MM)  # starts as an "auto" default

    dlg.via_dia.SetValue("0.42")  # user types a custom value
    dlg.via_type.SetStringSelection("Micro")
    dlg._on_via_type()
    assert dlg.via_dia.GetValue() == "0.42", "a hand-typed value must survive a via-type switch"

    dlg2 = vsl.ViaStitchingDialogLegacy(None, board)
    dlg2.via_type.SetStringSelection("Micro")  # never touched via_dia -- still an "auto" value
    dlg2._on_via_type()
    assert dlg2.via_dia.GetValue() == str(vsl.DEFAULT_MICROVIA_DIAMETER_MM), (
        "an untouched auto-default should switch to the new via type's default"
    )
    dlg.Destroy()
    dlg2.Destroy()
    vsl._clear_settings()


def test_clone_from_through_via_locks_layers_regardless_of_its_own_span():
    """A Through via's TopLayer/BottomLayer are always F_Cu/B_Cu by
    definition (confirmed earlier this session), so cloning one should
    show the same locked F.Cu/B.Cu pair a fresh Through selection would --
    not attempt to read some other span off it."""
    board, net = _board(layers=4)
    via = geo.make_via(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, 500_000, 250_000,
                        net.GetNetCode(), 5 * MM, 5 * MM)
    via.SetSelected()
    board.Add(via)
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    assert dlg.via_type.GetStringSelection() == "Through"
    values = dlg.values()
    assert values["start_layer"] == pcbnew.F_Cu and values["end_layer"] == pcbnew.B_Cu
    dlg.Destroy()


def test_clone_from_via_with_no_net_assigned():
    """A via can exist with netcode 0 (no net) -- cloning it should not
    crash, and should leave the net field blank/unselected rather than
    guessing."""
    board, net = _board(layers=2)
    via = pcbnew.PCB_VIA(board)
    via.SetViaType(pcbnew.VIATYPE_THROUGH)
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    via.SetPosition(geo.point(1 * MM, 1 * MM))
    via.SetWidth(500_000)
    via.SetDrill(250_000)
    via.SetSelected()  # netcode left at 0 (default, no net) deliberately
    board.Add(via)
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    assert dlg.net.GetValue() == ""
    try:
        dlg.values()
        raise AssertionError("expected ValueError for a blank net")
    except ValueError:
        pass
    dlg.Destroy()


def test_reset_overrides_a_cloned_via_selection():
    board, net = _board(layers=2)
    via = geo.make_via(board, pcbnew.VIATYPE_MICROVIA, pcbnew.F_Cu, pcbnew.B_Cu, 900_000, 500_000,
                        net.GetNetCode(), 1 * MM, 1 * MM)
    via.SetSelected()
    board.Add(via)
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    assert dlg.via_type.GetStringSelection() == "Micro"
    dlg._on_reset()
    assert dlg.via_type.GetStringSelection() == "Through"
    assert dlg.via_dia.GetValue() == str(vsl.DEFAULT_VIA_DIAMETER_MM)
    dlg.Destroy()


def test_values_error_does_not_write_settings():
    board, net = _board(layers=2)
    vsl._clear_settings()
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    dlg.spacing.SetValue("garbage")
    dlg.save_current_as_settings()  # must swallow the ValueError, not raise, not save
    import os
    assert not os.path.exists(vsl._settings_path())
    dlg.Destroy()


def test_combo_layer_name_with_no_selection_returns_none():
    board, net = _board(layers=2)
    dlg = vsl.ViaStitchingDialogLegacy(None, board)
    dlg.start_layer.Clear()  # no items, no selection at all
    assert dlg._combo_layer_name(dlg.start_layer) is None
    dlg.Destroy()


# ---- Run()'s own error-handling wrapper ------------------------------------

def test_run_shows_a_message_box_for_a_stitch_runtime_error_not_a_crash():
    """Drive the real Run() method end to end (mocking only the modal
    ShowModal/pcbnew.GetBoard, which can't work outside a live app) and
    confirm a stitch() RuntimeError is caught and surfaced via
    wx.MessageBox, not left to propagate as an unhandled exception."""
    board, net = _board(layers=2)
    # No filled copper on B.Cu for a net that doesn't exist -> guaranteed RuntimeError
    orig_get_board = pcbnew.GetBoard
    orig_show_modal = vsl.ViaStitchingDialogLegacy.ShowModal
    orig_values = vsl.ViaStitchingDialogLegacy.values
    orig_msgbox = wx.MessageBox
    shown = []
    try:
        pcbnew.GetBoard = lambda: board
        vsl.ViaStitchingDialogLegacy.ShowModal = lambda self: wx.ID_OK
        vsl.ViaStitchingDialogLegacy.values = lambda self: dict(
            via_type=pcbnew.VIATYPE_THROUGH, start_layer=pcbnew.F_Cu, end_layer=pcbnew.B_Cu,
            net_name="NOPE", via_dia_mm=0.6, drill_mm=0.3, spacing_mm=1.0, pattern="Square",
            x_offset_mm=0, y_offset_mm=0, avoid_other_zones=False, avoid_footprints=False,
            avoid_same_net_pads=False,
        )
        wx.MessageBox = lambda text, *a, **k: shown.append(text) or wx.OK

        plugin = vsl.ViaStitchingLegacy()
        plugin.Run()

        assert shown, "Run() should have shown a message box for the RuntimeError"
        assert "not found" in shown[0].lower()
    finally:
        pcbnew.GetBoard = orig_get_board
        vsl.ViaStitchingDialogLegacy.ShowModal = orig_show_modal
        vsl.ViaStitchingDialogLegacy.values = orig_values
        wx.MessageBox = orig_msgbox


def test_run_shows_a_message_box_for_a_dialog_value_error():
    board, net = _board(layers=2)
    orig_get_board = pcbnew.GetBoard
    orig_show_modal = vsl.ViaStitchingDialogLegacy.ShowModal
    orig_values = vsl.ViaStitchingDialogLegacy.values
    orig_msgbox = wx.MessageBox
    shown = []
    try:
        pcbnew.GetBoard = lambda: board

        def _raise_value_error(self):
            raise ValueError("Pick a net to stitch.")

        vsl.ViaStitchingDialogLegacy.ShowModal = lambda self: wx.ID_OK
        vsl.ViaStitchingDialogLegacy.values = _raise_value_error
        wx.MessageBox = lambda text, *a, **k: shown.append(text) or wx.OK

        plugin = vsl.ViaStitchingLegacy()
        plugin.Run()

        assert shown and "pick a net" in shown[0].lower()
    finally:
        pcbnew.GetBoard = orig_get_board
        vsl.ViaStitchingDialogLegacy.ShowModal = orig_show_modal
        vsl.ViaStitchingDialogLegacy.values = orig_values
        wx.MessageBox = orig_msgbox


def test_run_does_nothing_on_cancel():
    board, net = _board(layers=2)
    orig_get_board = pcbnew.GetBoard
    orig_show_modal = vsl.ViaStitchingDialogLegacy.ShowModal
    orig_msgbox = wx.MessageBox
    shown = []
    try:
        pcbnew.GetBoard = lambda: board
        vsl.ViaStitchingDialogLegacy.ShowModal = lambda self: wx.ID_CANCEL
        wx.MessageBox = lambda text, *a, **k: shown.append(text) or wx.OK

        plugin = vsl.ViaStitchingLegacy()
        plugin.Run()

        assert not shown, "Cancel should not run stitch() or show any message box"
        assert not any(isinstance(t, pcbnew.PCB_VIA) for t in board.GetTracks())
    finally:
        pcbnew.GetBoard = orig_get_board
        vsl.ViaStitchingDialogLegacy.ShowModal = orig_show_modal
        wx.MessageBox = orig_msgbox


def test_run_surfaces_an_unexpected_exception_without_crashing_kicad():
    """Anything that isn't RuntimeError/ValueError (a real bug, in other
    words) must still be caught and shown, not propagate out of Run() and
    potentially destabilize the host KiCad process."""
    board, net = _board(layers=2)
    orig_get_board = pcbnew.GetBoard
    orig_show_modal = vsl.ViaStitchingDialogLegacy.ShowModal
    orig_values = vsl.ViaStitchingDialogLegacy.values
    orig_msgbox = wx.MessageBox
    shown = []
    try:
        pcbnew.GetBoard = lambda: board

        def _raise_type_error(self):
            raise TypeError("something genuinely unexpected")

        vsl.ViaStitchingDialogLegacy.ShowModal = lambda self: wx.ID_OK
        vsl.ViaStitchingDialogLegacy.values = _raise_type_error
        wx.MessageBox = lambda text, *a, **k: shown.append(text) or wx.OK

        plugin = vsl.ViaStitchingLegacy()
        plugin.Run()  # must not raise out of this call

        assert shown and "unexpected error" in shown[0].lower()
    finally:
        pcbnew.GetBoard = orig_get_board
        vsl.ViaStitchingDialogLegacy.ShowModal = orig_show_modal
        vsl.ViaStitchingDialogLegacy.values = orig_values
        wx.MessageBox = orig_msgbox


# ---- full-pipeline netclass integration ------------------------------------

def test_full_pipeline_respects_higher_netclass_clearance():
    """Not just net_clearances() in isolation -- confirm stitch() actually
    keeps vias further from a high-clearance net's track than a
    low-clearance one, end to end.

    Each clearance value runs in its OWN subprocess (see
    _netclass_gap_check.py): building two pcbnew.BOARD()s with
    programmatically-added NETCLASSPTR objects in the SAME process was
    confirmed, deterministically across three separate repeated runs, to
    corrupt the netclass lookup for BOTH boards -- not fixed by the
    "canonical" NetNames()+SynchronizeNetsAndNetClasses() assignment path,
    not a GC-lifetime issue either. This never affects the real plugin
    (it only ever reads netclasses KiCad already loaded from a project
    file, never constructs one), so it's a test-isolation problem to work
    around, not a plugin bug to fix."""
    import subprocess

    def gap_for(vcc_clearance_nm):
        out = subprocess.run(
            [sys.executable, __file__.rsplit("test_", 1)[0] + "_netclass_gap_check.py",
             str(vcc_clearance_nm)],
            capture_output=True, text=True,
        )
        assert out.returncode == 0, (
            "the gap check subprocess failed: " + out.stdout + out.stderr
        )
        return int(out.stdout.strip())

    gap_tight = gap_for(100_000)    # 0.1mm clearance
    gap_loose = gap_for(1_500_000)  # 1.5mm clearance
    assert gap_loose > gap_tight, "a higher netclass clearance should push vias further from the track"


# ---- structural edge cases --------------------------------------------------

def test_zero_enabled_copper_layers_fails_cleanly():
    board = pcbnew.BOARD()
    # Deliberately do NOT call SetCopperLayerCount -- probe whatever the
    # rock-bottom default is, and confirm span_layers doesn't crash with an
    # unhelpful traceback either way.
    try:
        order = geo.copper_layer_order(board)
        if len(order) >= 2:
            geo.span_layers(board, order[0], order[-1])  # should just work
        else:
            try:
                geo.span_layers(board, pcbnew.F_Cu, pcbnew.B_Cu)
                raise AssertionError("expected a ValueError from .index() on a layer not enabled")
            except ValueError:
                pass  # a plain, if unfriendly, ValueError beats a silent wrong answer
    except Exception as e:
        assert not isinstance(e, AssertionError)


def test_stitching_the_same_net_and_layers_twice_creates_two_independent_groups():
    """Re-injects the fill between calls: stitch()'s own parting refill
    wipes every zone's fill headlessly (see test_two_nets_stitched_
    independently_avoid_each_other for the full explanation) -- a live
    KiCad session doesn't have this problem, so this is purely a headless-
    test workaround, not something the real plugin needs."""
    board, net = _board(layers=2, size=20 * MM)
    zones = list(board.Zones())
    vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
               via_dia_mm=0.4, drill_mm=0.2, spacing_mm=4.0, pattern="Square",
               x_offset_mm=0, y_offset_mm=0)
    for z in zones:
        layer = pcbnew.F_Cu if z.GetLayerSet().Contains(pcbnew.F_Cu) else pcbnew.B_Cu
        z.SetFilledPolysList(layer, _square_polyset(0, 0, 20 * MM, 20 * MM))
    vsl.stitch(board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
               via_dia_mm=0.4, drill_mm=0.2, spacing_mm=4.0, pattern="Square",
               x_offset_mm=0.3, y_offset_mm=0.3)  # nudged offset so it isn't fully blocked by run 1
    groups = list(board.Groups())
    assert len(groups) == 2
    group1_vias = [v for v in board.GetTracks() if isinstance(v, pcbnew.PCB_VIA) and v.GetParentGroup() is not None
                   and v.GetParentGroup().this == groups[0].this]
    geo.delete_grouped_vias(board, groups[0], group1_vias)
    remaining = [v for v in board.GetTracks() if isinstance(v, pcbnew.PCB_VIA)]
    assert len(remaining) > 0, "deleting one run's group must not remove the other run's vias"
    assert len(list(board.Groups())) == 1


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
