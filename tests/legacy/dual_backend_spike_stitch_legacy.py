# Spike: the full stitch() orchestration, assembled from every verified
# legacy-SWIG piece built so far, mirroring the real stitch() in
# via_stitching_action.py step for step.
#
# Board-API equivalents used here, each confirmed on a real KiCad 6.0 board:
#   kipy                                    legacy SWIG
#   board.get_nets() + filter by name       board.GetNetInfo().GetNetItem(name) -> None if missing
#   board.get_zones()                       board.Zones()
#   _copper_layer_order (enabled, ordered)  board.GetEnabledLayers().CuStack() -- an LSEQ,
#                                            confirmed directly Python-iterable in stackup order
#   board.get_layer_name(l)                 board.GetLayerName(l)
#   board.create_items(vias) (one batch     board.Add(via) per via, in a plain Python loop --
#     RPC call, opaque to progress)           this is the one place legacy is easier to
#                                              instrument than IPC, since each Add() returns
#                                              control to Python immediately (see the earlier
#                                              finding: IPC's create_items has no progress hook
#                                              at all, legacy's own loop already has one for free)
#   board.get_items_by_id(...)              not needed: Add() either works or raises, no
#     (defensive re-fetch after a round trip)  separate process/round trip that could echo back
#                                              something different from what was sent
#   board.refill_zones(block=False)         ZONE_FILLER(board).Fill(zones, aParent=parent) --
#                                            confirmed elsewhere: no unified progress dialog is
#                                            possible (PROGRESS_REPORTER isn't exposed to Python
#                                            on any version), so this pops KiCad's own separate
#                                            fill dialog, matching how the shipped IPC plugin
#                                            already behaves in practice.
#
# The pure-Python grid/nudge math (_grid_points, _nudge_radius, _nudged) has
# zero board-API calls in the real code -- it is reproduced here verbatim,
# not re-derived, because it is genuinely shared, backend-agnostic logic.
# In the real plugin this would be one shared module both backends import,
# not a copy; it's inlined here only because this spike can't import
# via_stitching_action.py directly (its top-level kipy import isn't
# installed in a plain interpreter and isn't relevant to the legacy runtime
# anyway).

import math

from dual_backend_spike_misc_legacy import (
    swig_group_vias, swig_delete_grouped_vias, swig_footprint_keepout_shapes,
)
from dual_backend_spike_netclass_legacy import swig_net_clearance
from dual_backend_spike_via_legacy import swig_legacy_make_via
from dual_backend_spike_zones_legacy import swig_layer_region
from dual_backend_spike_keepouts_legacy import (
    swig_via_keepout_shapes, swig_pad_drill_keepout_shapes, swig_track_keepout_shapes,
)
from dual_backend_spike_pad_rotation_legacy import swig_pad_copper_keepout_shapes
from dual_backend_spike_rule_area_legacy import swig_rule_area_keepout_shapes

HOLE_MARGIN_NM = 200_000  # 0.2mm, matches the real HOLE_MARGIN_MM
EDGE_EPS_NM = 10_000      # 0.01mm, matches the real EDGE_EPS_MM
VIA_COUNT_WARN = 5000


def _grid_points(bounds, spacing_nm, pattern):
    """Verbatim from via_stitching_action.py -- pure Python, no board calls,
    genuinely shared between both backends."""
    minx, miny, maxx, maxy = bounds
    minx, miny = int(math.floor(minx)), int(math.floor(miny))
    maxx, maxy = int(math.ceil(maxx)), int(math.ceil(maxy))
    if pattern == "Hexagonal":
        row_pitch = round(spacing_nm * math.sqrt(3) / 2)
    else:
        row_pitch = spacing_nm
    if row_pitch <= 0 or spacing_nm <= 0:
        return
    row = 0
    y = miny
    while y <= maxy:
        if pattern in ("Hexagonal", "Staggered") and (row % 2) == 1:
            x = minx + spacing_nm // 2
        else:
            x = minx
        while x <= maxx:
            yield (x, y)
            x += spacing_nm
        y += row_pitch
        row += 1


def _nudge_radius(spacing_nm, drill_nm):
    room = (spacing_nm - drill_nm - HOLE_MARGIN_NM) // 2
    return max(0, min(spacing_nm // 4, room))


def _nudged(x, y, nudge_r, allowed, blocked):
    if nudge_r <= 0:
        return None
    from shapely.geometry import Point
    for k in range(8):
        a = 2 * math.pi * k / 8
        nx = x + round(nudge_r * math.cos(a))
        ny = y + round(nudge_r * math.sin(a))
        if not blocked(nx, ny) and allowed(Point(nx, ny)):
            return (nx, ny)
    return None


def _blocked_predicate(shapes):
    if not shapes:
        return lambda x, y: False
    from shapely import STRtree
    from shapely.geometry import Point
    tree = STRtree(shapes)
    return lambda x, y: len(tree.query(Point(x, y), predicate="intersects")) > 0


def _span_layers(pcbnew_module, board, start_layer, end_layer):
    order = list(board.GetEnabledLayers().CuStack())
    i, j = order.index(start_layer), order.index(end_layer)
    if i > j:
        i, j = j, i
    return order[i:j + 1]


def swig_stitch(pcbnew_module, board, via_type, start_layer, end_layer, net_name,
                 via_dia_nm, drill_nm, spacing_nm, pattern, x_offset_nm, y_offset_nm,
                 avoid_other_zones=True, avoid_footprints=True, avoid_same_net_pads=True,
                 parent=None):
    """Legacy-SWIG stitch(), mirroring the real IPC stitch() step for step."""
    from shapely.geometry import Point
    from shapely.prepared import prep

    via_radius_nm = via_dia_nm // 2

    net = board.GetNetInfo().GetNetItem(net_name)
    if net is None:
        raise RuntimeError(f"Net '{net_name}' not found on the board.")

    zones = list(board.Zones())
    span = _span_layers(pcbnew_module, board, start_layer, end_layer)

    regions = {}
    for layer in set(span) | {start_layer, end_layer}:
        region = swig_layer_region(board, pcbnew_module, zones, net_name, layer)
        if region is not None:
            regions[layer] = region
    if not regions:
        raise RuntimeError(f"No filled copper found for net '{net_name}'.")

    missing = [l for l in (start_layer, end_layer) if l not in regions]
    if missing:
        names = ", ".join(board.GetLayerName(l) for l in missing)
        raise RuntimeError(f"Net '{net_name}' has no filled copper on: {names}.")

    region = regions[start_layer].intersection(regions[end_layer])
    for layer in span:
        if layer in regions and layer not in (start_layer, end_layer):
            region = region.intersection(regions[layer])
    if region.is_empty:
        raise RuntimeError("The selected net's planes do not overlap on the selected layer span.")

    inset = via_radius_nm + EDGE_EPS_NM
    region = region.buffer(-inset)
    if region.is_empty:
        raise RuntimeError("No room for vias after clearance inset.")

    minx, miny, maxx, maxy = region.bounds
    shifted_bounds = (minx + x_offset_nm, miny + y_offset_nm, maxx + x_offset_nm, maxy + y_offset_nm)
    prepared = prep(region)
    allowed = lambda pt: prepared.contains(pt)
    candidates = [
        (x, y) for (x, y) in _grid_points(shifted_bounds, spacing_nm, pattern)
        if allowed(Point(x, y))
    ]
    if not candidates:
        raise RuntimeError("No via positions fit inside the overlap of the planes.")

    all_nets = [board.GetNetInfo().GetNetItem(i) for i in range(board.GetNetInfo().GetNetCount())]
    all_nets = [n for n in all_nets if n is not None]

    def clearance_for(name):
        n = board.GetNetInfo().GetNetItem(name) if name else None
        nc_name = n.GetNetClassName() if n is not None else "Default"
        nc = board.GetNetClasses().Find(nc_name) or board.GetNetClasses().GetDefault()
        return swig_net_clearance(nc)

    own_clearance = clearance_for(net_name)

    def clearance_lookup(other_name):
        return max(own_clearance, clearance_for(other_name))

    span_set = set(span)
    keepout = swig_via_keepout_shapes(pcbnew_module, board, via_radius_nm, span_set, HOLE_MARGIN_NM)
    keepout += swig_pad_drill_keepout_shapes(pcbnew_module, board, via_radius_nm, HOLE_MARGIN_NM)
    keepout += swig_pad_copper_keepout_shapes(
        pcbnew_module, board, net_name, via_radius_nm, clearance_lookup, span_set, avoid_same_net_pads
    )
    keepout += swig_rule_area_keepout_shapes(zones, via_radius_nm, span_set)
    keepout += swig_track_keepout_shapes(pcbnew_module, board, net_name, via_radius_nm, own_clearance, span_set)
    if avoid_other_zones:
        for zone in zones:
            zone_net = zone.GetNet()
            zname = zone_net.GetNetname() if zone_net is not None else ""
            if zname == net_name:
                continue
            for layer in span_set:
                if not zone.GetLayerSet().Contains(layer):
                    continue
                polyset = zone.GetFilledPolysList(layer)
                from dual_backend_spike_zones_legacy import swig_polyset_to_shapely_polygons
                margin = via_radius_nm + clearance_lookup(zname)
                for poly in swig_polyset_to_shapely_polygons(polyset):
                    keepout.append(poly.buffer(margin, quad_segs=8))
    if avoid_footprints:
        keepout += swig_footprint_keepout_shapes(board, net_name, via_radius_nm, clearance_lookup)

    blocked = _blocked_predicate(keepout)
    nudge_r = _nudge_radius(spacing_nm, drill_nm)
    points = []
    for (x, y) in candidates:
        if not blocked(x, y):
            points.append((x, y))
            continue
        moved = _nudged(x, y, nudge_r, allowed, blocked)
        if moved is not None:
            points.append(moved)

    if not points:
        raise RuntimeError(f"All {len(candidates)} candidate positions are blocked.")

    if len(points) > VIA_COUNT_WARN and parent is not None:
        import wx
        msg = f"This will place {len(points)} vias, which may make KiCad slow.\nPlace them anyway?"
        if wx.MessageBox(msg, "Many vias", wx.YES_NO | wx.ICON_WARNING, parent) != wx.YES:
            return 0, False

    vias = []
    for (x, y) in points:
        via = swig_legacy_make_via(
            pcbnew_module, board, via_type, start_layer, end_layer, via_dia_nm, drill_nm,
            net.GetNetCode(), x, y,
        )
        board.Add(via)
        vias.append(via)

    group = swig_group_vias(pcbnew_module, board, vias, f"ViaStitching {net_name} {start_layer}:{end_layer}")

    try:
        pcbnew_module.ZONE_FILLER(board).Fill(zones, False, parent)
    except Exception:
        pass

    return len(vias), group is not None
