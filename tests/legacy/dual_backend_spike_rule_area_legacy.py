# Spike: rule-area outline -> shapely, behind _rule_area_keepout_shapes,
# on legacy SWIG.
#
# ZONE.Outline() returns the same SHAPE_POLY_SET type as GetFilledPolysList()
# (confirmed via help() on a real KiCad 6.0 ZONE) -- so the zone spike's
# swig_polyset_to_shapely_polygons converts a raw (unfilled) outline exactly
# the same way it converts a fill. No separate conversion path needed here,
# unlike kipy where zone.outline is a distinct PolygonWithHoles from
# zone.filled_polygons.
#
# Rule areas are unconditional (no clearance, just via_radius_nm), same as
# the real code -- a rule area is an explicit "keep out" from the board
# designer, not a copper-clearance concern.

from dual_backend_spike_zones_legacy import swig_polyset_to_shapely_polygons
from dual_backend_spike_misc_legacy import swig_zone_blocks_vias


def swig_rule_area_keepout_shapes(zones, via_radius_nm, span):
    span_set = set(span)
    shapes = []
    for zone in zones:
        if not swig_zone_blocks_vias(zone, span_set):
            continue
        outline = zone.Outline()
        if outline.OutlineCount() == 0:
            continue
        for poly in swig_polyset_to_shapely_polygons(outline):
            shapes.append(poly.buffer(via_radius_nm, quad_segs=8))
    return shapes


def demo():
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
        def __init__(self, outline_count):
            self._outline = _FakeChain([(0, 0), (1_000_000, 0), (1_000_000, 1_000_000), (0, 1_000_000)])
            self._outline_count = outline_count
        def OutlineCount(self):
            return self._outline_count
        def Outline(self, i):
            return self._outline
        def HoleCount(self, i):
            return 0
        def Hole(self, i, j):
            raise IndexError

    class _FakeZone:
        def __init__(self, is_rule_area, keepout_vias, layers, outline_count=1):
            self._rule_area, self._keepout_vias = is_rule_area, keepout_vias
            self._layers = set(layers)
            self._polyset = _FakePolySet(outline_count)
        def GetIsRuleArea(self):
            return self._rule_area
        def GetDoNotAllowVias(self):
            return self._keepout_vias
        def GetLayerSet(self):
            class _L:
                def __init__(s, layers):
                    s._layers = layers
                def Contains(s, l):
                    return l in s._layers
            return _L(self._layers)
        def Outline(self):
            return self._polyset

    span = {0}  # F_Cu

    blocking = _FakeZone(True, True, [0])
    shapes = swig_rule_area_keepout_shapes([blocking], via_radius_nm=100_000, span=span)
    assert len(shapes) == 1
    assert shapes[0].area > 1_000_000 * 1_000_000  # outline area plus the via_radius buffer

    not_a_rule_area = _FakeZone(False, True, [0])
    assert swig_rule_area_keepout_shapes([not_a_rule_area], 100_000, span) == []

    allows_vias = _FakeZone(True, False, [0])
    assert swig_rule_area_keepout_shapes([allows_vias], 100_000, span) == []

    wrong_layer = _FakeZone(True, True, [31])  # B_Cu only
    assert swig_rule_area_keepout_shapes([wrong_layer], 100_000, span) == []

    empty_outline = _FakeZone(True, True, [0], outline_count=0)
    assert swig_rule_area_keepout_shapes([empty_outline], 100_000, span) == []

    print("rule-area outline: blocks only when is_rule_area + keepout_vias + spanned layer + non-empty outline")


if __name__ == "__main__":
    demo()
