# Via Stitching - KiCad 10 IPC action plugin
#
# Fills the overlap of a net's copper zones (e.g. the top + bottom GND pours)
# with a grid of through vias, so the two ground planes are stitched together.
#
# Uses the modern IPC API (kicad-python / kipy), so it keeps working on KiCad 11+
# where the legacy SWIG pcbnew bindings are removed.
#
# Original via-stitching idea: JS Reynaud. This is an independent IPC re-implementation.
# License: GPL-3.0-or-later

import json
import math
import os
import re
import sys
import sysconfig
import traceback
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

# KiCad builds the plugin venv with --system-site-packages and scrubs PYTHONPATH
# only on Windows (api_plugin_manager.cpp), so elsewhere a PYTHONPATH pointing at
# the distro's dist-packages sits ahead of the venv and google.protobuf resolves
# to the system copy, which is older than kipy needs. Put the venv back in front
# before the first third-party import. Issue #1.
if sys.prefix != sys.base_prefix:
    _venv_site = sysconfig.get_path("purelib")
    if _venv_site in sys.path:
        sys.path.remove(_venv_site)
    sys.path.insert(0, _venv_site)

import wx
import wx.adv

from kipy import KiCad
from kipy.errors import ApiError, ConnectionError as KiCadConnectionError
from kipy.proto.common import ApiStatusCode
from kipy.proto.common.types import KiCadObjectType
from kipy.proto.board.board_types_pb2 import BoardLayer as BL, ViaType
from kipy.board_types import Group, Via, ArcTrack, PadType, PSS_CIRCLE
from kipy.geometry import Vector2
from kipy.util.units import from_mm, to_mm
from kipy.util.board_layer import iter_copper_layers

# Make the sibling helper modules importable regardless of how KiCad launches us.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mac_dialog import attach_to_stage_manager, prepare_app  # noqa: E402
from _win_dialog import make_tool_window  # noqa: E402
from _kicad_config import kicad_config_dirs  # noqa: E402
from _i18n import _  # noqa: E402

VERSION = "3.0.0"

# shapely (plus the numpy and GEOS it drags in) costs the better part of a second
# to import, more than the rest of start-up together, so the geometry functions
# import it lazily and the dialog is on screen before any of it is paid.

# kipy defaults to 2 s, which get_zones() or a few hundred vias blow through on a
# real board. Every miss surfaces as a lost connection mid-edit.
API_TIMEOUT_MS = 30000

DEFAULT_NET = "GND"

# Every group this plugin makes is named "<prefix><net> <start>:<end>".
# "Reset last run" finds its own work by that prefix, so nothing else on
# the board can be swept up by it.
GROUP_PREFIX = "ViaStitching "

# Default via settings
DEFAULT_VIA_DIAMETER_MM = 0.6
DEFAULT_DRILL_MM = 0.3
DEFAULT_SPACING_MM = 2.0

# Default microvia settings
DEFAULT_MICROVIA_DIAMETER_MM = 0.25
DEFAULT_MICROVIA_DRILL_MM = 0.1
DEFAULT_MICROVIA_SPACING_MM = DEFAULT_MICROVIA_DIAMETER_MM * 4

PATTERNS = ["Hexagonal", "Square", "Staggered"]
DEFAULT_PATTERN = "Square"

# Off by default: a via through another net's zone is not a DRC error. KiCad pulls
# the fill back around it on the refill this plugin already triggers. And on the
# common 4-layer stackup an inner power plane covers most of the board, so on by
# default would place no vias at all where it otherwise places a full grid.
# Tracks are the opposite case, so that keepout is unconditional.
DEFAULT_AVOID_OTHER_ZONES = False

# Off by default: thermal-via arrays under a QFN/BGA ground pad are a common,
# intentional use of via stitching, so this must not block them by default.
DEFAULT_AVOID_FOOTPRINTS = False

# Off by default, same reasoning: dropping a via array straight onto a QFN/BGA
# thermal pad is a normal use of stitching. Pads on *other* nets are always
# avoided, that keepout is not optional.
DEFAULT_AVOID_SAME_NET_PADS = False

# Safety margins applied silently (the dialog has no clearance field, by design).
# The inset keeps a via at least (via_radius + EDGE_EPS) inside the fill on every
# layer the net is poured on, where KiCad has already pulled the fill back by the
# board clearance. It says nothing about layers the net is *not* poured on: a
# through via's drill crosses those too, which is what the track and zone keepouts
# below are for. EDGE_EPS just absorbs polygon rounding.
EDGE_EPS_MM = 0.05

# Hole-to-hole margin kept between a new via drill and existing via/pad drills.
HOLE_MARGIN_MM = 0.25

# Copper-to-copper clearance used when KiCad's netclass carries no value of its
# own, which is what a netclass inheriting the board minimum reports over IPC.
FALLBACK_CLEARANCE_MM = 0.2

# Warn before placing more than this many vias (keeps KiCad responsive).
VIA_COUNT_WARN = 5000

# Environment lines for error reports. Filled in once we know the KiCad version.
_ENV = []

def _kipy_version():
    try:
        from importlib.metadata import version

        return version("kicad-python")
    except Exception:
        return "unknown"


def _settings_path():
    """Where saved dialog settings live: alongside KiCad's own per-version config
    so it's a location we already know is user-writable, falling back to the
    home directory if KiCad's config dir can't be found."""
    dirs = kicad_config_dirs()
    base = dirs[0] if dirs else os.path.expanduser("~")
    return os.path.join(base, "via_stitching_settings.json")


def _load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_settings(values):
    try:
        with open(_settings_path(), "w", encoding="utf-8") as fh:
            json.dump(values, fh, indent=2)
    except Exception:
        pass  # best effort: a stale or unwritable settings file is not fatal


def _clear_settings():
    try:
        os.remove(_settings_path())
    except FileNotFoundError:
        pass
    except Exception:
        pass


# KiCad default theme copper colors (fallback)
_DEFAULT_COPPER = {
    "f": (200, 52, 52), "in1": (127, 200, 127), "in2": (206, 125, 66),
    "in3": (79, 203, 203), "in4": (219, 98, 139), "in5": (167, 165, 198),
    "in6": (40, 204, 217), "b": (77, 127, 196),
}

def _copper_key_to_layer(key):
    if key == "f":
        return BL.BL_F_Cu
    if key == "b":
        return BL.BL_B_Cu
    m = re.match(r"in(\d+)$", key)
    return getattr(BL, f"BL_In{m.group(1)}_Cu", None) if m else None

def _layer_colors():
    """{BoardLayer: (r, g, b)} from the active KiCad color theme, with fallbacks."""
    copper = dict(_DEFAULT_COPPER)
    try:
        for config_dir in kicad_config_dirs():
            pcbnew_json = Path(config_dir) / "pcbnew.json"
            if not pcbnew_json.exists():
                continue
            theme = json.loads(pcbnew_json.read_text())["appearance"]["color_theme"]
            data = json.loads((Path(config_dir) / "colors" / f"{theme}.json").read_text())
            for key, val in data.get("board", {}).get("copper", {}).items():
                m = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", val)
                if m:
                    copper[key.lower()] = tuple(map(int, m.groups()))
            break  # newest version dir with a pcbnew.json wins
    except Exception:
        pass  # built-in theme or unreadable config: defaults stand
    out = {}
    for key, rgb in copper.items():
        layer = _copper_key_to_layer(key)
        if layer is not None:
            out[layer] = rgb
    return out

def _color_swatch(rgb, size=14):
    bmp = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(*rgb)))
    dc.Clear()
    dc.SetPen(wx.Pen(wx.Colour(90, 90, 90)))
    dc.SetBrush(wx.TRANSPARENT_BRUSH)
    dc.DrawRectangle(0, 0, size, size)
    dc.SelectObject(wx.NullBitmap)
    return bmp

def _polyline_coords(polyline):
    """Convert a kipy PolyLine into a list of (x, y) nm tuples for shapely.

    Zone fill outlines are polygonal; arc nodes (rare in fills) are approximated
    by their start/mid/end points.
    """
    coords = []
    for node in polyline.nodes:
        if node.has_point:
            p = node.point
            coords.append((p.x, p.y))
        elif node.has_arc:
            arc = node.arc
            for pt in (arc.start, arc.mid, arc.end):
                coords.append((pt.x, pt.y))
    return coords


def _polygon_with_holes_to_shapely(pwh):
    """Build a shapely Polygon (with holes) from a kipy PolygonWithHoles."""
    from shapely.geometry import Polygon

    shell = _polyline_coords(pwh.outline)
    if len(shell) < 3:
        return None
    holes = []
    for hole in pwh.holes:
        ring = _polyline_coords(hole)
        if len(ring) >= 3:
            holes.append(ring)
    poly = Polygon(shell, holes)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if (not poly.is_empty) else None


def _copper_layer_order(board):
    """Enabled copper layers, physically ordered front to back."""
    enabled = set(board.get_enabled_layers())
    return [l for l in iter_copper_layers() if l in enabled]


def _span_layers(board, start_layer, end_layer):
    """All enabled copper layers between start and end, inclusive, in stackup order."""
    order = _copper_layer_order(board)
    i, j = order.index(start_layer), order.index(end_layer)
    if i > j:
        i, j = j, i          # user may have picked them "upside down"
    return order[i:j + 1]


def _via_type_advisory(board, via_type, start_layer, end_layer):
    """A plain-language warning if this via type/layer combination isn't the
    standard shape for that type, or None if it looks normal.

    Never blocks anything by itself - via type is a label for what KiCad
    should treat the via as, not a hard constraint on which layers can be
    picked (some real vias intentionally don't match the "standard" shape,
    e.g. stitching two ground planes through an intermediate signal layer).
    This is what the confirm-or-cancel prompt in main() shows before
    committing, so a genuine mistake still gets caught, just after the fact
    rather than by disabling the fields outright.
    """
    order = _copper_layer_order(board)
    outer = {order[0], order[-1]}
    starts_outer = start_layer in outer
    ends_outer = end_layer in outer

    if via_type == ViaType.VT_MICRO:
        if not (starts_outer or ends_outer) or len(_span_layers(board, start_layer, end_layer)) != 2:
            return _(
                "This isn't a standard microvia: a microvia connects an "
                "outer layer (F.Cu or B.Cu) to the layer right next to it. "
                "KiCad's DRC will likely flag this via."
            )
    elif via_type == ViaType.VT_BLIND:
        if not (starts_outer or ends_outer):
            return _(
                "This isn't a standard blind via: a blind via starts on an "
                "outer layer (F.Cu or B.Cu)."
            )
    elif via_type == ViaType.VT_BURIED:
        if starts_outer or ends_outer:
            return _(
                "This isn't a standard buried via: a buried via stays "
                "between two inner layers, not F.Cu or B.Cu."
            )
    return None


def _layer_region(zones, net_name):
    """Return {layer: shapely geometry} of the filled copper of `net_name`."""
    from shapely.ops import unary_union

    per_layer = {}
    for zone in zones:
        if zone.is_rule_area():
            continue
        net = zone.net
        if net is None or net.name != net_name:
            continue
        for layer, shapes in zone.filled_polygons.items():
            for pwh in shapes:
                poly = _polygon_with_holes_to_shapely(pwh)
                if poly is not None and not poly.is_empty:
                    if not poly.is_valid:
                        poly = poly.buffer(0)   # repair rare self-intersections
                    per_layer.setdefault(layer, []).append(poly)
    return {layer: unary_union(polys) for layer, polys in per_layer.items()}


def _fallback_clearances():
    """Clearance lookup that answers FALLBACK_CLEARANCE_MM for every net."""
    return defaultdict(lambda: from_mm(FALLBACK_CLEARANCE_MM))


def _net_clearances(board, nets, net_name):
    """Clearance in nm to hold between a via on `net_name` and each other net.

    KiCad resolves the clearance between two items to the larger of their two
    netclass values, so that maximum is what gets pre-computed per net here. A
    netclass that simply inherits the board minimum reports no value of its own
    over IPC, and those fall back to FALLBACK_CLEARANCE_MM, as does every net if
    KiCad refuses the call outright.
    """
    clearances = _fallback_clearances()
    try:
        classes = board.get_netclass_for_nets(nets)
    except Exception:
        return clearances

    fallback = from_mm(FALLBACK_CLEARANCE_MM)
    # `is not None`, not `or`: a netclass may legitimately carry an explicit 0,
    # and only a genuinely unset value means "inherits the board minimum".
    values = {
        name: (fallback if nc.clearance is None else nc.clearance)
        for name, nc in classes.items()
    }
    own = values.get(net_name, fallback)
    clearances.update({name: max(own, value) for name, value in values.items()})
    return clearances


def _blocked_predicate(shapes):
    """Build a `blocked(x, y)` test over `shapes`, through one spatial index.

    Unioning the keepouts first is what made this slow. On a board with 8000
    track segments, the unary_union of the buffered tracks alone cost about 6 s
    of a 7.7 s total, against 0.6 s for the STRtree used here, for exactly the
    same set of blocked candidates.
    """
    if not shapes:
        return lambda x, y: False

    from shapely import STRtree
    from shapely.geometry import Point

    tree = STRtree(shapes)
    return lambda x, y: len(tree.query(Point(x, y), predicate="intersects")) > 0


def _keepout_shapes(board, via_radius_nm, span):
    """Drill keepouts around existing vias and through-hole pads.

    Prevents new stitching vias from violating hole-to-hole clearance.
    Only vias whose layer span overlaps the new via's span can collide.
    Round holes get circles; milled slots get a capsule along their long
    axis instead of a circle as wide as the slot is long, which blocked a
    far larger disc than the hole itself.
    """
    from shapely.geometry import Point, LineString
    margin = from_mm(HOLE_MARGIN_MM)
    span_set = set(span)
    shapes = []
    for via in board.get_vias():
        try:
            via_span = set(_span_layers(board, via.padstack.drill.start_layer, via.padstack.drill.end_layer))
        except ValueError:
            via_span = span_set  # unknown span: be conservative, treat as blocking
        if not (via_span & span_set):
            continue
        r = via_radius_nm + via.drill_diameter // 2 + margin
        shapes.append(Point(via.position.x, via.position.y).buffer(r, quad_segs=8))
    for pad in board.get_pads():
        if pad.pad_type not in (PadType.PT_PTH, PadType.PT_NPTH):
            continue
        try:
            # A drill is a Vector2 because it may be a milled slot.
            drill = pad.padstack.drill.diameter
            long_r = max(drill.x, drill.y) // 2
            short_r = min(drill.x, drill.y) // 2
        except Exception:
            long_r = 0
        if long_r <= 0:
            continue
        if long_r == short_r:
            r = via_radius_nm + long_r + margin
            shapes.append(Point(pad.position.x, pad.position.y).buffer(r, quad_segs=8))
        else:
            # Slot: segment along the long axis, buffered to a capsule that
            # follows the hole's true outline plus the margins.
            # KiCad angles run counter-clockwise on screen while the board y
            # axis points down, so a KiCad +angle is a negative rotation in raw
            # coordinates. Same sign as the affinity.rotate call in
            # _pad_copper_keepout_shapes: letting the two drift apart mirrors
            # this keepout for every slot that is not axis aligned.
            half = long_r - short_r
            a = -math.radians(_pad_angle_degrees(pad))
            if drill.y > drill.x:
                # Long axis is vertical at zero rotation.
                dx = round(-half * math.sin(a))
                dy = round(half * math.cos(a))
            else:
                dx = round(half * math.cos(a))
                dy = round(half * math.sin(a))
            seg = LineString([
                (pad.position.x - dx, pad.position.y - dy),
                (pad.position.x + dx, pad.position.y + dy),
            ])
            shapes.append(seg.buffer(via_radius_nm + short_r + margin, quad_segs=8))
    return shapes


def _track_keepout_shapes(board, net_name, via_radius_nm, clearances, span):
    """Clearance areas around other nets' tracks, on layers the via spans.

    A through via's drill spans every copper layer of the board, not just the
    ones `net_name` happens to be poured on, so a track on a layer the pour
    never touches (an outer signal layer over an inner GND plane, say) still
    has to be avoided or the via drills straight through it. A microvia or
    blind/buried via only spans `span`, so a track outside it is irrelevant:
    the via's drill never reaches that layer.
    """
    from shapely.geometry import LineString

    span_set = set(span)
    shapes = []
    for track in board.get_tracks():
        if track.net.name == net_name or track.layer not in span_set:
            continue
        if isinstance(track, ArcTrack):
            coords = [
                (track.start.x, track.start.y),
                (track.mid.x, track.mid.y),
                (track.end.x, track.end.y),
            ]
        else:
            coords = [(track.start.x, track.start.y), (track.end.x, track.end.y)]
        r = via_radius_nm + track.width // 2 + clearances[track.net.name]
        shapes.append(LineString(coords).buffer(r, quad_segs=8))

    return shapes


def _zone_keepout_shapes(zones, net_name, via_radius_nm, clearances, span):
    """Clearance areas around other nets' filled zones, on layers the via spans.

    A through via's drill spans every copper layer, so a filled zone for a
    different net on a layer `net_name` never pours on (an inner power plane
    under a GND-poured outer layer, say) can still be worth avoiding, even
    though KiCad's refill would clear the fill back around the via by itself.
    A microvia or blind/buried via only spans `span`, so a zone filled on a
    layer outside it never meets the via at all.
    """
    span_set = set(span)
    shapes = []
    for zone in zones:
        net = zone.net
        if net is not None and net.name == net_name:
            continue
        margin = via_radius_nm + clearances[net.name if net is not None else ""]
        for layer, filled in zone.filled_polygons.items():
            if layer not in span_set:
                continue
            for pwh in filled:
                poly = _polygon_with_holes_to_shapely(pwh)
                if poly is not None:
                    shapes.append(poly.buffer(margin, quad_segs=8))

    return shapes


def _rule_area_keepout_shapes(zones, via_radius_nm, span):
    """Outlines of rule areas that forbid vias, on layers the via spans.

    A rule area is an explicit "keep out" from whoever laid the board out, so
    this is unconditional and takes no clearance on top: the rule is that the
    via simply is not inside the area. A through via crosses every copper
    layer, so a rule area on any single layer still catches it; a microvia or
    blind/buried via only spans `span`, so a rule area drawn entirely outside
    it doesn't apply.

    kipy 0.7.1 wraps a zone's copper settings but not its RuleAreaSettings, so
    the restriction flags are read off the proto.
    """
    span_set = set(span)
    shapes = []
    for zone in zones:
        if not zone.is_rule_area():
            continue
        if not zone.proto.rule_area_settings.keepout_vias:
            continue
        if not (set(zone.layers) & span_set):
            continue
        # Zone.outline indexes polygons[0], which an empty PolySet would not have.
        if not zone.proto.outline.polygons:
            continue
        poly = _polygon_with_holes_to_shapely(zone.outline)
        if poly is not None:
            shapes.append(poly.buffer(via_radius_nm, quad_segs=8))

    return shapes


def _footprint_keepout_shapes(board, net_name, via_radius_nm, clearances):
    """Clearance areas around every footprint's bounding box.

    Keeps vias out from under component bodies (BGAs, connectors, anything
    with fine-pitch leads underneath). This is a mechanical fit concern, not
    a copper one, so it applies regardless of the footprint's net.
    """
    from shapely.geometry import box as shapely_box

    footprints = board.get_footprints()
    if not footprints:
        return []

    # KiCad drops any item it has no box for, so this list can come back short.
    # Silently not avoiding those is the one outcome worth saying out loud.
    boxes = board.get_item_bounding_box(footprints)
    if len(boxes) < len(footprints):
        _to_stderr(
            f"Via Stitching: KiCad returned no bounding box for "
            f"{len(footprints) - len(boxes)} of {len(footprints)} footprints. "
            "Those are not being avoided."
        )

    margin = via_radius_nm + clearances[net_name]
    return [
        shapely_box(
            bbox.pos.x - margin,
            bbox.pos.y - margin,
            bbox.pos.x + bbox.size.x + margin,
            bbox.pos.y + bbox.size.y + margin,
        )
        for bbox in boxes
    ]


def _pad_angle_degrees(pad):
    """Pad rotation in degrees; 0 when it can't be determined.

    kipy exposes the rotation on the padstack; the exact type has varied,
    so both an Angle object and a plain number are accepted. Calibrated
    visually: if rotated rectangular pads get vias hugging their short
    sides and gaps along their long sides, the sign at the call site is
    wrong for this KiCad version.
    """
    try:
        a = pad.padstack.angle
        return a.degrees if hasattr(a, "degrees") else float(a)
    except Exception:
        return 0.0


def _pad_copper_keepout_shapes(board, net_name, via_radius_nm, clearances, span,
                               avoid_same_net_pads=DEFAULT_AVOID_SAME_NET_PADS):
    """Clearance areas around pad copper on the spanned layers.

    The via ring may stand inside a fill void, so clearance to the pad that
    caused the void is enforced here instead of by fill containment. Each
    keepout follows the pad's rotated rectangle, not its enclosing circle,
    which on large rectangular pads blocked wide crescents of good pour.

    Through-hole pads block any span; SMD pads only block spans including a
    layer they have copper on; a pad whose layers can't be determined is
    treated as blocking. Same-net pads take no clearance, only the via
    radius, so stitching hugs the pad without the ring sitting on the
    copper -- and they are skipped entirely when `avoid_same_net_pads` is
    off, for intentional via-in-pad such as thermal arrays.
    """
    from shapely.geometry import box as shapely_box
    from shapely import affinity
    span_set = set(span)
    shapes = []
    for pad in board.get_pads():
        same_net = pad.net.name == net_name
        if same_net and not avoid_same_net_pads:
            continue
        if pad.pad_type not in (PadType.PT_PTH, PadType.PT_NPTH):
            try:
                pad_layers = set(pad.padstack.layers)
            except Exception:
                pad_layers = set()
            if pad_layers and not (pad_layers & span_set):
                continue
        # Size off a layer the via actually reaches. On a padstack with per-layer
        # copper (PST_FRONT_INNER_BACK, PST_CUSTOM) index 0 is F.Cu, which is the
        # wrong ring for a blind via between two inner layers.
        try:
            layers = pad.padstack.copper_layers
            size = next((l for l in layers if l.layer in span_set), layers[0]).size
        except Exception:
            continue
        if size.x <= 0 or size.y <= 0:
            continue
        margin = via_radius_nm + (0 if same_net else clearances[pad.net.name])
        rect = shapely_box(
            pad.position.x - size.x // 2, pad.position.y - size.y // 2,
            pad.position.x + size.x // 2, pad.position.y + size.y // 2,
        )
        angle_deg = _pad_angle_degrees(pad)
        if angle_deg:
            rect = affinity.rotate(rect, -angle_deg, origin="center")
        shapes.append(rect.buffer(margin, quad_segs=8))
    return shapes


def _grid_points(bounds, spacing_nm, pattern):
    """Yield candidate (x, y) nm points across `bounds` for the given pattern.

    The grid is anchored to `bounds`, so the user controls placement via the
    region itself (and the X/Y offset parameters). Positions are therefore
    not reproducible across runs if the region's bounding box changes.
    """
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
    """How far a blocked candidate may be moved off its grid position.

    Bounded on purpose, twice over. The grid has to still read as a grid, and
    two neighbours nudged toward each other must still clear each other's
    holes: nearest-neighbour distance is `spacing` in all three patterns (that
    is what the hexagonal row pitch of spacing*sqrt(3)/2 buys), so moving both
    ends by r leaves them (spacing - 2r) apart. spacing/4 keeps that at half
    the spacing, and the second term holds HOLE_MARGIN_MM between the two
    drills even at spacings tight enough that a quarter would break it.

    Zero means the grid has no slack to give and nothing gets nudged.
    """
    room = (spacing_nm - drill_nm - from_mm(HOLE_MARGIN_MM)) // 2
    return max(0, min(spacing_nm // 4, room))


def _nudged(x, y, nudge_r, allowed, blocked):
    """First clear position on a ring of 8 at `nudge_r` around (x, y), or None.

    One ring, not a spiral: a blocked candidate costs 8 extra tests instead of
    the ~50 a three-ring search costs, and on a board with an inner power plane
    most candidates are blocked. This runs after the dialog is dismissed, with
    no progress feedback, so that factor is the difference between a pause and
    an apparent hang. The ring sits at the full radius rather than working
    outwards, because a point blocked in the middle of a pad's clearance area
    is likeliest to escape it at the far edge.

    `blocked` is tested before `allowed`: an STRtree point query is the cheaper
    of the two to fail on, and near a keepout it is the one that fails.
    """
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


@lru_cache(maxsize=None)
def _via_template(via_type, start_layer, end_layer, diameter_nm, drill_nm):
    """The shared part of a stitching via, built once per parameter set.

    Every assignment to PadStack.type costs a full iter_copper_layers() sweep,
    because kipy builds its required-layers dict eagerly and the PST_CUSTOM entry
    lists all 32 copper layers via is_copper_layer, which rebuilds a 32-element
    set of protobuf enum lookups per layer. Via() does one such assignment and the
    diameter setter another, so a 2401-via run paid it 4802 times: 14.8 million
    protobuf attribute lookups, 95% of the whole stitch run. Paying it once and
    CopyFrom-ing the finished proto per via is ~300x cheaper for the same bytes.

    Returns the raw proto. Callers must copy it, never mutate it.
    """
    via = Via()  # defaults to VT_THROUGH with a PST_NORMAL, F_Cu-only padstack
    via.type = via_type
    via.padstack.drill.start_layer = start_layer
    via.padstack.drill.end_layer = end_layer
    via.diameter = diameter_nm
    via.drill_diameter = drill_nm
    via.padstack.copper_layers[0].shape = PSS_CIRCLE
    return via.proto


def _make_via(x, y, via_type, start_layer, end_layer, diameter_nm, drill_nm, net):
    """Create a through Via with a valid single-layer PST_NORMAL padstack."""
    via = Via(proto=_via_template(via_type, start_layer, end_layer, diameter_nm, drill_nm))
    via.position = Vector2.from_xy(int(x), int(y))
    via.net = net
    return via


def _group_vias(board, vias, net_name, start_layer, end_layer):
    """Bundle the vias into one named group. Best effort, never raises.

    Built on create_items, not the editor's Group Items action. Driving that action
    over a large selection is what left the new vias out of the canvas view on
    KiCad 10, and nothing but reopening the board brought them back. It also split
    one run across four groups, because add_to_selection returns before the
    selection has crossed KiCad's UI thread, and its only success signal was
    get_groups(), which raises ApiError if any unrelated group on the board has a
    dangling member. create_items needs no selection and returns what it made.
    """

    if not vias:
        return False
    try:
        group = Group()
        span = f"{board.get_layer_name(start_layer)}:{board.get_layer_name(end_layer)}"
        # Group.name is read-only in kicad-python 0.7.x, the inherited proto is not.
        group.proto.name = f"{GROUP_PREFIX}{net_name} {span}"
        group.items = vias  # stores the member KIIDs, so the vias must exist already
        return bool(board.create_items(group))
    except Exception:
        return False


def stitching_runs(board):
    """Every stitching run still on the board, as (group, its vias).

    Deliberately not board.get_groups(): that unwraps the members of every
    group on the board in one call, so one unrelated group with a dangling
    member makes KiCad refuse the lot. Asking per group costs a round trip
    each and loses only the broken group.
    """
    runs = []
    for group in board.get_items(KiCadObjectType.KOT_PCB_GROUP):
        if not group.proto.name.startswith(GROUP_PREFIX):
            continue
        try:
            members = board.get_items_by_id(list(group.proto.items))
        except ApiError:
            continue
        vias = [m for m in members if isinstance(m, Via)]
        if vias:  # a run whose vias are already gone is nothing to remove
            runs.append((group, vias))
    return runs


def stitch(
    board, via_type, start_layer, end_layer, net_name, via_dia_mm, drill_mm, spacing_mm,
    pattern, x_offset_mm, y_offset_mm,
    avoid_other_zones=DEFAULT_AVOID_OTHER_ZONES, avoid_footprints=DEFAULT_AVOID_FOOTPRINTS,
    avoid_same_net_pads=DEFAULT_AVOID_SAME_NET_PADS, parent=None):
    """Run the stitching. Returns (vias placed, whether they were grouped)."""
    from shapely.geometry import Point
    from shapely.prepared import prep

    diameter_nm = from_mm(via_dia_mm)
    drill_nm = from_mm(drill_mm)
    spacing_nm = from_mm(spacing_mm)
    via_radius_nm = diameter_nm // 2
    x_offset_nm = from_mm(x_offset_mm)
    y_offset_nm = from_mm(y_offset_mm)

    # Resolve the real Net object (carries the proper proto for assignment).
    nets = board.get_nets()
    net = next((n for n in nets if n.name == net_name), None)
    if net is None:
        raise RuntimeError(_("Net '{net}' not found on the board.").format(net=net_name))

    zones = board.get_zones()
    regions = _layer_region(zones, net_name)
    if not regions:
        raise RuntimeError(
            _(
                "No filled copper found for net '{net}'.\n"
                "Fill the zones first (press B in the PCB editor), then run again."
            ).format(net=net_name)
        )

    span = _span_layers(board, start_layer, end_layer)

    # The via must land on the net's copper on the two layers it connects.
    missing = [l for l in (start_layer, end_layer) if l not in regions]
    if missing:
        names = ", ".join(board.get_layer_name(l) for l in missing)
        raise RuntimeError(
            _(
                "Net '{net}' has no filled copper on: {layers}.\n"
                "Pick start/end layers where the net is poured, or fill the zones "
                "first (press B in the PCB editor)."
            ).format(net=net_name, layers=names)
        )

    region = regions[start_layer].intersection(regions[end_layer])

    # Intermediate layers inside the span: where this net is poured there too,
    # keep the via on that copper (the barrel connects those layers as well).
    # Layers in the span where the net is NOT poured are left to DRC: the barrel
    # passing through another net's copper there is not checked here.
    for layer in span:
        if layer in regions and layer not in (start_layer, end_layer):
            region = region.intersection(regions[layer])

    if region.is_empty:
        raise RuntimeError(
            _(
                "The selected net's planes do not overlap anywhere on the "
                "selected layer span."
            )
        )

    # Inset so the via body + clearance ring stay inside the fill on all layers.
    inset = via_radius_nm + from_mm(EDGE_EPS_MM)
    region = region.buffer(-inset)
    if region.is_empty:
        raise RuntimeError(
            _("No room for vias after clearance inset. Try a smaller via diameter.")
        )

    # Grid before subtracting the keepout, so "grid too coarse" and "every position
    # is taken by an existing hole" get different messages. The grid is anchored to
    # region.bounds (shifted by the offset), so it is not reproducible run to run.
    #
    # The offset shifts the bounds themselves, not the generated points: shifting
    # points after generating them against the un-shifted bounds can only ever
    # drop candidates that move outside the region, never add the candidates
    # that only become valid once shifted, which silently undercounts a whole
    # edge for any nonzero offset.
    minx, miny, maxx, maxy = region.bounds
    shifted_bounds = (
        minx + x_offset_nm, miny + y_offset_nm, maxx + x_offset_nm, maxy + y_offset_nm,
    )
    prepared = prep(region)
    allowed = lambda pt: prepared.contains(pt)
    candidates = [
        (x, y)
        for (x, y) in _grid_points(shifted_bounds, spacing_nm, pattern)
        if allowed(Point(x, y))
    ]
    if not candidates:
        raise RuntimeError(
            _(
                "No via positions fit inside the overlap of the planes.\n"
                "Try a smaller spacing or via diameter."
            )
        )

    # Every keepout as one flat list of shapes, tested through a single spatial
    # index. Avoid existing via/pad drill holes, other nets' pad copper and
    # tracks, plus whatever else the dialog asked for.
    clearances = _net_clearances(board, nets, net_name)
    keepout = _keepout_shapes(board, via_radius_nm, span)
    keepout += _pad_copper_keepout_shapes(board, net_name, via_radius_nm, clearances, span, avoid_same_net_pads)
    keepout += _rule_area_keepout_shapes(zones, via_radius_nm, span)
    keepout += _track_keepout_shapes(board, net_name, via_radius_nm, clearances, span)
    if avoid_other_zones:
        keepout += _zone_keepout_shapes(zones, net_name, via_radius_nm, clearances, span)
    if avoid_footprints:
        keepout += _footprint_keepout_shapes(board, net_name, via_radius_nm, clearances)
    blocked = _blocked_predicate(keepout)

    # A candidate blocked by a keepout gets one bounded attempt at a spot beside
    # it before being dropped, so the grid keeps a via next to a pad's clearance
    # area instead of leaving a hole there. Clear candidates cost nothing extra.
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
        # Only name the optional keepouts that are actually switched on, so this
        # never sends someone off to untick a box that is already off.
        # These fragments join with a plain " or " regardless of language --
        # full grammatical localization of a dynamically assembled sentence
        # is out of scope; each piece is still translated on its own.
        optional = [
            name
            for name, on in (
                (_("other nets' zones"), avoid_other_zones),
                (_("footprints"), avoid_footprints),
            )
            if on
        ]
        blockers = " or ".join(
            [_("existing vias, pads, tracks or rule areas")] + optional
        )
        untick = (
            " " + _("Or untick") + " " + " or ".join(f"'{_('Avoid')} {n}'" for n in optional) + "."
            if optional
            else ""
        )
        raise RuntimeError(
            _(
                "All {count} candidate positions are blocked by {blockers}.\n"
                "If these zones are already stitched, delete the previous vias first, "
                "or try a smaller spacing."
            ).format(count=len(candidates), blockers=blockers) + untick
        )

    if len(points) > VIA_COUNT_WARN:
        msg = _(
            "This will place {count} vias, which may make KiCad slow.\n"
            "Increase the spacing for fewer vias.\n\nPlace them anyway?"
        ).format(count=len(points))
        style = wx.YES_NO | wx.ICON_WARNING | wx.STAY_ON_TOP
        if wx.MessageBox(msg, _("Many vias"), style, parent) != wx.YES:
            return 0, False

    vias = [_make_via(x, y, via_type, start_layer, end_layer, diameter_nm, drill_nm, net) for (x, y) in points]

    # No explicit commit: one create_items call is already one undo step, and an
    # open commit can be seized or rolled back by anything else touching the editor.
    created = board.create_items(vias)
    if len(created) != len(vias):
        raise RuntimeError(
            _(
                "KiCad accepted only {created} of the {total} vias.\n"
                "Check that the drill is smaller than the via diameter."
            ).format(created=len(created), total=len(vias))
        )

    # create_items reports what it echoed back, not what the board kept.
    placed = board.get_items_by_id([v.id for v in created])
    if len(placed) != len(created):
        raise RuntimeError(
            _(
                "{created} vias were created but only {placed} are on the "
                "board. Nothing was rolled back, so check the board before saving."
            ).format(created=len(created), placed=len(placed))
        )

    grouped = _group_vias(board, created, net_name, start_layer, end_layer)

    # Refilling is safe: vias placed this way survive a manual B and an API refill.
    # block=False because kipy's blocking poll loop never increments its counter, so
    # a busy KiCad would spin it forever.
    try:
        board.refill_zones(block=False)
    except Exception:
        pass

    return len(placed), grouped


class ViaStitchingDialog(wx.Dialog):
    """The 'Via Stitching Parameters' input dialog."""

    def __init__(self, parent, net_names, board):
        super().__init__(
            parent,
            title=_("Via Stitching Parameters"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
        )

        icon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "icon.png"
        )
        if os.path.exists(icon_path):
            self.SetIcon(wx.Icon(icon_path, wx.BITMAP_TYPE_PNG))

        # Default tooltip auto-pop is ~5s, too short for the multi-paragraph
        # tooltips below. This is a process-global wx setting, so one call here
        # covers every tooltip in the dialog. Windows' native tooltip control
        # takes this as a signed 16-bit delay (TTM_SETDELAYTIME): anything at
        # or above 32768 wraps around and comes back *shorter* than the
        # default, which is why 60000 didn't actually help. 32000 is the
        # longest value that doesn't wrap.
        wx.ToolTip.SetAutoPop(32000)

        enabled = set(board.get_enabled_layers())
        copper_layer_ids = [l for l in iter_copper_layers() if l in enabled]
        self.layer_map   = {board.get_layer_name(l): l for l in copper_layer_ids}
        self.layer_names = {v: k for k, v in self.layer_map.items()}
        copper_layer_names = list(self.layer_map.keys())
        self._all_layer_names = copper_layer_names  # physical order, front to back
        self._layer_colors = _layer_colors()
        layer_colors = self._layer_colors

        self.VIA_TYPE_CHOICES = {
            "Through":      ViaType.VT_THROUGH,
            "Micro":        ViaType.VT_MICRO,
            "Blind/Buried": ViaType.VT_BLIND_BURIED,
            "Blind":        ViaType.VT_BLIND,
            "Buried":       ViaType.VT_BURIED,
        }

        self.VIA_TYPE_NAMES = {v: k for k, v in self.VIA_TYPE_CHOICES.items()}   # int -> name

        self.board = board

        selected_vias = board.get_selection(KiCadObjectType.KOT_PCB_VIA)
        sample_via = None
        if selected_vias:
            sample_via = selected_vias[0]
            sample_via_start_layer = sample_via.padstack.drill.start_layer
            sample_via_end_layer = sample_via.padstack.drill.end_layer
            sample_via_net_name = sample_via.net.name

        # A preselected via's own settings always win. Otherwise fall back to
        # whatever was saved from the last successful run, then the hardcoded
        # defaults. Never touched when a via was preselected, so pasting a real
        # via's settings is never second-guessed by a stale saved run.
        saved = {} if sample_via else _load_settings()

        grid = wx.FlexGridSizer(cols=2, vgap=5, hgap=5)
        grid.AddGrowableCol(1)

        def add_row(label, ctrl):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 1, wx.EXPAND)

        via_types = list(self.VIA_TYPE_CHOICES.keys())
        self.via_type = wx.Choice(self, choices=via_types)
        if sample_via:
            self.via_type.SetStringSelection(self.VIA_TYPE_NAMES.get(sample_via.type))
        elif saved.get("via_type_name") in via_types:
            self.via_type.SetStringSelection(saved["via_type_name"])
        else:
            self.via_type.SetSelection(0)
        self.via_type.Bind(wx.EVT_CHOICE, lambda evt: self._on_via_type())

        def _layer_combo():
            combo = wx.adv.BitmapComboBox(self, style=wx.CB_READONLY)
            for name in copper_layer_names:
                rgb = layer_colors.get(self.layer_map[name], (128, 128, 128))
                combo.Append(name, _color_swatch(rgb))
            return combo

        self.start_layer = _layer_combo()
        if sample_via:
            self.start_layer.SetStringSelection(self.layer_names[sample_via_start_layer])
        elif saved.get("start_layer_name") in copper_layer_names:
            self.start_layer.SetStringSelection(saved["start_layer_name"])
        else:
            self.start_layer.SetSelection(0)
        self.end_layer = _layer_combo()
        if sample_via:
            self.end_layer.SetStringSelection(self.layer_names[sample_via_end_layer])
        elif saved.get("end_layer_name") in copper_layer_names:
            self.end_layer.SetStringSelection(saved["end_layer_name"])
        else:
            self.end_layer.SetSelection(len(copper_layer_names) - 1)
        # Micro's end layer is derived from which outer layer the start is, so
        # changing the start has to re-derive it. Either layer changing can also
        # flip the advisory (e.g. picking a non-outer end layer for a Blind via).
        self.start_layer.Bind(wx.EVT_COMBOBOX, lambda evt: self._on_start_layer())
        self.end_layer.Bind(wx.EVT_COMBOBOX, lambda evt: self._update_advisory())

        if sample_via:
            via_dia_str = str(to_mm(sample_via.diameter))
            drill_str = str(to_mm(sample_via.drill_diameter))
            spacing_str = str(to_mm(sample_via.diameter * 4))
        elif saved:
            via_dia_str = str(saved.get("via_dia_mm", DEFAULT_VIA_DIAMETER_MM))
            drill_str = str(saved.get("drill_mm", DEFAULT_DRILL_MM))
            spacing_str = str(saved.get("spacing_mm", DEFAULT_SPACING_MM))
        else:
            via_dia_str = str(DEFAULT_VIA_DIAMETER_MM)
            drill_str = str(DEFAULT_DRILL_MM)
            spacing_str = str(DEFAULT_SPACING_MM)

        self.via_dia = wx.TextCtrl(self, value=via_dia_str)
        self.drill = wx.TextCtrl(self, value=drill_str)
        self.spacing = wx.TextCtrl(self, value=spacing_str)

        # Values the dialog put in those three fields itself, which
        # _apply_via_type_defaults is then allowed to swap out. Seeded empty when
        # the fields came from a preselected via, so switching the via type never
        # discards settings that were copied off a real via on the board.
        self._auto_values = (
            set() if sample_via
            else {via_dia_str, drill_str, spacing_str}
        )

        self.pattern = wx.Choice(self, choices=PATTERNS)
        if saved.get("pattern") in PATTERNS:
            self.pattern.SetSelection(PATTERNS.index(saved["pattern"]))
        else:
            self.pattern.SetSelection(PATTERNS.index(DEFAULT_PATTERN))

        # if sample via layer starts in an odd layer then it starts with an offset
        if sample_via and sample_via_start_layer % 2 == 0:
            x_offset_str = str(to_mm(sample_via.diameter * 2))
            y_offset_str = str(to_mm(sample_via.diameter * 2))
        elif not sample_via and saved:
            x_offset_str = str(saved.get("x_offset_mm", to_mm(0.0)))
            y_offset_str = str(saved.get("y_offset_mm", to_mm(0.0)))
        else:
            x_offset_str = str(to_mm(0.0))
            y_offset_str = str(to_mm(0.0))

        self.x_offset = wx.TextCtrl(self, value=x_offset_str)
        self.y_offset = wx.TextCtrl(self, value=y_offset_str)
        offset_tip = _(
            "Shifts this run's grid, so a second pass (e.g. a back-side "
            "microvia stitch) doesn't land on top of the first."
        )
        self.x_offset.SetToolTip(offset_tip)
        self.y_offset.SetToolTip(offset_tip)

        names = list(net_names)
        self.net = wx.ComboBox(self, choices=names, style=wx.CB_DROPDOWN)
        if sample_via:
            self.net.SetValue(sample_via_net_name)
        elif saved.get("net_name"):
            self.net.SetValue(saved["net_name"])
        else:
            if DEFAULT_NET in names:
                self.net.SetValue(DEFAULT_NET)
            elif names:
                self.net.SetSelection(0)

        # Not derived from a preselected via either way, so a saved run applies
        # regardless of sample_via.
        self.avoid_zones = wx.CheckBox(self, label=_("Avoid zones of other nets"))
        self.avoid_zones.SetValue(bool(saved.get("avoid_other_zones", DEFAULT_AVOID_OTHER_ZONES)))
        self.avoid_zones.SetToolTip(_(
            "Keep vias out of other nets' filled copper on every layer.\n\n"
            "Off by default: a via through another net's pour is not a DRC "
            "error, because KiCad clears the fill back around it when the "
            "zones are refilled.\n\n"
            "Tick this to leave an inner power plane unperforated. Expect far "
            "fewer vias, since such a plane often covers most of the board."
        ))

        self.avoid_footprints = wx.CheckBox(self, label=_("Avoid footprints"))
        self.avoid_footprints.SetValue(bool(saved.get("avoid_footprints", DEFAULT_AVOID_FOOTPRINTS)))
        self.avoid_footprints.SetToolTip(_(
            "Keep vias out from under every component's bounding box.\n\n"
            "This is about mechanical fit, not clearance, so it applies "
            "whatever net the footprint is on.\n\n"
            "Off by default: a thermal via array under a QFN or BGA ground pad "
            "is a normal use of via stitching, and this would block it."
        ))

        self.avoid_same_net_pads = wx.CheckBox(
            self, label=_("Avoid pads already on this net")
        )
        self.avoid_same_net_pads.SetValue(bool(saved.get("avoid_same_net_pads", DEFAULT_AVOID_SAME_NET_PADS)))
        self.avoid_same_net_pads.SetToolTip(_(
            "Keep vias off the copper of pads that are already on the net being "
            "stitched.\n\n"
            "Off by default: dropping a via array straight onto a QFN or BGA "
            "thermal pad is a normal use of stitching, and this would block it.\n\n"
            "Tick this to leave same-net pads alone, for example to keep vias out "
            "of a paste-critical pad. Pads on every other net are avoided either "
            "way, with their full clearance."
        ))

        self.main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Ctrl+Z does undo a run here, unlike on the SWIG build, but only until
        # the board is saved and reopened. This removes a run by its group at
        # any point after that, and keeps the dialog identical on both builds.
        self.remove_run_btn = wx.Button(self, label=_("Reset last run"))
        self.remove_run_btn.Bind(wx.EVT_BUTTON, lambda evt: self._on_remove_last_run())
        self.remove_run_label = wx.StaticText(self, label="")
        top_row = wx.BoxSizer(wx.HORIZONTAL)
        top_row.Add(self.remove_run_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        top_row.Add(self.remove_run_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        self.main_sizer.Add(top_row, 0, wx.EXPAND | wx.ALL, 5)
        self._refresh_remove_run_btn()

        self._make_group(self.main_sizer, [
            (_("Net Name:"), self.net)
        ])

        self._make_group(self.main_sizer, [
            (_("Via Type:"), self.via_type),
            (_("Start Layer:"), self.start_layer),
            (_("End Layer:"), self.end_layer),
            (_("Via Diameter (mm):"), self.via_dia),
            (_("Drill (mm):"), self.drill),
        ])

        # Live, non-blocking indicator for _via_type_advisory. Never a dialog
        # someone has to click through: it just reflects what the current via
        # type/layer combination looks like, and updates as those change.
        self.advisory_label = wx.StaticText(self, label="")
        self.advisory_label.SetForegroundColour(wx.Colour(180, 95, 0))
        self.advisory_label.Hide()
        self.main_sizer.Add(self.advisory_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self._make_group(self.main_sizer, [
            (_("Via Pattern:"), self.pattern),
            (_("Spacing (mm):"), self.spacing),
            (_("X-Offset (mm):"), self.x_offset),
            (_("Y-Offset (mm):"), self.y_offset),
            (_("Zones:"), self.avoid_zones),
            (_("Footprints:"), self.avoid_footprints),
            (_("Same-net pads:"), self.avoid_same_net_pads),
        ])

        # CreateButtonSizer, not a hand-built one: it orders OK/Cancel to match
        # the platform's own convention (OK-then-Cancel on Windows) instead of
        # a fixed left-to-right order that only matches some platforms.
        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)

        self.reset_btn = wx.Button(self, label=_("Reset settings"))
        self.reset_btn.SetToolTip(_(
            "Restore the built-in defaults and clear the settings saved from "
            "previous runs."
        ))
        self.reset_btn.Bind(wx.EVT_BUTTON, lambda evt: self._on_reset())

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        button_row.Add(self.reset_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        button_row.AddStretchSpacer(1)
        button_row.Add(buttons, 0, wx.EXPAND)

        self.main_sizer.AddSpacer(15)
        self.main_sizer.Add(button_row, 0, wx.EXPAND)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self.main_sizer, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(outer)

        # Lock Start/End Layer to F.Cu/B.Cu if the initial via type (Through
        # by default, or the preselected via's type) is Through.
        self._refresh_layer_controls()
        self._update_advisory()

    def _set_layer_combo_choices(self, combo, names, keep):
        """Repopulate a layer BitmapComboBox, keeping `keep` selected if still valid."""
        combo.Clear()
        for name in names:
            rgb = self._layer_colors.get(self.layer_map[name], (128, 128, 128))
            combo.Append(name, _color_swatch(rgb))
        if names:
            combo.SetStringSelection(keep if keep in names else names[0])
        combo.Enable(len(names) > 1)

    def _refresh_layer_controls(self):
        """Lock Start/End Layer to F.Cu/B.Cu for Through, since that's always
        true by definition (kipy's own Via.type setter enforces it) - not a
        preference, so there's nothing to lose by fixing it. Every other via
        type is left fully free: via type is a label for what KiCad should
        treat the via as, not a hard constraint on which layers can be
        picked. A stricter version used to snap Start/End back to a
        "standard" template on every change, which fought both pasting an
        existing via's settings and deliberately atypical vias (e.g.
        stitching two ground planes through an intermediate signal layer,
        issue #7). `_via_type_advisory` catches a genuine mismatch instead,
        as a confirm-or-cancel prompt before committing rather than by
        disabling fields.
        """
        names = self._all_layer_names
        if self.via_type.GetStringSelection() == "Through":
            self._set_layer_combo_choices(self.start_layer, [names[0]], names[0])
            self._set_layer_combo_choices(self.end_layer, [names[-1]], names[-1])
        else:
            self._set_layer_combo_choices(
                self.start_layer, names, self.start_layer.GetStringSelection()
            )
            self._set_layer_combo_choices(
                self.end_layer, names, self.end_layer.GetStringSelection()
            )

    def _on_via_type(self):
        """Via type changed: re-derive the layer controls, then the size defaults."""
        self._refresh_layer_controls()
        self._apply_via_type_defaults()
        self._update_advisory()

    def _on_start_layer(self):
        """Start layer changed: re-derive the other layer control, then the advisory."""
        self._refresh_layer_controls()
        self._update_advisory()

    def _update_advisory(self):
        """Refresh the inline via-type advisory label from the current controls.

        Reads the via type and layers directly off the controls rather than via
        values(), which also validates the size/net fields and would raise
        ValueError while those are mid-edit -- irrelevant to this advisory.
        """
        via_type = self.VIA_TYPE_CHOICES[self.via_type.GetStringSelection()]
        start_layer = self.layer_map.get(self.start_layer.GetStringSelection())
        end_layer = self.layer_map.get(self.end_layer.GetStringSelection())

        advisory = None
        if start_layer is not None and end_layer is not None and start_layer != end_layer:
            advisory = _via_type_advisory(self.board, via_type, start_layer, end_layer)

        if advisory:
            self.advisory_label.SetLabel(advisory)
            self.advisory_label.Wrap(380)
            self.advisory_label.Show()
        else:
            self.advisory_label.SetLabel("")
            self.advisory_label.Hide()
        self.main_sizer.Layout()
        self.Fit()

    def _refresh_remove_run_btn(self):
        try:
            runs = stitching_runs(self.board)
        except Exception:
            runs = []  # a board read must never stop the dialog from opening
        self.remove_run_btn.Enable(bool(runs))
        if runs:
            group, vias = runs[-1]
            run = group.proto.name[len(GROUP_PREFIX):] or group.proto.name
            self.remove_run_label.SetLabel(
                _("{run}, {count} vias").format(run=run, count=len(vias))
            )
            self.remove_run_btn.SetToolTip(
                _("Delete the {count} vias placed by '{run}'.").format(
                    count=len(vias), run=run
                )
            )
        else:
            self.remove_run_label.SetLabel(_("No stitching run on this board"))
            self.remove_run_btn.SetToolTip(
                _("Only vias placed by this plugin, and still grouped, can be "
                  "removed this way.")
            )
        self.remove_run_label.SetForegroundColour(
            wx.SystemSettings.GetColour(
                wx.SYS_COLOUR_GRAYTEXT if not runs else wx.SYS_COLOUR_WINDOWTEXT
            )
        )
        self.Layout()

    def _on_remove_last_run(self):
        try:
            runs = stitching_runs(self.board)
        except Exception as exc:
            _report(self, _("Removing the stitching run hit an unexpected error."), exc)
            return
        if not runs:
            self._refresh_remove_run_btn()
            return
        group, vias = runs[-1]
        confirm = wx.MessageBox(
            _("Remove the {count} vias placed by '{run}'?").format(
                count=len(vias), run=group.proto.name
            ),
            _("Reset last run"),
            wx.YES_NO | wx.ICON_WARNING | wx.STAY_ON_TOP, self,
        )
        if confirm != wx.YES:
            return
        try:
            # The group goes with its vias: emptying it leaves a stray group
            # behind in the board tree.
            self.board.remove_items(list(vias) + [group])
            self.board.refill_zones(block=False)
        except Exception as exc:
            _report(self, _("Removing the stitching run hit an unexpected error."), exc)
        self._refresh_remove_run_btn()

    def _on_reset(self):
        """Restore the hardcoded defaults and clear whatever was saved from a
        previous run, undoing both persistence and any in-progress edits."""
        _clear_settings()

        self.via_type.SetSelection(0)  # "Through"
        self._refresh_layer_controls()

        self.via_dia.SetValue(str(DEFAULT_VIA_DIAMETER_MM))
        self.drill.SetValue(str(DEFAULT_DRILL_MM))
        self.spacing.SetValue(str(DEFAULT_SPACING_MM))
        self._auto_values = {
            str(DEFAULT_VIA_DIAMETER_MM), str(DEFAULT_DRILL_MM), str(DEFAULT_SPACING_MM)
        }

        self.pattern.SetSelection(PATTERNS.index(DEFAULT_PATTERN))
        self.x_offset.SetValue(str(to_mm(0.0)))
        self.y_offset.SetValue(str(to_mm(0.0)))

        net_choices = [self.net.GetString(i) for i in range(self.net.GetCount())]
        self.net.SetValue(DEFAULT_NET if DEFAULT_NET in net_choices else "")

        self.avoid_zones.SetValue(DEFAULT_AVOID_OTHER_ZONES)
        self.avoid_footprints.SetValue(DEFAULT_AVOID_FOOTPRINTS)
        self.avoid_same_net_pads.SetValue(DEFAULT_AVOID_SAME_NET_PADS)

        self._update_advisory()

    def _settings_from_values(self, values):
        """Convert a values() dict into the JSON-able form _save_settings expects.

        Layers and via type are saved by name, not by the raw ids/ints in
        values(), so a saved run still applies to a board whose layer table
        happens to enumerate differently.
        """
        return {
            "via_type_name": self.VIA_TYPE_NAMES.get(values["via_type"]),
            "start_layer_name": self.layer_names.get(values["start_layer"]),
            "end_layer_name": self.layer_names.get(values["end_layer"]),
            "via_dia_mm": values["via_dia_mm"],
            "drill_mm": values["drill_mm"],
            "spacing_mm": values["spacing_mm"],
            "pattern": values["pattern"],
            "net_name": values["net_name"],
            "x_offset_mm": values["x_offset_mm"],
            "y_offset_mm": values["y_offset_mm"],
            "avoid_other_zones": values["avoid_other_zones"],
            "avoid_footprints": values["avoid_footprints"],
            "avoid_same_net_pads": values["avoid_same_net_pads"],
        }

    def _apply_via_type_defaults(self):
        """Swap the via/microvia size defaults in, without discarding real input.

        A microvia at the 0.6 mm through-via default is not manufacturable, so
        picking Micro should move diameter, drill and spacing. It must not move
        them over a value copied from a preselected via or typed by hand, so only
        a field still holding a value this dialog put there is fair game. That is
        also why this hangs off the via type alone: _refresh_layer_controls runs
        from __init__ and on every Start Layer change too, and resetting the sizes
        from there wiped both the preselected via's settings and the user's own.
        """
        if self.via_type.GetStringSelection() == "Micro":
            wanted = (
                DEFAULT_MICROVIA_DIAMETER_MM,
                DEFAULT_MICROVIA_DRILL_MM,
                DEFAULT_MICROVIA_SPACING_MM,
            )
        else:
            wanted = (DEFAULT_VIA_DIAMETER_MM, DEFAULT_DRILL_MM, DEFAULT_SPACING_MM)

        wanted = [str(v) for v in wanted]
        for ctrl, value in zip((self.via_dia, self.drill, self.spacing), wanted):
            if ctrl.GetValue() in self._auto_values:
                ctrl.SetValue(value)
        self._auto_values.update(wanted)

    def _make_group(self, parent_sizer, rows):
        """rows: list of (label, control) pairs."""
        box = wx.StaticBoxSizer(wx.VERTICAL, self, "")
        grid = wx.FlexGridSizer(0, 2, 5, 5)     # rows grow, 2 cols
        grid.AddGrowableCol(1)
        for label, ctrl in rows:
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 1, wx.EXPAND)
        box.Add(grid, 1, wx.EXPAND | wx.ALL, 5)
        parent_sizer.Add(box, 0, wx.EXPAND | wx.ALL, 5)

    def values(self):
        """Validated dialog values. ValueError carries a user-facing message."""

        via_type_name = self.via_type.GetStringSelection()
        via_type = self.VIA_TYPE_CHOICES[via_type_name]

        start_layer = self.layer_map[self.start_layer.GetStringSelection()]
        end_layer = self.layer_map[self.end_layer.GetStringSelection()]

        if start_layer == end_layer:
            raise ValueError(
                _("Start and end layers must not be the same ({layer})").format(layer=start_layer)
            )

        try:
            via_dia_mm = float(self.via_dia.GetValue())
            drill_mm = float(self.drill.GetValue())
            spacing_mm = float(self.spacing.GetValue())
            x_offset_mm = float(self.x_offset.GetValue())
            y_offset_mm = float(self.y_offset.GetValue())
        except ValueError:
            raise ValueError(_("Via diameter, drill and spacing and offsets must be numbers (mm)."))

        # isfinite first: nan parses fine as a float and then compares False
        # against everything, so it would slip past both checks below and only
        # blow up later, inside from_mm().
        if not all(math.isfinite(v) for v in (via_dia_mm, drill_mm, spacing_mm)):
            raise ValueError(
                _("Via diameter, drill and spacing must be real numbers (mm).")
            )
        if min(via_dia_mm, drill_mm, spacing_mm) <= 0:
            raise ValueError(
                _("Via diameter, drill and spacing must all be greater than zero.")
            )
        if drill_mm >= via_dia_mm:
            raise ValueError(
                _(
                    "The drill ({drill} mm) must be smaller than the via diameter "
                    "({dia} mm)."
                ).format(drill=drill_mm, dia=via_dia_mm)
            )

        net_name = self.net.GetValue().strip()
        if not net_name:
            raise ValueError(_("Pick the net to stitch."))

        return {
            "via_type": via_type,
            "start_layer": start_layer,
            "end_layer": end_layer,
            "via_dia_mm": via_dia_mm,
            "drill_mm": drill_mm,
            "spacing_mm": spacing_mm,
            "pattern": PATTERNS[self.pattern.GetSelection()],
            "net_name": net_name,
            "x_offset_mm": x_offset_mm,
            "y_offset_mm": y_offset_mm,
            "avoid_other_zones": self.avoid_zones.GetValue(),
            "avoid_footprints": self.avoid_footprints.GetValue(),
            "avoid_same_net_pads": self.avoid_same_net_pads.GetValue(),
        }


class ErrorDialog(wx.Dialog):
    """Unexpected-failure report. Selectable, so Ctrl+A and Ctrl+C paste into a bug."""

    def __init__(self, parent, summary, details):
        super().__init__(
            parent,
            title=_("Via Stitching Error"),
            size=(660, 420),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.STAY_ON_TOP,
        )

        label = wx.StaticText(self, label=summary)
        label.Wrap(620)

        text = wx.TextCtrl(
            self,
            value=details,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        text.SetFont(wx.Font(wx.FontInfo(9).Family(wx.FONTFAMILY_TELETYPE)))

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        outer.Add(text, 1, wx.EXPAND | wx.ALL, 12)
        outer.Add(
            self.CreateButtonSizer(wx.OK), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12
        )
        self.SetSizer(outer)


def _api_enabled_in_config():
    """Read KiCad's own API setting. True, False, or None if we cannot tell.

    KiCad only binds the API socket while starting up, so "switched off" and
    "switched on after KiCad launched" both look like a refused connection from
    here. The setting on disk is what tells them apart.
    """
    try:
        for config_dir in kicad_config_dirs():
            path = os.path.join(config_dir, "kicad_common.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    return bool(json.load(fh).get("api", {}).get("enable_server"))
    except Exception:
        pass
    return None


def _is_dial_failure(exc):
    """True if this ConnectionError means nothing answered, not that a reply was late.

    kipy raises the same type for both. It phrases a refused socket as "Failed to
    connect to KiCad", and a late reply as "Error receiving reply from KiCad".
    """
    return "Failed to connect" in str(exc)


def _is_busy(exc):
    """True if KiCad refused the call because it is mid-operation.

    This plugin's own parting refill_zones(block=False) is the usual cause: KiCad
    answers AS_BUSY to everything until that fill finishes, so a second run
    started too soon fails on its very first call, board open or not.
    """
    return isinstance(exc, ApiError) and exc.code == ApiStatusCode.AS_BUSY


def _is_token_mismatch(exc):
    """True if we reached a different KiCad than the one that launched us.

    KiCad serves the API on one socket per machine and the instance that starts
    first keeps it. A second instance still hands its own KICAD_API_TOKEN to the
    plugins it launches, so running this from the second one dials the socket,
    reaches the first, and is turned away on the token. It looks exactly like
    "no board open" from here, which is the wrong thing to go and fix, so it
    gets told apart by KiCad's own status code rather than by guesswork.
    """
    return isinstance(exc, ApiError) and exc.code == ApiStatusCode.AS_TOKEN_MISMATCH


BUSY_HELP = _(
    "KiCad is busy and refused the request.\n\n"
    "It is most likely still refilling the zones from a previous run. Wait for "
    "the PCB editor to go idle, then run this again."
)

TOKEN_HELP = _(
    "Another KiCad instance owns the plugin API.\n\n"
    "KiCad serves the API on a single socket, and whichever instance started "
    "first holds it. This plugin was launched from a different one, so the "
    "instance that answered refused the request as coming from elsewhere.\n\n"
    "Close the other KiCad windows, then reopen the board you want to stitch "
    "and run this again. A KiCad only claims the socket while it is starting "
    "up, so the instance you keep has to be restarted, not just left open."
)

NO_BOARD_HELP = _(
    "KiCad's API server answered but did not hand over a board.\n\n"
    "Open a board in the PCB editor and run this again."
)


def _connection_help(enabled, dial_failed=True):
    """Wording for a failed connection, given the API setting on disk.

    kipy raises the same ConnectionError for a refused socket and for a reply
    timeout. Only the first says anything about the server not running, so the
    confident advice is gated on dial_failed.
    """
    if not dial_failed:
        return _(
            "KiCad did not reply in time.\n\n"
            "It is probably busy, for example filling zones. Wait for it to "
            "finish and run this again."
        )
    if enabled is False:
        # Never mention restarting on its own here: the setting is the real problem.
        return _(
            "KiCad's API server is switched off.\n\n"
            "Turn on 'Enable KiCad API' in Preferences > Plugins, then restart "
            "KiCad. The server is only started while KiCad launches, so the "
            "setting does not take effect until then."
        )
    if enabled is True:
        return _(
            "KiCad's API server is enabled but not listening.\n\n"
            "Restart KiCad. The server is only started while KiCad launches, so "
            "switching it on in Preferences does nothing for an instance that is "
            "already running.\n\n"
            "Then open a board in the PCB editor and run this again."
        )
    return _(
        "Could not talk to KiCad.\n\n"
        "Check that 'Enable KiCad API' is on in Preferences > Plugins, restart "
        "KiCad, and make sure a board is open in the PCB editor."
    )


def _to_stderr(text):
    """KiCad 10.0.1+ shows plugin stderr in the editor's notification area."""
    try:
        print(text, file=sys.stderr or sys.__stderr__)
    except Exception:
        pass


def _msg(parent, text, style):
    """Message box that stays above the PCB editor."""
    wx.MessageBox(text, _("Via Stitching"), style | wx.STAY_ON_TOP, parent)


def _report(parent, summary, exc):
    """Show an unexpected failure with everything a bug report needs."""
    details = "\n".join(
        [f"{type(exc).__name__}: {exc}", ""] + _ENV + ["", traceback.format_exc()]
    )
    _to_stderr(summary + "\n" + details)
    try:
        dlg = ErrorDialog(parent, summary, details)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
    except Exception:
        # No usable wx, so stderr above is all the reporting we get.
        pass


def main():
    app = wx.App()  # noqa: F841  (must outlive every window below)
    prepare_app()

    _ENV.extend(
        [
            f"Via Stitching {VERSION}",
            f"kicad-python {_kipy_version()}",
            f"Python {sys.version.split()[0]} on {sys.platform}",
        ]
    )

    try:
        kicad = KiCad(timeout_ms=API_TIMEOUT_MS)
        board = kicad.get_board()
        try:
            _ENV.append(f"KiCad {kicad.get_version()}")
        except Exception:
            pass
        net_names = sorted({n.name for n in board.get_nets() if n.name})
    except KiCadConnectionError as exc:
        # If kipy's wording ever changes we fall back to the vaguer message rather
        # than telling someone to restart KiCad for no reason.
        _report(
            None,
            _connection_help(_api_enabled_in_config(), _is_dial_failure(exc)),
            exc,
        )
        return
    except Exception as exc:
        # The server answered, so it is running. What is left is which KiCad
        # answered, and what state it was in: a second instance holding the
        # socket, KiCad still busy with our own parting zone refill, or no board.
        if _is_token_mismatch(exc):
            help_text = TOKEN_HELP
        elif _is_busy(exc):
            help_text = BUSY_HELP
        else:
            help_text = NO_BOARD_HELP
        _report(None, help_text, exc)
        return

    # The plugin runs as its own process. Give the dialog editor-attached window
    # behavior: no taskbar button on Windows, and a floating window that joins the
    # PCB editor's Stage Manager set on macOS.
    dlg = ViaStitchingDialog(None, net_names, board)
    make_tool_window(dlg)
    attach_to_stage_manager(dlg)
    dlg.CentreOnScreen()

    # The dialog stays visible to the end so every message box has a real parent.
    try:
        if dlg.ShowModal() != wx.ID_OK:
            return
        try:
            params = dlg.values()
        except ValueError as exc:
            _msg(dlg, str(exc), wx.OK | wx.ICON_ERROR)
            return
        _ENV.append(
            "Parameters: " + ", ".join(f"{k}={v}" for k, v in sorted(params.items()))
        )

        # Save regardless of whether the stitch below succeeds: these are the
        # dialog's own values at the moment OK was pressed, not a promise the
        # stitch worked. The via-type/layer advisory is now shown live inside
        # the dialog itself (see ViaStitchingDialog._update_advisory), so there
        # is nothing left to confirm here.
        _save_settings(dlg._settings_from_values(params))

        busy = wx.BusyCursor()
        try:
            count, grouped = stitch(board, parent=dlg, **params)
        except (RuntimeError, ValueError) as exc:
            # Conditions we raise ourselves, already worded for the user.
            del busy
            _msg(dlg, str(exc), wx.OK | wx.ICON_ERROR)
            return
        except Exception as exc:
            del busy
            _report(
                dlg,
                BUSY_HELP if _is_busy(exc) else _("Via Stitching failed while placing vias."),
                exc,
            )
            return
        del busy

        if count:
            text = _("Placed {count} stitching vias on net '{net}'.").format(
                count=count, net=params["net_name"]
            )
            if not grouped:
                text += "\n\n" + _(
                    "KiCad would not group them, so they delete individually "
                    "rather than as a set. The vias themselves are fine."
                )
            _msg(dlg, text, wx.OK | wx.ICON_INFORMATION)
    finally:
        dlg.Destroy()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # nothing may exit silently
        _report(None, "Via Stitching hit an unexpected error.", exc)
