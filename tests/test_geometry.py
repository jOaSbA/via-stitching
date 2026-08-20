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

from via_stitching_action import (  # noqa: E402
    FALLBACK_CLEARANCE_MM,
    _blocked_predicate,
    _fallback_clearances,
    _footprint_keepout_shapes,
    _grid_points,
    _keepout_shapes,
    _make_via,
    _net_clearances,
    _nudge_radius,
    _nudged,
    _pad_copper_keepout_shapes,
    _rule_area_keepout_shapes,
    _track_keepout_shapes,
    _zone_keepout_shapes,
    stitch,
)

MM = from_mm(1.0)
BOX = (0, 0, 10 * MM, 10 * MM)


def SimpleNamespaceXY(x, y):
    from types import SimpleNamespace

    return SimpleNamespace(x=x, y=y)


def test_square_grid():
    pts = list(_grid_points(BOX, from_mm(2.0), "Square"))
    assert len(pts) == 36, len(pts)  # 6 x 6 inclusive of both edges
    assert all(x % from_mm(2.0) == 0 and y % from_mm(2.0) == 0 for x, y in pts)


def test_hexagonal_grid():
    spacing = from_mm(2.0)
    pts = list(_grid_points(BOX, spacing, "Hexagonal"))
    rows = sorted({y for _, y in pts})
    # round(), not int(): truncation biased the row pitch slightly short.
    assert rows[1] - rows[0] == round(spacing * math.sqrt(3) / 2)
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
    via = _make_via(
        1000, 2000, ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu,
        from_mm(0.6), from_mm(0.3), Net(),
    )
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


def test_make_via_microvia_spans_given_layers():
    # Regression: only VT_THROUGH gets its drill span auto-filled by kipy's
    # own Via.type setter, so a microvia's start/end layers must be set from
    # what was actually asked for.
    via = _make_via(
        1000, 2000, ViaType.VT_MICRO, BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu,
        from_mm(0.25), from_mm(0.1), Net(),
    )
    assert via.type == ViaType.VT_MICRO
    assert via.padstack.drill.start_layer == BoardLayer.BL_F_Cu
    assert via.padstack.drill.end_layer == BoardLayer.BL_In1_Cu
    assert via.drill_diameter == 100000


def test_keepout_shapes_use_drill_not_copper():
    # Regression: _keepout_shapes must size clearance off the hole (drill), not the
    # copper pad/annular ring, or hole-to-hole spacing comes out far too generous.
    from types import SimpleNamespace

    from kipy.board_types import PadType

    full_span = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]

    def through_via(diameter, drill_diameter):
        return SimpleNamespace(
            position=SimpleNamespace(x=0, y=0),
            diameter=diameter,
            drill_diameter=drill_diameter,
            padstack=SimpleNamespace(
                drill=SimpleNamespace(
                    start_layer=BoardLayer.BL_F_Cu, end_layer=BoardLayer.BL_B_Cu
                )
            ),
        )

    via = through_via(from_mm(2.0), from_mm(0.3))  # large copper, small drill
    board = SimpleNamespace(
        get_vias=lambda: [via], get_pads=lambda: [], get_enabled_layers=lambda: full_span
    )
    shapes = _keepout_shapes(board, from_mm(0.15), full_span)
    assert len(shapes) == 1
    # bounds is a square [-r, -r, r, r]; r == via_radius + drill_radius + margin
    minx, miny, maxx, maxy = shapes[0].bounds
    expected_r = from_mm(0.15) + from_mm(0.15) + from_mm(0.25)
    assert abs(maxx - expected_r) < from_mm(0.01), (maxx, expected_r)

    def pth_pad(drill_x, drill_y):
        # A real kipy drill diameter is a Vector2, always carrying both axes.
        return SimpleNamespace(
            pad_type=PadType.PT_PTH,
            position=SimpleNamespace(x=0, y=0),
            padstack=SimpleNamespace(
                drill=SimpleNamespace(
                    diameter=SimpleNamespace(x=drill_x, y=drill_y)
                )
            ),
        )

    round_pad = pth_pad(from_mm(0.3), from_mm(0.3))
    board_pad_only = SimpleNamespace(get_vias=lambda: [], get_pads=lambda: [round_pad])
    shapes2 = _keepout_shapes(board_pad_only, from_mm(0.15), full_span)
    _, _, maxx2, _ = shapes2[0].bounds
    assert abs(maxx2 - expected_r) < from_mm(0.01), (maxx2, expected_r)

    # A milled slot is a drill too, and gets a capsule along its long axis: tight
    # across the slot, still covering the far end. Sizing it off the short axis
    # would leave a via sitting on the end of the slot; a circle as wide as the
    # slot is long (what this used to do) blocked a far larger disc than the hole.
    slot_pad = pth_pad(from_mm(0.3), from_mm(3.0))
    board_slot = SimpleNamespace(get_vias=lambda: [], get_pads=lambda: [slot_pad])
    _, _, maxx3, maxy3 = _keepout_shapes(
        board_slot, from_mm(0.15), full_span
    )[0].bounds
    across = from_mm(0.15) + from_mm(0.15) + from_mm(0.25)  # via + short axis + margin
    along = from_mm(1.5) - from_mm(0.15) + across  # plus half the slot's travel
    assert abs(maxx3 - across) < from_mm(0.01), (maxx3, across)
    assert abs(maxy3 - along) < from_mm(0.01), (maxy3, along)

    # Regression: a via whose span doesn't overlap the new via's span must not
    # count as a keepout (e.g. a front microvia stack when placing a back one).
    disjoint_via = SimpleNamespace(
        position=SimpleNamespace(x=0, y=0),
        diameter=from_mm(0.25),
        drill_diameter=from_mm(0.1),
        padstack=SimpleNamespace(
            drill=SimpleNamespace(
                start_layer=BoardLayer.BL_In2_Cu, end_layer=BoardLayer.BL_B_Cu
            )
        ),
    )
    board_disjoint = SimpleNamespace(
        get_vias=lambda: [disjoint_via],
        get_pads=lambda: [],
        get_enabled_layers=lambda: [
            BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu, BoardLayer.BL_In2_Cu, BoardLayer.BL_B_Cu,
        ],
    )
    front_span = [BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu]
    assert _keepout_shapes(board_disjoint, from_mm(0.15), front_span) == []


def test_blocked_predicate_without_shapes_blocks_nothing():
    # An empty board must not need special-casing at every call site.
    assert not _blocked_predicate([])(0, 0)


def test_net_clearances_takes_the_larger_netclass_value():
    # KiCad resolves a pair's clearance to the larger of the two netclasses, so a
    # 1.5 mm high-voltage net has to push stitching vias further away than a
    # signal net that just inherits the board minimum.
    from types import SimpleNamespace

    nets = [SimpleNamespace(name=n) for n in ("GND", "HV", "SIG", "ZEROCLASS")]
    classes = {
        "GND": SimpleNamespace(clearance=from_mm(0.1)),
        "HV": SimpleNamespace(clearance=from_mm(1.5)),
        "SIG": SimpleNamespace(clearance=None),  # inherits the board minimum
        "ZEROCLASS": SimpleNamespace(clearance=0),  # explicitly zero, not unset
    }
    board = SimpleNamespace(get_netclass_for_nets=lambda n: classes)

    clearances = _net_clearances(board, nets, "GND")
    assert clearances["HV"] == from_mm(1.5)
    # Unset means "inherits the board minimum", which IPC won't tell us: fallback.
    assert clearances["SIG"] == from_mm(FALLBACK_CLEARANCE_MM)
    # Zero is a real value, so the stitched net's own clearance is what wins. A
    # falsy-vs-None mix-up here would hand back the 0.2 mm fallback instead.
    assert clearances["ZEROCLASS"] == from_mm(0.1)
    # A net KiCad said nothing about still answers, with the fallback.
    assert clearances["NOT_ON_THIS_BOARD"] == from_mm(FALLBACK_CLEARANCE_MM)

    # KiCad refusing the call is not fatal: every net falls back.
    def refuse(_nets):
        raise RuntimeError("no netclasses for you")

    broken = SimpleNamespace(get_netclass_for_nets=refuse)
    assert _net_clearances(broken, nets, "GND")["HV"] == from_mm(FALLBACK_CLEARANCE_MM)


def test_track_keepout_skips_same_net_blocks_others():
    # Regression: a through via's drill spans every copper layer, so a track on
    # a layer the stitched net never pours on must still block placement, as
    # long as that layer is inside the via's own span.
    from types import SimpleNamespace

    from kipy.board_types import BoardLayer

    same_net_track = SimpleNamespace(
        net=SimpleNamespace(name="GND"),
        layer=BoardLayer.BL_F_Cu,
        start=SimpleNamespace(x=0, y=0),
        end=SimpleNamespace(x=from_mm(5.0), y=0),
        width=from_mm(0.2),
    )
    other_net_track = SimpleNamespace(
        net=SimpleNamespace(name="SIG"),
        layer=BoardLayer.BL_F_Cu,
        start=SimpleNamespace(x=0, y=from_mm(1.0)),
        end=SimpleNamespace(x=from_mm(5.0), y=from_mm(1.0)),
        width=from_mm(0.2),
    )
    board = SimpleNamespace(get_tracks=lambda: [same_net_track, other_net_track])
    full_span = [BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu, BoardLayer.BL_B_Cu]

    blocked = _blocked_predicate(
        _track_keepout_shapes(board, "GND", from_mm(0.3), _fallback_clearances(), full_span)
    )

    # On the same net: not a keepout, even directly on the track.
    assert not blocked(from_mm(2.5), 0)
    # On another net, on a layer the via's span does cover: blocked.
    assert blocked(from_mm(2.5), from_mm(1.0))
    assert not blocked(from_mm(2.5), from_mm(5.0))

    # Regression (issue #7): a microvia or blind/buried via only spans some of
    # the board's layers, so a track outside that span must not block it, even
    # though a through via would have to avoid it.
    narrow_span = [BoardLayer.BL_In1_Cu, BoardLayer.BL_B_Cu]  # excludes F.Cu
    not_blocked = _blocked_predicate(
        _track_keepout_shapes(board, "GND", from_mm(0.3), _fallback_clearances(), narrow_span)
    )
    assert not not_blocked(from_mm(2.5), from_mm(1.0))


def test_zone_keepout_skips_same_net_blocks_others():
    # Regression: a through via's drill spans every copper layer, so a filled
    # zone for a different net on a layer `net_name` never pours on (an inner
    # power plane under a GND-poured outer layer, say) must still block it.
    from types import SimpleNamespace

    from kipy.board_types import BoardLayer

    def node(x, y):
        return SimpleNamespace(has_point=True, has_arc=False, point=SimpleNamespace(x=x, y=y))

    def square_pwh(x0, y0, x1, y1):
        outline = SimpleNamespace(
            nodes=[node(x0, y0), node(x1, y0), node(x1, y1), node(x0, y1)]
        )
        return SimpleNamespace(outline=outline, holes=[])

    same_net_zone = SimpleNamespace(
        net=SimpleNamespace(name="GND"),
        filled_polygons={BoardLayer.BL_F_Cu: [square_pwh(0, 0, from_mm(5.0), from_mm(5.0))]},
    )
    other_net_zone = SimpleNamespace(
        net=SimpleNamespace(name="3V3"),
        filled_polygons={
            BoardLayer.BL_In1_Cu: [
                square_pwh(from_mm(10.0), 0, from_mm(15.0), from_mm(5.0))
            ]
        },
    )
    full_span = [BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu, BoardLayer.BL_B_Cu]

    blocked = _blocked_predicate(
        _zone_keepout_shapes(
            [same_net_zone, other_net_zone], "GND", from_mm(0.3), _fallback_clearances(),
            full_span,
        )
    )

    # Same net: not a keepout, even well inside its own zone.
    assert not blocked(from_mm(2.5), from_mm(2.5))
    # Other net, on a layer GND never pours on but the via's span does cover:
    # blocked, since a through via drills through it regardless of layer.
    assert blocked(from_mm(12.5), from_mm(2.5))
    assert not blocked(from_mm(30.0), from_mm(2.5))

    # Regression (issue #7): a microvia or blind/buried via only spans some of
    # the board's layers, so a zone filled outside that span must not block
    # it, even though a through via would have to avoid it.
    narrow_span = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]  # excludes In1.Cu
    not_blocked = _blocked_predicate(
        _zone_keepout_shapes(
            [same_net_zone, other_net_zone], "GND", from_mm(0.3), _fallback_clearances(),
            narrow_span,
        )
    )
    assert not not_blocked(from_mm(12.5), from_mm(2.5))


def test_rule_area_keepout_blocks_via_keepouts_only():
    # Regression: a "no vias" rule area is an explicit instruction from whoever
    # laid the board out, and nothing used to consult it at all.
    from types import SimpleNamespace

    from kipy.board_types import BoardLayer

    # Pin the proto field this reads through: kipy 0.7.1 wraps a zone's copper
    # settings but not its RuleAreaSettings, so a rename there would silently
    # switch the whole keepout off.
    from kipy.proto.board.board_types_pb2 import RuleAreaSettings

    assert "keepout_vias" in RuleAreaSettings.DESCRIPTOR.fields_by_name

    def node(x, y):
        return SimpleNamespace(has_point=True, has_arc=False, point=SimpleNamespace(x=x, y=y))

    def rule_area(x0, y0, x1, y1, keepout_vias, layers=(BoardLayer.BL_F_Cu,)):
        outline = SimpleNamespace(
            nodes=[node(x0, y0), node(x1, y0), node(x1, y1), node(x0, y1)]
        )
        pwh = SimpleNamespace(outline=outline, holes=[])
        return SimpleNamespace(
            is_rule_area=lambda: True,
            outline=pwh,
            layers=layers,
            proto=SimpleNamespace(
                rule_area_settings=SimpleNamespace(keepout_vias=keepout_vias),
                outline=SimpleNamespace(polygons=[pwh]),
            ),
        )

    no_vias = rule_area(0, 0, from_mm(5.0), from_mm(5.0), True)
    other_restriction = rule_area(
        from_mm(10.0), 0, from_mm(15.0), from_mm(5.0), False
    )
    copper_zone = SimpleNamespace(is_rule_area=lambda: False)
    full_span = [BoardLayer.BL_F_Cu, BoardLayer.BL_In1_Cu, BoardLayer.BL_B_Cu]

    blocked = _blocked_predicate(
        _rule_area_keepout_shapes(
            [no_vias, other_restriction, copper_zone], from_mm(0.3), full_span
        )
    )

    assert blocked(from_mm(2.5), from_mm(2.5))
    # A rule area that restricts something else (tracks, footprints) must not
    # stop vias going in.
    assert not blocked(from_mm(12.5), from_mm(2.5))
    assert not blocked(from_mm(30.0), from_mm(2.5))

    # The via's copper, not just its centre, has to clear the boundary.
    assert blocked(from_mm(5.0) + from_mm(0.2), from_mm(2.5))
    assert not blocked(from_mm(5.0) + from_mm(0.4), from_mm(2.5))

    # A rule area KiCad handed over without an outline must not raise.
    empty = rule_area(0, 0, from_mm(1.0), from_mm(1.0), True)
    empty.proto.outline.polygons = []
    assert _rule_area_keepout_shapes([empty], from_mm(0.3), full_span) == []

    # Regression (issue #7): a microvia or blind/buried via only spans some of
    # the board's layers, so a rule area drawn entirely outside that span must
    # not block it, even though a through via would have to avoid it.
    inner_only = rule_area(
        0, 0, from_mm(5.0), from_mm(5.0), True, layers=(BoardLayer.BL_In1_Cu,)
    )
    narrow_span = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]  # excludes In1.Cu
    not_blocked = _blocked_predicate(
        _rule_area_keepout_shapes([inner_only], from_mm(0.3), narrow_span)
    )
    assert not not_blocked(from_mm(2.5), from_mm(2.5))


def test_footprint_keepout_covers_bounding_box():
    # Regression: vias must stay clear of a component's footprint bounding
    # box, independent of net, since this is a mechanical fit concern.
    from types import SimpleNamespace

    bbox = SimpleNamespace(
        pos=SimpleNamespace(x=0, y=0),
        size=SimpleNamespace(x=from_mm(5.0), y=from_mm(5.0)),
    )
    board = SimpleNamespace(
        get_footprints=lambda: [object()],
        get_item_bounding_box=lambda footprints: [bbox],
    )

    args = ("GND", from_mm(0.3), _fallback_clearances())
    blocked = _blocked_predicate(_footprint_keepout_shapes(board, *args))
    assert blocked(from_mm(2.5), from_mm(2.5))
    assert not blocked(from_mm(20.0), from_mm(20.0))

    # No footprints on the board: no shapes, and nothing downstream cares.
    empty_board = SimpleNamespace(get_footprints=lambda: [])
    assert _footprint_keepout_shapes(empty_board, *args) == []

    # KiCad hands back no box for some items. Fewer boxes than footprints must
    # still produce the keepouts it did answer for, not raise.
    short_board = SimpleNamespace(
        get_footprints=lambda: [object(), object()],
        get_item_bounding_box=lambda footprints: [bbox],
    )
    assert len(_footprint_keepout_shapes(short_board, *args)) == 1


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


def test_is_busy_recognises_kicads_busy_status():
    # Pinned against real kipy types: this is how the plugin tells "KiCad is
    # mid-operation" (usually our own parting refill) from "no board open".
    from kipy.errors import ApiError
    from kipy.proto.common import ApiStatusCode

    from via_stitching_action import _is_busy

    assert _is_busy(ApiError("busy", code=ApiStatusCode.AS_BUSY))
    assert not _is_busy(ApiError("nope", code=ApiStatusCode.AS_BAD_REQUEST))
    assert not _is_busy(ApiError("default code"))
    assert not _is_busy(KiCadConnectionError("Failed to connect to KiCad"))
    assert not _is_busy(RuntimeError("something else entirely"))


def test_dialogs_build():
    # The error dialog is the one thing that must never fail, so build it for real.
    from types import SimpleNamespace

    import wx

    from via_stitching_action import ErrorDialog, ViaStitchingDialog

    app = wx.App()  # noqa: F841

    err = ErrorDialog(None, "summary line", "line one\nline two")
    try:
        assert err.GetTitle() == "Via Stitching Error"
    finally:
        err.Destroy()

    fake_layers = {
        BoardLayer.BL_F_Cu: "F.Cu",
        BoardLayer.BL_In1_Cu: "In1.Cu",
        BoardLayer.BL_In2_Cu: "In2.Cu",
        BoardLayer.BL_B_Cu: "B.Cu",
    }
    fake_board = SimpleNamespace(
        get_enabled_layers=lambda: list(fake_layers.keys()),
        get_layer_name=lambda layer: fake_layers[layer],
        get_selection=lambda kind: [],
    )

    dlg = ViaStitchingDialog(None, ["GND", "VCC"], fake_board)
    try:
        assert dlg.values()["net_name"] == "GND"

        for field, bad in (
            (dlg.spacing, "0"),
            (dlg.spacing, "-1"),
            (dlg.spacing, "abc"),
            (dlg.drill, "0.6"),  # drill >= via diameter
            # float() accepts these, and every nan comparison is False, so they
            # sail past the >0 and drill<diameter checks and die inside from_mm.
            (dlg.spacing, "nan"),
            (dlg.via_dia, "nan"),
            (dlg.spacing, "inf"),
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


def _fake_board(pads=(), vias=(), tracks=(), size_mm=20.0, nets=("GND", "SIG")):
    """Enough of a board for stitch() to run end to end, offline.

    Two copper layers with the whole square poured on `nets[0]`, and whatever
    pads/vias/tracks the caller wants in the way. create_items echoes back what
    it was handed, which is what the real API does.
    """
    from types import SimpleNamespace

    def node(x, y):
        return SimpleNamespace(
            has_point=True, has_arc=False, point=SimpleNamespace(x=x, y=y)
        )

    side = from_mm(size_mm)
    outline = SimpleNamespace(
        nodes=[node(0, 0), node(side, 0), node(side, side), node(0, side)]
    )
    pour = [SimpleNamespace(outline=outline, holes=[])]
    zone = SimpleNamespace(
        net=SimpleNamespace(name=nets[0]),
        is_rule_area=lambda: False,
        filled_polygons={BoardLayer.BL_F_Cu: pour, BoardLayer.BL_B_Cu: pour},
    )

    created = []

    def create_items(items):
        made = items if isinstance(items, list) else [items]
        created.extend(made)
        return made

    return SimpleNamespace(
        get_nets=lambda: [Net(name=n) for n in nets],
        get_zones=lambda: [zone],
        get_enabled_layers=lambda: [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu],
        get_layer_name=lambda l: {BoardLayer.BL_F_Cu: "F.Cu",
                                  BoardLayer.BL_B_Cu: "B.Cu"}[l],
        get_netclass_for_nets=lambda n: {},
        get_vias=lambda: list(vias),
        get_pads=lambda: list(pads),
        get_tracks=lambda: list(tracks),
        create_items=create_items,
        get_items_by_id=lambda ids: list(ids),
        refill_zones=lambda block=True: None,
        placed=created,
    )


def test_stitch_places_a_clean_grid():
    # The only test that drives the whole pipeline. Nothing below asserts a
    # placement *quality*; they assert the invariants that a via array has to
    # satisfy to be manufacturable, and they are what catches a helper whose
    # signature or return type drifted out of step with its caller.
    from itertools import combinations

    from via_stitching_action import HOLE_MARGIN_MM

    drill_mm, dia_mm, spacing_mm = 0.3, 0.6, 2.0
    pad = _smd_pad("SIG", from_mm(2.0), from_mm(2.0))
    pad.position = SimpleNamespaceXY(from_mm(10.0), from_mm(10.0))
    board = _fake_board(pads=[pad])

    count, _grouped = stitch(
        board, ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, "GND",
        dia_mm, drill_mm, spacing_mm, "Square", 0.0, 0.0,
    )
    assert count > 50, count

    points = [(v.position.x, v.position.y) for v in board.placed if hasattr(v, "position")]
    assert len(points) == count
    assert len(set(points)) == count, "duplicate via positions"

    # Every via has to stay inside the pour, inset by its own radius.
    for x, y in points:
        assert from_mm(0.3) <= x <= from_mm(19.7), (x, y)
        assert from_mm(0.3) <= y <= from_mm(19.7), (x, y)

    # Nothing may land on the SIG pad's copper plus its clearance.
    reach = from_mm(1.0) + from_mm(0.3) + from_mm(FALLBACK_CLEARANCE_MM)
    for x, y in points:
        assert max(abs(x - from_mm(10.0)), abs(y - from_mm(10.0))) >= reach - 1, (x, y)

    # And no two of them may crowd each other's holes, nudged or not. The bound
    # itself is covered by test_nudge_radius_keeps_hole_margin_between_neighbours;
    # this is the cheap end-to-end version of the same invariant, over the vias a
    # real run actually produced rather than over nominal grid positions.
    floor = from_mm(drill_mm) + from_mm(HOLE_MARGIN_MM)
    close = [
        (a, b) for a, b in combinations(points, 2)
        if math.hypot(a[0] - b[0], a[1] - b[1]) < floor
    ]
    assert not close, close[:3]


def test_stitch_offset_does_not_lose_an_edge():
    # Regression: applying the X/Y offset to the generated points instead of to
    # the grid bounds tests each candidate before it moves, which can only drop
    # positions and never add the ones that become valid once shifted. It cost a
    # whole row and column of the grid. Offsetting by half the spacing lands the
    # grid strictly inside the inset pour, so it must not place *fewer* vias.
    plain, _ = stitch(
        _fake_board(), ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu,
        "GND", 0.6, 0.3, 2.0, "Square", 0.0, 0.0,
    )
    shifted, _ = stitch(
        _fake_board(), ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu,
        "GND", 0.6, 0.3, 2.0, "Square", 1.0, 1.0,
    )
    assert shifted > plain, (plain, shifted)


def _smd_pad(net, size_x, size_y, angle_deg=0.0):
    """An SMD pad on F.Cu, shaped like the kipy objects the keepouts read."""
    from types import SimpleNamespace

    from kipy.board_types import PadType

    return SimpleNamespace(
        pad_type=PadType.PT_SMD,
        net=SimpleNamespace(name=net),
        position=SimpleNamespace(x=0, y=0),
        padstack=SimpleNamespace(
            layers=[BoardLayer.BL_F_Cu],
            angle=SimpleNamespace(degrees=angle_deg),
            copper_layers=[
                SimpleNamespace(
                    layer=BoardLayer.BL_F_Cu,
                    size=SimpleNamespace(x=size_x, y=size_y),
                )
            ],
        ),
    )


def test_pad_copper_keepout_respects_same_net_toggle():
    # The thermal-via case: with same-net pads off, a GND pad must produce no
    # keepout at all, or a via array under a QFN ground pad places nothing. Pads
    # on other nets are avoided either way.
    from types import SimpleNamespace

    span = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]
    gnd = _smd_pad("GND", from_mm(3.0), from_mm(3.0))
    sig = _smd_pad("SIG", from_mm(1.0), from_mm(1.0))
    board = SimpleNamespace(get_pads=lambda: [gnd, sig])
    clearances = _fallback_clearances()

    off = _pad_copper_keepout_shapes(
        board, "GND", from_mm(0.3), clearances, span, avoid_same_net_pads=False
    )
    assert len(off) == 1, off  # SIG only

    on = _pad_copper_keepout_shapes(
        board, "GND", from_mm(0.3), clearances, span, avoid_same_net_pads=True
    )
    assert len(on) == 2, on

    # A same-net pad takes the via radius and no clearance on top, so stitching
    # can hug it without the ring landing on the pad copper.
    _, _, maxx, _ = on[0].bounds
    assert abs(maxx - (from_mm(1.5) + from_mm(0.3))) < from_mm(0.01), maxx

    # An SMD pad on a layer outside the span is not in the via's way at all.
    inner = [BoardLayer.BL_In1_Cu, BoardLayer.BL_In2_Cu]
    assert _pad_copper_keepout_shapes(
        board, "GND", from_mm(0.3), clearances, inner, avoid_same_net_pads=True
    ) == []


def test_slot_and_pad_copper_rotate_the_same_way():
    # Regression: the slot capsule in _keepout_shapes and the copper rect in
    # _pad_copper_keepout_shapes describe the same physical pad, so they have to
    # rotate in the same direction. They used to use opposite signs, which
    # mirrored the drill keepout for every pad that was not axis aligned.
    from types import SimpleNamespace

    from kipy.board_types import PadType

    def y_at_max_x(shape):
        return max(shape.exterior.coords, key=lambda c: c[0])[1]

    span = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]
    angle = 30.0

    slot = SimpleNamespace(
        pad_type=PadType.PT_PTH,
        position=SimpleNamespace(x=0, y=0),
        padstack=SimpleNamespace(
            angle=SimpleNamespace(degrees=angle),
            drill=SimpleNamespace(
                diameter=SimpleNamespace(x=from_mm(3.0), y=from_mm(0.3))
            ),
        ),
    )
    board_slot = SimpleNamespace(get_vias=lambda: [], get_pads=lambda: [slot])
    capsule = _keepout_shapes(board_slot, from_mm(0.15), span)[0]

    rect_pad = _smd_pad("SIG", from_mm(3.0), from_mm(1.0), angle_deg=angle)
    board_rect = SimpleNamespace(get_pads=lambda: [rect_pad])
    rect = _pad_copper_keepout_shapes(
        board_rect, "GND", from_mm(0.15), _fallback_clearances(), span
    )[0]

    # KiCad angles are counter-clockwise on screen with the board y axis pointing
    # down, so a +30 degree pad leans toward negative y at its far end. Both
    # keepouts have to agree on that.
    assert y_at_max_x(capsule) < 0, y_at_max_x(capsule)
    assert y_at_max_x(rect) < 0, y_at_max_x(rect)

    # And the long axis has to survive the rotation: a 3 mm slot rotated 30
    # degrees still reaches about 1.35 mm * cos(30) out in x.
    minx, miny, maxx, maxy = capsule.bounds
    assert maxx > from_mm(1.0), maxx
    assert maxy < maxx, (maxy, maxx)


def test_nudge_radius_keeps_hole_margin_between_neighbours():
    # Two neighbours nudged toward each other end up (spacing - 2r) apart, so r
    # has to leave HOLE_MARGIN_MM between their drills. Nearest-neighbour
    # distance is `spacing` in all three patterns. The old spacing/3 broke this
    # at the microvia defaults (0.333 mm apart, 0.233 mm of hole gap).
    from via_stitching_action import HOLE_MARGIN_MM

    for spacing_mm, drill_mm in ((2.0, 0.3), (1.0, 0.1), (0.8, 0.3)):
        spacing, drill = from_mm(spacing_mm), from_mm(drill_mm)
        r = _nudge_radius(spacing, drill)
        assert 0 < r <= spacing // 4, (spacing_mm, r)  # grid still reads as a grid
        gap = (spacing - 2 * r) - drill
        assert gap >= from_mm(HOLE_MARGIN_MM), (spacing_mm, drill_mm, gap)

    # A grid with no slack left nudges nothing rather than crowding the holes.
    assert _nudge_radius(from_mm(0.5), from_mm(0.3)) == 0


def test_nudged_finds_a_spot_beside_a_keepout():
    # The point of the whole thing: a candidate landing in a pad's clearance area
    # should come out beside the pad instead of leaving a hole in the grid.
    from shapely.geometry import Point, box
    from shapely.prepared import prep

    spacing, drill = from_mm(2.0), from_mm(0.3)
    nudge_r = _nudge_radius(spacing, drill)
    region = box(0, 0, 10 * MM, 10 * MM)
    prepared = prep(region)

    def allowed(pt):
        return prepared.contains(pt)

    here = (5 * MM, 5 * MM)
    small = _blocked_predicate([Point(*here).buffer(from_mm(0.3))])
    moved = _nudged(here[0], here[1], nudge_r, allowed, small)
    assert moved is not None
    assert not small(*moved)
    assert allowed(Point(*moved))
    assert math.hypot(moved[0] - here[0], moved[1] - here[1]) <= nudge_r + 1

    # A keepout that swallows the whole ring drops the candidate rather than
    # walking it across the board.
    big = _blocked_predicate([Point(*here).buffer(from_mm(2.0))])
    assert _nudged(here[0], here[1], nudge_r, allowed, big) is None

    # Near the edge of the pour, whatever it settles on is still inside it.
    edge = _blocked_predicate([Point(0, 0).buffer(from_mm(0.3))])
    at_edge = _nudged(0, 0, nudge_r, allowed, edge)
    assert at_edge is None or allowed(Point(*at_edge)), at_edge

    # A pour with no room for the ring drops the candidate instead of putting a
    # via outside the copper.
    tiny = prep(box(0, 0, from_mm(0.1), from_mm(0.1)))
    assert _nudged(0, 0, nudge_r, lambda pt: tiny.contains(pt), edge) is None

    # No slack in the grid: nothing moves.
    assert _nudged(here[0], here[1], 0, allowed, small) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
