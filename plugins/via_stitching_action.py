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
import sys
import traceback
from collections import defaultdict

import wx

from kipy import KiCad
from kipy.errors import ApiError, ConnectionError as KiCadConnectionError
from kipy.proto.common import ApiStatusCode
from kipy.board_types import Group, Via, ArcTrack, BoardLayer, PadType, PSS_CIRCLE
from kipy.geometry import Vector2
from kipy.util import from_mm

# Make the sibling helper modules importable regardless of how KiCad launches us.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mac_dialog import attach_to_stage_manager, prepare_app  # noqa: E402
from _win_dialog import make_tool_window  # noqa: E402

VERSION = "1.0.2"

# shapely (plus the numpy and GEOS it drags in) costs the better part of a second
# to import, more than the rest of start-up together, so the geometry functions
# import it lazily and the dialog is on screen before any of it is paid.

# kipy defaults to 2 s, which get_zones() or a few hundred vias blow through on a
# real board. Every miss surfaces as a lost connection mid-edit.
API_TIMEOUT_MS = 30000


# --- Dialog defaults --------------------------------------------------------
DEFAULT_VIA_DIAMETER_MM = 0.6
DEFAULT_DRILL_MM = 0.3
DEFAULT_SPACING_MM = 2.0
DEFAULT_NET = "GND"
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


def _layer_region(zones, net_name):
    """Return {layer: shapely geometry} of the filled copper of `net_name`."""
    from shapely.ops import unary_union

    per_layer = {}
    for zone in zones:
        net = zone.net
        if net is None or net.name != net_name:
            continue
        for layer, shapes in zone.filled_polygons.items():
            for pwh in shapes:
                poly = _polygon_with_holes_to_shapely(pwh)
                if poly is not None:
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


def _keepout_shapes(board, via_radius_nm):
    """Drill keepouts around existing vias and through-hole pads.

    Prevents new stitching vias from violating hole-to-hole clearance.
    """
    from shapely.geometry import Point

    margin = from_mm(HOLE_MARGIN_MM)
    circles = []

    for via in board.get_vias():
        r = via_radius_nm + via.drill_diameter // 2 + margin
        circles.append(Point(via.position.x, via.position.y).buffer(r, quad_segs=8))

    for pad in board.get_pads():
        if pad.pad_type not in (PadType.PT_PTH, PadType.PT_NPTH):
            continue
        try:
            # A drill is a Vector2 because it may be a milled slot, so the long
            # axis is what a round keepout has to cover.
            drill = pad.padstack.drill.diameter
            hole_r = max(drill.x, drill.y) // 2
        except Exception:
            hole_r = 0
        if hole_r <= 0:
            continue
        r = via_radius_nm + hole_r + margin
        circles.append(Point(pad.position.x, pad.position.y).buffer(r, quad_segs=8))

    return circles


def _track_keepout_shapes(board, net_name, via_radius_nm, clearances):
    """Clearance areas around other nets' tracks, on any copper layer.

    A through via's drill spans every copper layer of the board, not just the
    ones `net_name` happens to be poured on, so a track on a layer the pour
    never touches (an outer signal layer over an inner GND plane, say) still
    has to be avoided or the via drills straight through it.
    """
    from shapely.geometry import LineString

    shapes = []
    for track in board.get_tracks():
        if track.net.name == net_name:
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


def _zone_keepout_shapes(zones, net_name, via_radius_nm, clearances):
    """Clearance areas around other nets' filled zones, on any layer.

    A through via's drill spans every copper layer, so a filled zone for a
    different net on a layer `net_name` never pours on (an inner power plane
    under a GND-poured outer layer, say) can still be worth avoiding, even
    though KiCad's refill would clear the fill back around the via by itself.
    """
    shapes = []
    for zone in zones:
        net = zone.net
        if net is not None and net.name == net_name:
            continue
        margin = via_radius_nm + clearances[net.name if net is not None else ""]
        for filled in zone.filled_polygons.values():
            for pwh in filled:
                poly = _polygon_with_holes_to_shapely(pwh)
                if poly is not None:
                    shapes.append(poly.buffer(margin, quad_segs=8))

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


def _grid_points(bounds, spacing_nm, pattern):
    """Yield candidate (x, y) nm points across `bounds` for the given pattern."""
    minx, miny, maxx, maxy = bounds
    minx, miny = int(math.floor(minx)), int(math.floor(miny))
    maxx, maxy = int(math.ceil(maxx)), int(math.ceil(maxy))

    if pattern == "Hexagonal":
        row_pitch = int(spacing_nm * math.sqrt(3) / 2)
    else:
        row_pitch = spacing_nm
    if row_pitch <= 0:
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


def _make_via(x, y, diameter_nm, drill_nm, net):
    """Create a through Via with a valid single-layer PST_NORMAL padstack."""
    via = Via()  # defaults to VT_THROUGH with a PST_NORMAL, F_Cu-only padstack
    via.diameter = diameter_nm
    via.drill_diameter = drill_nm
    via.padstack.copper_layers[0].shape = PSS_CIRCLE
    via.position = Vector2.from_xy(int(x), int(y))
    via.net = net
    return via


def _group_vias(board, vias, net_name):
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
        # Group.name is read-only in kicad-python 0.7.x, the inherited proto is not.
        group.proto.name = f"ViaStitching {net_name}"
        group.items = vias  # stores the member KIIDs, so the vias must exist already
        return bool(board.create_items(group))
    except Exception:
        return False


def stitch(
    board,
    net_name,
    via_dia_mm,
    drill_mm,
    spacing_mm,
    pattern,
    avoid_other_zones=DEFAULT_AVOID_OTHER_ZONES,
    avoid_footprints=DEFAULT_AVOID_FOOTPRINTS,
    parent=None,
):
    """Run the stitching. Returns (vias placed, whether they were grouped)."""
    from shapely.geometry import Point
    from shapely.prepared import prep

    diameter_nm = from_mm(via_dia_mm)
    drill_nm = from_mm(drill_mm)
    spacing_nm = from_mm(spacing_mm)
    via_radius_nm = diameter_nm // 2

    # Resolve the real Net object (carries the proper proto for assignment).
    nets = board.get_nets()
    net = next((n for n in nets if n.name == net_name), None)
    if net is None:
        raise RuntimeError(f"Net '{net_name}' not found on the board.")

    zones = board.get_zones()
    regions = _layer_region(zones, net_name)
    if not regions:
        raise RuntimeError(
            f"No filled copper found for net '{net_name}'.\n"
            "Fill the zones first (press B in the PCB editor), then run again."
        )
    if len(regions) < 2:
        only = BoardLayer.Name(next(iter(regions)))
        raise RuntimeError(
            f"Net '{net_name}' only has filled copper on one layer ({only}).\n"
            "Via stitching needs the net poured on at least two layers."
        )

    # Region where a through via lands on copper on every layer the net is poured.
    region = None
    for geom in regions.values():
        region = geom if region is None else region.intersection(geom)
    if region is None or region.is_empty:
        raise RuntimeError("The selected net's planes do not overlap anywhere.")

    # Inset so the via body + clearance ring stay inside the fill on all layers.
    inset = via_radius_nm + from_mm(EDGE_EPS_MM)
    region = region.buffer(-inset)
    if region.is_empty:
        raise RuntimeError(
            "No room for vias after clearance inset. Try a smaller via diameter."
        )

    # Grid before subtracting the keepout, so "grid too coarse" and "every position
    # is taken by an existing hole" get different messages. The grid is anchored to
    # region.bounds, so it is not reproducible run to run.
    prepared = prep(region)
    candidates = [
        (x, y)
        for (x, y) in _grid_points(region.bounds, spacing_nm, pattern)
        if prepared.contains(Point(x, y))
    ]
    if not candidates:
        raise RuntimeError(
            "No via positions fit inside the overlap of the planes.\n"
            "Try a smaller spacing or via diameter."
        )

    # Every keepout as one flat list of shapes, tested through a single spatial
    # index. Avoid existing via/pad drill holes and other nets' tracks on any
    # layer, plus whatever else the dialog asked for.
    clearances = _net_clearances(board, nets, net_name)
    keepout = _keepout_shapes(board, via_radius_nm)
    keepout += _track_keepout_shapes(board, net_name, via_radius_nm, clearances)
    if avoid_other_zones:
        keepout += _zone_keepout_shapes(zones, net_name, via_radius_nm, clearances)
    if avoid_footprints:
        keepout += _footprint_keepout_shapes(board, net_name, via_radius_nm, clearances)

    blocked = _blocked_predicate(keepout)
    points = [(x, y) for (x, y) in candidates if not blocked(x, y)]
    if not points:
        # Only name the optional keepouts that are actually switched on, so this
        # never sends someone off to untick a box that is already off.
        optional = [
            name
            for name, on in (
                ("other nets' zones", avoid_other_zones),
                ("footprints", avoid_footprints),
            )
            if on
        ]
        blockers = " or ".join(["existing vias, pads or tracks"] + optional)
        untick = (
            " Or untick " + " or ".join(f"'Avoid {n}'" for n in optional) + "."
            if optional
            else ""
        )
        raise RuntimeError(
            f"All {len(candidates)} candidate positions are blocked by {blockers}.\n"
            "If these zones are already stitched, delete the previous vias first, "
            "or try a smaller spacing." + untick
        )

    if len(points) > VIA_COUNT_WARN:
        msg = (
            f"This will place {len(points)} vias, which may make KiCad slow.\n"
            "Increase the spacing for fewer vias.\n\nPlace them anyway?"
        )
        style = wx.YES_NO | wx.ICON_WARNING | wx.STAY_ON_TOP
        if wx.MessageBox(msg, "Many vias", style, parent) != wx.YES:
            return 0, False

    vias = [_make_via(x, y, diameter_nm, drill_nm, net) for (x, y) in points]

    # No explicit commit: one create_items call is already one undo step, and an
    # open commit can be seized or rolled back by anything else touching the editor.
    created = board.create_items(vias)
    if len(created) != len(vias):
        raise RuntimeError(
            f"KiCad accepted only {len(created)} of the {len(vias)} vias.\n"
            "Check that the drill is smaller than the via diameter."
        )

    # create_items reports what it echoed back, not what the board kept.
    placed = board.get_items_by_id([v.id for v in created])
    if len(placed) != len(created):
        raise RuntimeError(
            f"{len(created)} vias were created but only {len(placed)} are on the "
            "board. Nothing was rolled back, so check the board before saving."
        )

    grouped = _group_vias(board, created, net_name)

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

    def __init__(self, parent, net_names):
        super().__init__(
            parent,
            title="Via Stitching Parameters",
            style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
        )

        icon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "icon.png"
        )
        if os.path.exists(icon_path):
            self.SetIcon(wx.Icon(icon_path, wx.BITMAP_TYPE_PNG))

        grid = wx.FlexGridSizer(5, 2, 8, 10)
        grid.AddGrowableCol(1, 1)

        def add_row(label, ctrl):
            grid.Add(
                wx.StaticText(self, label=label),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            grid.Add(ctrl, 1, wx.EXPAND)

        self.via_dia = wx.TextCtrl(self, value=str(DEFAULT_VIA_DIAMETER_MM))
        self.drill = wx.TextCtrl(self, value=str(DEFAULT_DRILL_MM))
        self.spacing = wx.TextCtrl(self, value=str(DEFAULT_SPACING_MM))

        names = list(net_names)
        self.net = wx.ComboBox(self, choices=names, style=wx.CB_DROPDOWN)
        if DEFAULT_NET in names:
            self.net.SetValue(DEFAULT_NET)
        elif names:
            self.net.SetSelection(0)

        self.pattern = wx.Choice(self, choices=PATTERNS)
        self.pattern.SetSelection(PATTERNS.index(DEFAULT_PATTERN))

        add_row("Via Diameter (mm):", self.via_dia)
        add_row("Drill (mm):", self.drill)
        add_row("Spacing (mm):", self.spacing)
        add_row("Net Name:", self.net)
        add_row("Pattern:", self.pattern)

        self.avoid_zones = wx.CheckBox(self, label="Avoid other nets' zones")
        self.avoid_zones.SetValue(DEFAULT_AVOID_OTHER_ZONES)
        self.avoid_zones.SetToolTip(
            "Keep vias out of other nets' filled copper on every layer.\n\n"
            "Off by default: a via through another net's pour is not a DRC "
            "error, because KiCad clears the fill back around it when the "
            "zones are refilled.\n\n"
            "Tick this to leave an inner power plane unperforated. Expect far "
            "fewer vias, since such a plane often covers most of the board."
        )

        self.avoid_footprints = wx.CheckBox(self, label="Avoid footprints")
        self.avoid_footprints.SetValue(DEFAULT_AVOID_FOOTPRINTS)
        self.avoid_footprints.SetToolTip(
            "Keep vias out from under every component's bounding box.\n\n"
            "This is about mechanical fit, not clearance, so it applies "
            "whatever net the footprint is on.\n\n"
            "Off by default: a thermal via array under a QFN or BGA ground pad "
            "is a normal use of via stitching, and this would block it."
        )

        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(grid, 1, wx.EXPAND | wx.ALL, 12)
        outer.Add(self.avoid_zones, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        outer.Add(self.avoid_footprints, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        outer.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizerAndFit(outer)

    def values(self):
        """Validated dialog values. ValueError carries a user-facing message."""
        try:
            via_dia_mm = float(self.via_dia.GetValue())
            drill_mm = float(self.drill.GetValue())
            spacing_mm = float(self.spacing.GetValue())
        except ValueError:
            raise ValueError("Via diameter, drill and spacing must be numbers (mm).")

        # isfinite first: nan parses fine as a float and then compares False
        # against everything, so it would slip past both checks below and only
        # blow up later, inside from_mm().
        if not all(math.isfinite(v) for v in (via_dia_mm, drill_mm, spacing_mm)):
            raise ValueError(
                "Via diameter, drill and spacing must be real numbers (mm)."
            )
        if min(via_dia_mm, drill_mm, spacing_mm) <= 0:
            raise ValueError(
                "Via diameter, drill and spacing must all be greater than zero."
            )
        if drill_mm >= via_dia_mm:
            raise ValueError(
                f"The drill ({drill_mm} mm) must be smaller than the via diameter "
                f"({via_dia_mm} mm)."
            )

        net_name = self.net.GetValue().strip()
        if not net_name:
            raise ValueError("Pick the net to stitch.")

        return {
            "via_dia_mm": via_dia_mm,
            "drill_mm": drill_mm,
            "spacing_mm": spacing_mm,
            "net_name": net_name,
            "pattern": PATTERNS[self.pattern.GetSelection()],
            "avoid_other_zones": self.avoid_zones.GetValue(),
            "avoid_footprints": self.avoid_footprints.GetValue(),
        }


class ErrorDialog(wx.Dialog):
    """Unexpected-failure report. Selectable, so Ctrl+A and Ctrl+C paste into a bug."""

    def __init__(self, parent, summary, details):
        super().__init__(
            parent,
            title="Via Stitching Error",
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
    root = os.environ.get("KICAD_CONFIG_HOME")
    if not root:
        if sys.platform == "win32":
            root = os.path.join(os.environ.get("APPDATA", ""), "kicad")
        elif sys.platform == "darwin":
            root = os.path.expanduser("~/Library/Preferences/kicad")
        else:
            root = os.path.join(
                os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                "kicad",
            )
    try:
        # Version subdirectories, newest first, so KiCad 11 wins over 10.
        versions = sorted(
            (d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))),
            key=lambda d: [int(p) for p in d.split(".") if p.isdigit()] or [0],
            reverse=True,
        )
        for name in versions:
            path = os.path.join(root, name, "kicad_common.json")
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


BUSY_HELP = (
    "KiCad is busy and refused the request.\n\n"
    "It is most likely still refilling the zones from a previous run. Wait for "
    "the PCB editor to go idle, then run this again."
)

NO_BOARD_HELP = (
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
        return (
            "KiCad did not reply in time.\n\n"
            "It is probably busy, for example filling zones. Wait for it to "
            "finish and run this again."
        )
    if enabled is False:
        # Never mention restarting on its own here: the setting is the real problem.
        return (
            "KiCad's API server is switched off.\n\n"
            "Turn on 'Enable KiCad API' in Preferences > Plugins, then restart "
            "KiCad. The server is only started while KiCad launches, so the "
            "setting does not take effect until then."
        )
    if enabled is True:
        return (
            "KiCad's API server is enabled but not listening.\n\n"
            "Restart KiCad. The server is only started while KiCad launches, so "
            "switching it on in Preferences does nothing for an instance that is "
            "already running.\n\n"
            "Then open a board in the PCB editor and run this again."
        )
    return (
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
    wx.MessageBox(text, "Via Stitching", style | wx.STAY_ON_TOP, parent)


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
        # The server answered, so it is running. Something else went wrong: KiCad
        # still busy with our own parting zone refill, or no board open.
        _report(None, BUSY_HELP if _is_busy(exc) else NO_BOARD_HELP, exc)
        return

    # The plugin runs as its own process. Give the dialog editor-attached window
    # behavior: no taskbar button on Windows, and a floating window that joins the
    # PCB editor's Stage Manager set on macOS.
    dlg = ViaStitchingDialog(None, net_names)
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
                BUSY_HELP if _is_busy(exc) else "Via Stitching failed while placing vias.",
                exc,
            )
            return
        del busy

        if count:
            text = f"Placed {count} stitching vias on net '{params['net_name']}'."
            if not grouped:
                text += (
                    "\n\nKiCad would not group them, so they delete individually "
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
