# Board-touching geometry for the legacy (pre-9) SWIG backend of Via Stitching.
#
# Mirrors via_stitching_action.py's geometry functions one for one, using
# pcbnew's SWIG bindings instead of kipy/IPC. Every function here was
# verified against a real KiCad 6.0 board (not just against fakes) during
# development -- see via-stitching/spikes/legacy_swig/ for the individual
# spike files that established each piece, including the real API traps
# found along the way (constructor shapes, setter ordering, broken
# accessors). This module is the consolidated, production version of that
# work; the spikes remain as the record of what was checked and why.

import math
from collections import defaultdict

FALLBACK_CLEARANCE_MM = 0.2
FALLBACK_CLEARANCE_NM = round(FALLBACK_CLEARANCE_MM * 1_000_000)


def _line_chain_coords(chain):
    return [(chain.GetPoint(i).x, chain.GetPoint(i).y) for i in range(chain.PointCount())]


def polyset_to_shapely_polygons(polyset):
    """One shapely Polygon per outline in a SHAPE_POLY_SET, holes attached.

    Used for both a zone's filled copper (GetFilledPolysList) and its raw
    outline (Outline()) -- both return the same SHAPE_POLY_SET type on
    legacy, unlike kipy where filled polygons and the raw outline are
    distinct shapes."""
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
            poly = poly.buffer(0)  # repair rare self-intersections
        if not poly.is_empty:
            polygons.append(poly)
    return polygons


def copper_layer_order(board):
    """Enabled copper layers, physically ordered front to back.

    Layer IDs 0..31 are copper; KiCad numbers inner layers in physical
    stackup order between F_Cu(0) and B_Cu(31), so filtering to enabled
    copper layers while keeping ascending numeric order is sufficient --
    confirmed on a real 4-layer board (F_Cu, In1_Cu, In2_Cu, B_Cu)."""
    import pcbnew

    return [l for l in range(32) if pcbnew.IsCopperLayer(l) and board.IsLayerEnabled(l)]


def span_layers(board, start_layer, end_layer):
    """All enabled copper layers between start and end, inclusive, in stackup order."""
    order = copper_layer_order(board)
    i, j = order.index(start_layer), order.index(end_layer)
    if i > j:
        i, j = j, i
    return order[i:j + 1]


def zone_blocks_vias(zone, span_set):
    """True if a rule area on `zone` forbids vias on any layer in span_set."""
    if not zone.GetIsRuleArea():
        return False
    if not zone.GetDoNotAllowVias():
        return False
    layerset = zone.GetLayerSet()
    return any(layerset.Contains(l) for l in span_set)


def layer_region(zones, net_name, layer):
    """Union of net_name's filled copper on one layer."""
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
        polys.extend(polyset_to_shapely_polygons(zone.GetFilledPolysList(layer)))
    return unary_union(polys) if polys else None


def net_clearances(board, net_name):
    """Clearance in nm to hold between a via on net_name and each other net.

    KiCad resolves the clearance between two items to the larger of their
    two netclass values, so that maximum is precomputed here per net. Reads
    the netclass via board.GetNetClasses().Find(net.GetNetClassName()), not
    net.GetNetClass()/GetEffectiveNetclass() -- both of those return a
    broken untyped object with no usable methods on a real KiCad 6.0 board."""
    netclasses = board.GetNetClasses()
    values = {}
    for net in board.GetNetsByNetcode().values():
        nc = netclasses.Find(net.GetNetClassName()) or netclasses.GetDefault()
        values[net.GetNetname()] = nc.GetClearance()

    own = values.get(net_name, FALLBACK_CLEARANCE_NM)
    clearances = defaultdict(lambda: FALLBACK_CLEARANCE_NM)
    clearances.update({name: max(own, value) for name, value in values.items()})
    return clearances


def via_keepout_shapes(board, via_radius_nm, span, hole_margin_nm):
    """Drill keepouts around existing vias, on layers the new via's span overlaps."""
    import pcbnew
    from shapely.geometry import Point

    span_set = set(span)
    shapes = []
    for track in board.GetTracks():
        if not isinstance(track, pcbnew.PCB_VIA):
            continue
        top, bottom = track.TopLayer(), track.BottomLayer()
        via_span = set(range(min(top, bottom), max(top, bottom) + 1))
        if not (via_span & span_set):
            continue
        r = via_radius_nm + track.GetDrillValue() // 2 + hole_margin_nm
        pos = track.GetPosition()
        shapes.append(Point(pos.x, pos.y).buffer(r, quad_segs=8))
    return shapes


def pad_drill_keepout_shapes(board, via_radius_nm, hole_margin_nm):
    """Drill keepouts around through-hole pads (round holes get circles,
    slots get a capsule along their long axis, same as the real code)."""
    import pcbnew
    from shapely.geometry import Point, LineString

    shapes = []
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            if pad.GetAttribute() not in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
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


def track_keepout_shapes(board, net_name, via_radius_nm, clearances, span):
    """Clearance areas around other nets' tracks, on layers the via spans."""
    import pcbnew
    from shapely.geometry import LineString

    span_set = set(span)
    shapes = []
    for track in board.GetTracks():
        if isinstance(track, pcbnew.PCB_VIA):
            continue
        if track.GetNetname() == net_name or track.GetLayer() not in span_set:
            continue
        if isinstance(track, pcbnew.PCB_ARC):
            s, m, e = track.GetStart(), track.GetMid(), track.GetEnd()
            coords = [(s.x, s.y), (m.x, m.y), (e.x, e.y)]
        else:
            s, e = track.GetStart(), track.GetEnd()
            coords = [(s.x, s.y), (e.x, e.y)]
        r = via_radius_nm + track.GetWidth() // 2 + clearances[track.GetNetname()]
        shapes.append(LineString(coords).buffer(r, quad_segs=8))
    return shapes


def zone_keepout_shapes(zones, net_name, via_radius_nm, clearances, span):
    """Clearance areas around other nets' filled zones, on layers the via spans."""
    span_set = set(span)
    shapes = []
    for zone in zones:
        if zone.GetIsRuleArea():
            continue
        net = zone.GetNet()
        if net is not None and net.GetNetname() == net_name:
            continue
        margin = via_radius_nm + clearances[net.GetNetname() if net is not None else ""]
        for layer in span_set:
            if not zone.GetLayerSet().Contains(layer):
                continue
            for poly in polyset_to_shapely_polygons(zone.GetFilledPolysList(layer)):
                shapes.append(poly.buffer(margin, quad_segs=8))
    return shapes


def rule_area_keepout_shapes(zones, via_radius_nm, span):
    """Outlines of rule areas that forbid vias, on layers the via spans.
    Unconditional -- no clearance added, same as the real code."""
    span_set = set(span)
    shapes = []
    for zone in zones:
        if not zone_blocks_vias(zone, span_set):
            continue
        outline = zone.Outline()
        if outline.OutlineCount() == 0:
            continue
        for poly in polyset_to_shapely_polygons(outline):
            shapes.append(poly.buffer(via_radius_nm, quad_segs=8))
    return shapes


def footprint_boxes(footprints):
    """Per-footprint bounding boxes. Unlike kipy's batch call, GetBoundingBox()
    is a plain per-item method with nothing to silently drop."""
    return [fp.GetBoundingBox() for fp in footprints]


def footprint_keepout_shapes(board, net_name, via_radius_nm, clearances):
    """Margin box around every footprint's bounding box (mechanical fit, not
    copper -- applies regardless of the footprint's net)."""
    from shapely.geometry import box as shapely_box

    footprints = list(board.GetFootprints())
    if not footprints:
        return []
    margin = via_radius_nm + clearances[net_name]
    return [
        shapely_box(
            bbox.GetX() - margin, bbox.GetY() - margin,
            bbox.GetX() + bbox.GetWidth() + margin, bbox.GetY() + bbox.GetHeight() + margin,
        )
        for bbox in footprint_boxes(footprints)
    ]


def pad_copper_keepout_shapes(board, net_name, via_radius_nm, clearances, span,
                               avoid_same_net_pads=False):
    """Clearance areas around pad copper on the spanned layers.

    Legacy pads have one flat size (GetSize()), not kipy's per-layer
    padstack copper -- per-layer pad shapes are themselves a KiCad 9+
    feature, confirmed absent (PAD has no Padstack()) on a real KiCad 6.0
    board, so a legacy board can never have a pad whose copper differs by
    layer in the first place."""
    import pcbnew
    from shapely.geometry import box as shapely_box
    from shapely import affinity

    span_set = set(span)
    shapes = []
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            same_net = pad.GetNetname() == net_name
            if same_net and not avoid_same_net_pads:
                continue
            if pad.GetAttribute() not in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
                layerset = pad.GetLayerSet()
                if not any(layerset.Contains(l) for l in span_set):
                    continue
            size = pad.GetSize()
            if size.x <= 0 or size.y <= 0:
                continue
            margin = via_radius_nm + (0 if same_net else clearances[pad.GetNetname()])
            pos = pad.GetPosition()
            rect = shapely_box(
                pos.x - size.x // 2, pos.y - size.y // 2,
                pos.x + size.x // 2, pos.y + size.y // 2,
            )
            angle_deg = pad.GetOrientationDegrees()
            if angle_deg:
                rect = affinity.rotate(rect, -angle_deg, origin="center")
            shapes.append(rect.buffer(margin, quad_segs=8))
    return shapes


def make_via(board, via_type, start_layer, end_layer, diameter_nm, drill_nm, net_code, x, y):
    """Create a via. No padstack/template to build on legacy -- just the
    setters, once per via.

    SetViaType MUST come before SetLayerPair: calling them in the other
    order silently resets the via's span to a full F_Cu-B_Cu through-via
    span with no error, confirmed on a real board."""
    import pcbnew

    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.wxPoint(int(x), int(y)))
    via.SetViaType(via_type)
    via.SetLayerPair(start_layer, end_layer)
    via.SetWidth(diameter_nm)
    via.SetDrill(drill_nm)
    via.SetNetCode(net_code)
    return via


def group_vias(board, vias, name):
    """Bundle the vias into one named group -- the only trustworthy way to
    remove a legacy stitching run as a set, since legacy SWIG ActionPlugins
    have no reliable native undo (PCB_EDIT_FRAME's undo registration isn't
    exposed to Python at all). Best effort, never raises."""
    import pcbnew

    if not vias:
        return None
    try:
        group = pcbnew.PCB_GROUP(board)
        group.SetName(name)
        board.Add(group)
        for via in vias:
            group.AddItem(via)
        return group
    except Exception:
        return None


def grouped_vias(board, group):
    """The vias belonging to a group. A group can't be asked for its own
    members from Python -- GetItems() returns a non-iterable SwigPyObject
    and RunOnChildren() has no std::function typemap -- so scan the board
    and match on the back-link instead. SWIG hands out a fresh wrapper on
    every call, so compare .this, never identity."""
    import pcbnew

    vias = []
    for track in board.GetTracks():
        if not isinstance(track, pcbnew.PCB_VIA):
            continue
        parent = track.GetParentGroup()
        if parent is not None and parent.this == group.this:
            vias.append(track)
    return vias


def delete_grouped_vias(board, group, vias):
    """The only reliable way to remove a grouped run: explicit member-by-
    member removal. board.Remove(group) alone leaves every via still on
    the board, still pointing at the now-detached group -- confirmed on a
    real board; this asymmetry is not documented anywhere obvious."""
    for via in vias:
        board.Remove(via)
    if group is not None:
        board.Remove(group)
