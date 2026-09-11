# Verifies plugins_legacy/_geometry_legacy.py (the consolidated production
# module) against a real KiCad 6.0 board -- re-runs the same multi-layer
# scenario as dual_backend_spike_full_stitch_legacy.py, but importing from
# the real module instead of the scattered spikes, to catch any
# transcription error introduced while consolidating.

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins")
sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "plugins_legacy")

import pcbnew
import wx

wx.DisableAsserts()  # importing the plugin registers an ActionPlugin, see the other suites

import via_stitching_action_legacy as vsl  # noqa: E402

try:  # the IPC module's grid/nudge, to prove both backends share the maths
    import via_stitching_action as vsa  # noqa: E402
except ImportError:  # kipy is not installed (CI, or any KiCad 6 box)
    import via_stitching_action_legacy as vsa  # noqa: E402
import _geometry_legacy as geo  # noqa: E402

from shapely.geometry import Point
from shapely.prepared import prep

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
    board.SetCopperLayerCount(4)
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(gnd)
    vcc = pcbnew.NETINFO_ITEM(board, "VCC")
    board.Add(vcc)

    size = 10 * MM
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

    # Real span computation, real 4-layer board.
    span = geo.span_layers(board, pcbnew.F_Cu, pcbnew.In1_Cu)
    assert span == [pcbnew.F_Cu, pcbnew.In1_Cu]

    region = geo.layer_region(zones, "GND", pcbnew.F_Cu).intersection(
        geo.layer_region(zones, "GND", pcbnew.In1_Cu)
    )
    assert abs(region.area - size * size) < 1

    via_dia_nm, drill_nm, spacing_nm = 600_000, 300_000, 2 * MM
    via_radius_nm = via_dia_nm // 2
    region = region.buffer(-(via_radius_nm + 10_000))
    assert region.area > 0

    track = pcbnew.PCB_TRACK(board)
    track.SetNet(vcc)
    track.SetLayer(pcbnew.F_Cu)
    track.SetStart(geo.point(0, 5 * MM))
    track.SetEnd(geo.point(size, 5 * MM))
    track.SetWidth(300_000)
    board.Add(track)

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

    span_set = set(span)
    clearances = geo.net_clearances(board, "GND")
    keepout = []
    keepout += geo.via_keepout_shapes(board, via_radius_nm, span_set, 50_000)
    keepout += geo.track_keepout_shapes(board, "GND", via_radius_nm, clearances, span_set)
    keepout += geo.rule_area_keepout_shapes(zones + [rule_zone], via_radius_nm, span_set)
    keepout += geo.pad_drill_keepout_shapes(board, via_radius_nm, 50_000)
    keepout += geo.pad_copper_keepout_shapes(board, "GND", via_radius_nm, clearances, span_set)
    keepout += geo.zone_keepout_shapes(zones, "GND", via_radius_nm, clearances, span_set)
    keepout += geo.footprint_keepout_shapes(board, "GND", via_radius_nm, clearances)

    # The plugin's own predicate, not a copy of it: a copy silently drifted
    # from the real one and only shapely 1.8 noticed.
    blocked = vsl._blocked_predicate(keepout)
    prepared = prep(region)
    allowed = lambda pt: prepared.contains(pt)  # noqa: E731

    minx, miny, maxx, maxy = region.bounds
    candidates = [
        (x, y)
        for (x, y) in vsa._grid_points((minx, miny, maxx, maxy), spacing_nm, "Square")
        if allowed(Point(x, y))
    ]
    assert candidates

    nudge_r = vsa._nudge_radius(spacing_nm, drill_nm)
    points = []
    for (x, y) in candidates:
        if not blocked(x, y):
            points.append((x, y))
            continue
        moved = vsa._nudged(x, y, nudge_r, allowed, blocked)
        if moved is not None:
            points.append(moved)
    assert points
    assert all(x > 3 * MM for (x, y) in points)

    vias = [
        geo.make_via(board, pcbnew.VIATYPE_BLIND_BURIED, pcbnew.F_Cu, pcbnew.In1_Cu,
                     via_dia_nm, drill_nm, gnd.GetNetCode(), x, y)
        for (x, y) in points
    ]
    for via in vias:
        board.Add(via)
        assert via.TopLayer() == pcbnew.F_Cu and via.BottomLayer() == pcbnew.In1_Cu

    group = geo.group_vias(board, vias, "ViaStitching GND F_Cu:In1_Cu")
    assert group is not None
    assert all(v.GetParentGroup() is not None for v in vias)

    # And confirm the production delete path actually cleans up correctly.
    geo.delete_grouped_vias(board, group, vias)
    assert all(v.GetParentGroup() is None for v in vias)
    assert len(list(board.GetTracks())) == 1  # only the VCC track left, all vias gone

    print(
        f"consolidated _geometry_legacy.py: {len(candidates)} candidates, "
        f"{len(points)} placed, grouped, and cleanly deleted as a set"
    )


if __name__ == "__main__":
    run()
