# Spike: zone fill geometry -> shapely, behind _layer_region /
# _polygon_with_holes_to_shapely, on legacy SWIG.
#
# kipy: zone.filled_polygons is {layer: [PolygonWithHoles, ...]}, each with
# a .outline PolyLine and a list of .holes PolyLines; _polyline_coords walks
# .nodes, handling straight points and arc nodes (start/mid/end) separately.
#
# legacy pcbnew: zone.GetFilledPolysList(layer) returns one SHAPE_POLY_SET
# for that layer directly (no per-layer dict to build -- the layer argument
# already selects it). A SHAPE_POLY_SET holds its own outlines and holes
# together: OutlineCount()/Outline(i) for shells, HoleCount(i)/Hole(i, j)
# for that shell's holes, each a SHAPE_LINE_CHAIN. No arc-node distinction
# here: confirmed on a real KiCad 6.0 SHAPE_LINE_CHAIN that GetPoint(i)
# always returns a plain VECTOR2I regardless of IsArcSegment(i) -- fills are
# already flattened to straight segments by the time they reach this API,
# on legacy same as kipy (the misc spike's demo already noted fill
# flattening isn't a 9+-only thing).

def _line_chain_coords(chain):
    return [(chain.GetPoint(i).x, chain.GetPoint(i).y) for i in range(chain.PointCount())]


def swig_polyset_to_shapely_polygons(polyset):
    """One shapely Polygon per outline in the SHAPE_POLY_SET, holes attached."""
    from shapely.geometry import Polygon

    polygons = []
    for i in range(polyset.OutlineCount()):
        shell = _line_chain_coords(polyset.Outline(i))
        if len(shell) < 3:
            continue
        holes = []
        for j in range(polyset.HoleCount(i)):
            ring = _line_chain_coords(polyset.Hole(i, j))
            if len(ring) >= 3:
                holes.append(ring)
        poly = Polygon(shell, holes)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            polygons.append(poly)
    return polygons


def swig_layer_region(board, pcbnew_module, zones, net_name, layer):
    """Union of net_name's filled copper on one layer -- the legacy
    equivalent of one entry in kipy's _layer_region() dict."""
    from shapely.ops import unary_union

    polys = []
    for zone in zones:
        if zone.GetIsRuleArea():
            continue
        net = zone.GetNet()
        if net is None or net.GetNetname() != net_name:
            continue
        if not zone.GetLayerSet().Contains(layer):
            continue
        polyset = zone.GetFilledPolysList(layer)
        polys.extend(swig_polyset_to_shapely_polygons(polyset))
    return unary_union(polys) if polys else None


def swig_zone_keepout_shapes(zones, net_name, via_radius_nm, clearance_lookup, span):
    """Clearance areas around other nets' filled zones, on layers the via spans.

    Verified against real filled zones on a KiCad 6.0 board: correctly
    excludes the requesting net's own zone and zones with no fill on a
    spanned layer, keeping only genuinely other-net copper."""
    span_set = set(span)
    shapes = []
    for zone in zones:
        if zone.GetIsRuleArea():
            continue
        net = zone.GetNet()
        if net is not None and net.GetNetname() == net_name:
            continue
        margin = via_radius_nm + clearance_lookup(net.GetNetname() if net is not None else "")
        for layer in span_set:
            if not zone.GetLayerSet().Contains(layer):
                continue
            for poly in swig_polyset_to_shapely_polygons(zone.GetFilledPolysList(layer)):
                shapes.append(poly.buffer(margin, quad_segs=8))
    return shapes


def demo():
    # Pure-shapely-side check that doesn't need a real board: build a
    # SHAPE_POLY_SET-shaped fake (same outline/hole access pattern) and
    # confirm the conversion produces the right shape and area.
    class _FakeVec:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class _FakeChain:
        def __init__(self, points):
            self._points = points
        def PointCount(self):
            return len(self._points)
        def GetPoint(self, i):
            return _FakeVec(*self._points[i])

    class _FakePolySet:
        def __init__(self, outline, holes=()):
            self._outline = _FakeChain(outline)
            self._holes = [_FakeChain(h) for h in holes]
        def OutlineCount(self):
            return 1
        def Outline(self, i):
            return self._outline
        def HoleCount(self, i):
            return len(self._holes)
        def Hole(self, i, j):
            return self._holes[j]

    # A 1mm square (1,000,000 nm side) with a 200,000nm square hole in the middle.
    outer = [(0, 0), (1_000_000, 0), (1_000_000, 1_000_000), (0, 1_000_000)]
    hole = [(400_000, 400_000), (600_000, 400_000), (600_000, 600_000), (400_000, 600_000)]

    polys = swig_polyset_to_shapely_polygons(_FakePolySet(outer, [hole]))
    assert len(polys) == 1
    poly = polys[0]
    assert poly.is_valid
    expected_area = 1_000_000 * 1_000_000 - 200_000 * 200_000
    assert abs(poly.area - expected_area) < 1  # exact for axis-aligned rectangles

    # No outline at all (degenerate/empty fill) -> nothing comes back.
    empty = swig_polyset_to_shapely_polygons(_FakePolySet([]))
    assert empty == []

    print(f"zone polyset->shapely: square-minus-hole area {poly.area:.0f} nm^2 matches expected")


if __name__ == "__main__":
    demo()
