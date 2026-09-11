# End-to-end integration test: a realistic 4-layer-board scenario for the
# legacy backend, combining every verified piece into one run against a
# real KiCad 6.0 board -- not a fake, not an isolated function.
#
# Scenario: a blind via (F.Cu <-> In1.Cu) stitching a GND pour that exists
# on both layers, with a VCC track crossing F.Cu and a via-keepout rule area
# covering the left third of In1.Cu. Uses the REAL _grid_points/_nudge_radius/
# _nudged from via_stitching_action.py (pure Python, zero board-API calls,
# confirmed shared as-is between backends) plus this session's legacy
# board-touching spikes for everything that does touch the board.
#
# Run with KiCad 6.0's own python.exe, with via-stitching/plugins on
# sys.path (for the real shared grid/nudge functions) and this directory on
# sys.path (for the legacy spikes). kicad-python must be pip-installed into
# that interpreter for via_stitching_action.py's module-level import to
# succeed even though none of its board-touching (kipy) code actually runs.

import math
import sys

import pcbnew

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins")
import via_stitching_action as vsa  # noqa: E402  (real shared _grid_points/_nudge_radius/_nudged)

from dual_backend_spike_zones_legacy import swig_layer_region
from dual_backend_spike_keepouts_legacy import swig_track_keepout_shapes, swig_via_keepout_shapes
from dual_backend_spike_rule_area_legacy import swig_rule_area_keepout_shapes
from dual_backend_spike_via_legacy import swig_legacy_make_via
from dual_backend_spike_misc_legacy import swig_group_vias

from shapely.geometry import Point
from shapely.prepared import prep
from shapely import STRtree

MM = 1_000_000


def _square_polyset(x0, y0, x1, y1):
    ps = pcbnew.SHAPE_POLY_SET()
    chain = pcbnew.SHAPE_LINE_CHAIN()
    for (x, y) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
        chain.Append(pcbnew.VECTOR2I(int(x), int(y)))
    chain.SetClosed(True)
    ps.AddOutline(chain)
    return ps


def run():
    board = pcbnew.BOARD()
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(gnd)
    vcc = pcbnew.NETINFO_ITEM(board, "VCC")
    board.Add(vcc)

    size = 10 * MM

    # GND fill on F.Cu and In1.Cu (injected: real Fill() was confirmed
    # broken headless on KiCad 6.0 in this session, but both the real
    # plugin and this one only ever read whatever the user already filled
    # via the GUI's 'B' shortcut -- they never call Fill() themselves for
    # this initial read, so an injected fill is the right stand-in for
    # "a board the user already filled", not a shortcut around a gap).
    zone_f = pcbnew.ZONE(board)
    zone_f.SetNet(gnd)
    zone_f.SetLayer(pcbnew.F_Cu)
    board.Add(zone_f)
    zone_f.SetFilledPolysList(pcbnew.F_Cu, _square_polyset(0, 0, size, size))

    zone_i1 = pcbnew.ZONE(board)
    zone_i1.SetNet(gnd)
    zone_i1.SetLayer(pcbnew.In1_Cu)
    board.Add(zone_i1)
    zone_i1.SetFilledPolysList(pcbnew.In1_Cu, _square_polyset(0, 0, size, size))

    zones = [zone_f, zone_i1]
    region = swig_layer_region(board, pcbnew, zones, "GND", pcbnew.F_Cu).intersection(
        swig_layer_region(board, pcbnew, zones, "GND", pcbnew.In1_Cu)
    )
    assert abs(region.area - size * size) < 1

    via_dia_nm, drill_nm, spacing_nm = 600_000, 300_000, 2 * MM
    via_radius_nm = via_dia_nm // 2
    region = region.buffer(-(via_radius_nm + 10_000))
    assert region.area > 0

    # Obstacle: a VCC track crossing F.Cu through the middle.
    track = pcbnew.PCB_TRACK(board)
    track.SetNet(vcc)
    track.SetLayer(pcbnew.F_Cu)
    track.SetStart(pcbnew.wxPoint(0, 5 * MM))
    track.SetEnd(pcbnew.wxPoint(size, 5 * MM))
    track.SetWidth(300_000)
    board.Add(track)

    # Obstacle: a via-keepout rule area over the left third of In1.Cu.
    rule_zone = pcbnew.ZONE(board)
    rule_zone.SetIsRuleArea(True)
    rule_zone.SetDoNotAllowVias(True)
    rule_zone.SetLayer(pcbnew.In1_Cu)
    board.Add(rule_zone)
    outline = pcbnew.SHAPE_LINE_CHAIN()
    for (x, y) in [(0, 0), (3 * MM, 0), (3 * MM, size), (0, size)]:
        outline.Append(pcbnew.VECTOR2I(x, y))
    outline.SetClosed(True)
    rule_zone.Outline().RemoveAllContours()
    rule_zone.Outline().AddOutline(outline)

    span = {pcbnew.F_Cu, pcbnew.In1_Cu}
    keepout = []
    keepout += swig_via_keepout_shapes(pcbnew, board, via_radius_nm, span, 50_000)
    keepout += swig_track_keepout_shapes(pcbnew, board, "GND", via_radius_nm, 200_000, span)
    keepout += swig_rule_area_keepout_shapes([rule_zone], via_radius_nm, span)

    tree = STRtree(keepout) if keepout else None
    blocked = (
        (lambda x, y: len(tree.query(Point(x, y), predicate="intersects")) > 0)
        if tree
        else (lambda x, y: False)
    )
    prepared = prep(region)
    allowed = lambda pt: prepared.contains(pt)  # noqa: E731

    minx, miny, maxx, maxy = region.bounds
    candidates = [
        (x, y)
        for (x, y) in vsa._grid_points((minx, miny, maxx, maxy), spacing_nm, "Square")
        if allowed(Point(x, y))
    ]
    assert candidates, "grid produced no candidates inside the region"

    nudge_r = vsa._nudge_radius(spacing_nm, drill_nm)
    points = []
    for (x, y) in candidates:
        if not blocked(x, y):
            points.append((x, y))
            continue
        moved = vsa._nudged(x, y, nudge_r, allowed, blocked)
        if moved is not None:
            points.append(moved)
    assert points, "every candidate was blocked with no nudge escape"

    # Nothing placed left of the rule area's 3mm boundary -- it's a hard
    # keepout, and a 0.5mm-radius nudge can't cross a 3mm-wide rule area.
    assert all(x > 3 * MM for (x, y) in points)
    for (x, y) in points:
        assert not blocked(x, y)

    vias = [
        swig_legacy_make_via(
            pcbnew, board, pcbnew.VIATYPE_BLIND_BURIED, pcbnew.F_Cu, pcbnew.In1_Cu,
            via_dia_nm, drill_nm, gnd.GetNetCode(), x, y,
        )
        for (x, y) in points
    ]
    for via in vias:
        board.Add(via)
        assert via.TopLayer() == pcbnew.F_Cu and via.BottomLayer() == pcbnew.In1_Cu

    group = swig_group_vias(pcbnew, board, vias, "ViaStitching GND F_Cu:In1_Cu")
    assert all(v.GetParentGroup() is not None for v in vias)

    print(
        f"end-to-end multi-layer stitch: {len(candidates)} candidates, "
        f"{len(points)} placed (rule area + track avoided), all grouped"
    )


if __name__ == "__main__":
    run()
