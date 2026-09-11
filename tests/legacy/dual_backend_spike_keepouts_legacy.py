# Spike: existing-hole and other-net-track keepouts, behind _keepout_shapes
# and _track_keepout_shapes, on legacy SWIG.
#
# Real ordering trap found and confirmed on a real board (see the via spike
# for the full via.SetViaType/SetLayerPair case): PCB_VIA has no LayerPair()
# getter usable from Python -- it takes two 'PCB_LAYER_ID *' output params
# and SWIG never turns those into a return tuple here, so calling it raises
# TypeError. TopLayer()/BottomLayer() are the real, working accessors for a
# via's layer span; confirmed both return the actual values set (unlike the
# LayerPair() setter/getter pair, which is asymmetric in this binding: the
# two-arg setter works, its matching getter does not).
#
# GetNetname() is a real, direct win over kipy's shape here: both PCB_VIA and
# PCB_TRACK expose it straight on the item, no separate .net object hop
# needed (kipy: track.net.name; legacy: track.GetNetname()).

def swig_via_keepout_shapes(pcbnew_module, board, via_radius_nm, span, hole_margin_nm):
    """Drill keepouts around existing vias, on layers the new via's span overlaps."""
    from shapely.geometry import Point

    span_set = set(span)
    shapes = []
    for track in board.GetTracks():
        if not isinstance(track, pcbnew_module.PCB_VIA):
            continue
        top, bottom = track.TopLayer(), track.BottomLayer()
        via_span = set(range(min(top, bottom), max(top, bottom) + 1))
        if not (via_span & span_set):
            continue
        r = via_radius_nm + track.GetDrillValue() // 2 + hole_margin_nm
        pos = track.GetPosition()
        shapes.append(Point(pos.x, pos.y).buffer(r, quad_segs=8))
    return shapes


def swig_pad_drill_keepout_shapes(pcbnew_module, board, via_radius_nm, hole_margin_nm):
    """Drill keepouts around through-hole pads (round or slotted).

    First version of this only used a circle sized off the long axis for
    every hole, which over-blocks a slot's short axis. Ported the real
    code's capsule construction (a segment along the slot's long axis,
    buffered) using GetOrientationDegrees() -- same sign convention as the
    pad-copper rotation spike, carried forward, not re-derived."""
    import math
    from shapely.geometry import Point, LineString

    shapes = []
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            if pad.GetAttribute() not in (pcbnew_module.PAD_ATTRIB_PTH, pcbnew_module.PAD_ATTRIB_NPTH):
                continue
            drill = pad.GetDrillSize()
            if drill.x <= 0:
                continue
            long_r = max(drill.x, drill.y) // 2
            short_r = min(drill.x, drill.y) // 2
            pos = pad.GetPosition()
            if long_r == short_r:
                r = via_radius_nm + long_r + hole_margin_nm
                shapes.append(Point(pos.x, pos.y).buffer(r, quad_segs=8))
                continue
            half = long_r - short_r
            a = -math.radians(pad.GetOrientationDegrees())
            if drill.y > drill.x:
                dx = round(-half * math.sin(a))
                dy = round(half * math.cos(a))
            else:
                dx = round(half * math.cos(a))
                dy = round(half * math.sin(a))
            seg = LineString([(pos.x - dx, pos.y - dy), (pos.x + dx, pos.y + dy)])
            shapes.append(seg.buffer(via_radius_nm + short_r + hole_margin_nm, quad_segs=8))
    return shapes


def swig_track_keepout_shapes(pcbnew_module, board, net_name, via_radius_nm, clearance_nm, span):
    """Clearance areas around other nets' tracks, on layers the via spans.

    GetNetname() replaces kipy's track.net.name -- no separate net object
    needed. Arc handling mirrors the real code: start/mid/end as a 3-point
    LineString, close enough for a buffered keepout (the real fill geometry
    is already flattened well before this point, same as the zone spike)."""
    from shapely.geometry import LineString

    span_set = set(span)
    shapes = []
    for track in board.GetTracks():
        if isinstance(track, pcbnew_module.PCB_VIA):
            continue
        if track.GetNetname() == net_name or track.GetLayer() not in span_set:
            continue
        if isinstance(track, pcbnew_module.PCB_ARC):
            s, m, e = track.GetStart(), track.GetMid(), track.GetEnd()
            coords = [(s.x, s.y), (m.x, m.y), (e.x, e.y)]
        else:
            s, e = track.GetStart(), track.GetEnd()
            coords = [(s.x, s.y), (e.x, e.y)]
        r = via_radius_nm + track.GetWidth() // 2 + clearance_nm
        shapes.append(LineString(coords).buffer(r, quad_segs=8))
    return shapes


def demo():
    # Smoke-test the shapely side with a plain LineString/Point, independent
    # of any pcbnew object -- the real-board verification (constructor
    # shapes, GetNetname, TopLayer/BottomLayer, PCB_ARC dispatch) happens
    # separately against a live KiCad 6.0 board, not fakeable meaningfully
    # here without re-implementing half of pcbnew's class hierarchy.
    from shapely.geometry import Point, LineString

    p = Point(0, 0).buffer(500_000, quad_segs=8)
    assert p.area > 0
    line = LineString([(0, 0), (1_000_000, 0)]).buffer(100_000, quad_segs=8)
    assert line.area > 0
    print("keepout shape primitives: buffered point/line geometry sane")


if __name__ == "__main__":
    demo()
