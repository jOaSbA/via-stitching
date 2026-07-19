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

import math
import os
import sys

import wx

from kipy import KiCad
from kipy.errors import ConnectionError as KiCadConnectionError
from kipy.board_types import Via, BoardLayer, PadType, PSS_CIRCLE
from kipy.geometry import Vector2
from kipy.util import from_mm

# Make the sibling helper module importable regardless of how KiCad launches us.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mac_dialog import attach_to_stage_manager, prepare_app  # noqa: E402
from _win_dialog import get_foreground_hwnd, attach_to_kicad  # noqa: E402

# shapely (plus the numpy and GEOS it drags in) costs about 1.6 s to import,
# which is most of the plugin's start-up time. We only need it once the user
# clicks OK, so it's imported lazily inside the geometry functions below instead
# of at module load. That way the dialog shows up almost immediately.


# --- Dialog defaults --------------------------------------------------------
DEFAULT_VIA_DIAMETER_MM = 0.6
DEFAULT_DRILL_MM = 0.3
DEFAULT_SPACING_MM = 2.0
DEFAULT_NET = "GND"
PATTERNS = ["Hexagonal", "Square", "Staggered"]
DEFAULT_PATTERN = "Square"

# Safety margins applied silently (the dialog has no clearance field, by design).
# A via placed at least (via_radius + EDGE_EPS) inside the zone fill stays clear of
# other-net copper, because KiCad already pulled the fill back by the board
# clearance when filling. EDGE_EPS just absorbs polygon rounding.
EDGE_EPS_MM = 0.05
# Hole-to-hole margin kept between a new via drill and existing via/pad drills.
HOLE_MARGIN_MM = 0.25

# Warn before placing more than this many vias (keeps KiCad responsive).
VIA_COUNT_WARN = 5000


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


def _keepout_region(board, new_via_radius_nm):
    """Union of drill keepouts around existing vias and through-hole pads.

    Prevents new stitching vias from violating hole-to-hole clearance.
    """
    from shapely.geometry import Point
    from shapely.ops import unary_union

    margin = from_mm(HOLE_MARGIN_MM)
    circles = []

    for via in board.get_vias():
        r = new_via_radius_nm + via.diameter // 2 + margin
        circles.append(Point(via.position.x, via.position.y).buffer(r, quad_segs=8))

    for pad in board.get_pads():
        if pad.pad_type not in (PadType.PT_PTH, PadType.PT_NPTH):
            continue
        try:
            hole_r = pad.padstack.drill.diameter.x // 2
        except Exception:
            hole_r = 0
        if hole_r <= 0:
            continue
        r = new_via_radius_nm + hole_r + margin
        circles.append(Point(pad.position.x, pad.position.y).buffer(r, quad_segs=8))

    return unary_union(circles) if circles else None


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
    via = Via()  # defaults to VT_THROUGH with PST_NORMAL padstack
    ps = via.padstack
    if not ps.copper_layers:
        # Fresh padstack has no copper layers; add one (PST_NORMAL uses index 0
        # to describe the via on all copper layers).
        ps._add_copper_layer(BoardLayer.BL_F_Cu)
    ps.copper_layers[0].shape = PSS_CIRCLE
    via.diameter = diameter_nm
    via.drill_diameter = drill_nm
    via.position = Vector2.from_xy(int(x), int(y))
    via.net = net
    return via


def _group_vias(kicad, board, vias, net_name):
    """Group vias through KiCad's native editor action.

    KiCad 10.0.1 cannot create a Group through ``board.create_items`` (the
    server silently drops it), even if the member vias were committed first.
    Selection plus the editor's own Group Items action uses the proven native
    path and works across KiCad 10 releases.
    """
    if not vias:
        return None

    before_group_ids = {group.id.value for group in board.get_groups()}
    board.clear_selection()
    board.add_to_selection(vias)
    # Grouping moved to the common editor actions in KiCad 10. The old
    # pcbnew.EditorControl.group name is accepted by run_action as a request but
    # does nothing, leaving all the vias selected and ungrouped.
    kicad.run_action("common.Interactive.group")

    new_groups = [
        group
        for group in board.get_groups()
        if group.id.value not in before_group_ids
    ]
    if not new_groups:
        raise RuntimeError("KiCad did not create a group for the stitching vias.")

    group = new_groups[0]
    # Group.name is read-only in kicad-python 0.7.x, but the inherited public
    # proto is mutable like the other wrapper types.
    group.proto.name = f"ViaStitching {net_name}"
    [group] = board.update_items(group)
    return group


def stitch(
    kicad, board, net_name, via_dia_mm, drill_mm, spacing_mm, pattern, parent=None
):
    """Run the stitching. Returns the number of vias placed."""
    from shapely.geometry import Point
    from shapely.prepared import prep

    diameter_nm = from_mm(via_dia_mm)
    drill_nm = from_mm(drill_mm)
    spacing_nm = from_mm(spacing_mm)
    via_radius_nm = diameter_nm // 2

    # Resolve the real Net object (carries the proper proto for assignment).
    net = next((n for n in board.get_nets() if n.name == net_name), None)
    if net is None:
        raise RuntimeError(f"Net '{net_name}' not found on the board.")

    regions = _layer_region(board.get_zones(), net_name)
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

    # Avoid existing via/pad drill holes.
    keepout = _keepout_region(board, via_radius_nm)
    if keepout is not None:
        region = region.difference(keepout)
    if region.is_empty:
        raise RuntimeError("No free area left for stitching vias.")

    prepared = prep(region)
    points = [
        (x, y)
        for (x, y) in _grid_points(region.bounds, spacing_nm, pattern)
        if prepared.contains(Point(x, y))
    ]

    if not points:
        raise RuntimeError(
            "No via positions fit. Try a smaller spacing or via diameter."
        )

    if len(points) > VIA_COUNT_WARN:
        msg = (
            f"This will place {len(points)} vias, which may make KiCad slow.\n"
            "Increase the spacing for fewer vias.\n\nPlace them anyway?"
        )
        if wx.MessageBox(msg, "Many vias", wx.YES_NO | wx.ICON_WARNING, parent) != wx.YES:
            return 0

    vias = [_make_via(x, y, diameter_nm, drill_nm, net) for (x, y) in points]

    # Remember which vias already existed so we can identify the new ones.
    before_ids = {v.id.value for v in board.get_vias()}

    commit = board.begin_commit()
    try:
        board.create_items(vias)
        board.push_commit(commit, "Via Stitching")
    except Exception:
        board.drop_commit(commit)
        raise

    # Bundle every new via into one named group so they delete as a set, while
    # KiCad's native "Remove from Group" still lets you peel out one via.
    new_vias = [v for v in board.get_vias() if v.id.value not in before_ids]
    _group_vias(kicad, board, new_vias, net_name)

    try:
        board.refill_zones()
    except KiCadConnectionError:
        pass

    return len(new_vias) if new_vias else len(vias)


class ViaStitchingDialog(wx.Dialog):
    """The 'Via Stitching Parameters' input dialog."""

    def __init__(self, parent, net_names):
        super().__init__(parent, title="Via Stitching Parameters")

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

        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(grid, 1, wx.EXPAND | wx.ALL, 12)
        outer.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizerAndFit(outer)

    def values(self):
        return {
            "via_dia_mm": float(self.via_dia.GetValue()),
            "drill_mm": float(self.drill.GetValue()),
            "spacing_mm": float(self.spacing.GetValue()),
            "net_name": self.net.GetValue().strip(),
            "pattern": PATTERNS[self.pattern.GetSelection()],
        }


def main():
    # Capture the focused window (the PCB editor) before we create any window.
    foreground_hwnd = get_foreground_hwnd()

    app = wx.App()
    prepare_app()

    try:
        kicad = KiCad()
        board = kicad.get_board()
    except KiCadConnectionError:
        wx.MessageBox(
            "Could not connect to KiCad.\n\n"
            "Enable the API server in Preferences > Plugins, and make sure a "
            "board is open in the PCB editor.",
            "Via Stitching",
            wx.OK | wx.ICON_ERROR,
        )
        return

    net_names = sorted({n.name for n in board.get_nets() if n.name})

    # The plugin runs as its own process. Give the dialog editor-attached window
    # behavior: a KiCad-owned tool window on Windows, and a floating window that
    # joins the PCB editor's Stage Manager set on macOS.
    dlg = ViaStitchingDialog(None, net_names)
    attach_to_kicad(dlg, foreground_hwnd, board)
    attach_to_stage_manager(dlg)
    dlg.CentreOnScreen()

    # Keep the dialog alive (hidden) until the end so every message box can be
    # parented to it and inherit the same owned-window behaviour.
    def msg(text, style):
        wx.MessageBox(text, "Via Stitching", style, dlg)

    try:
        if dlg.ShowModal() != wx.ID_OK:
            return
        try:
            params = dlg.values()
        except ValueError:
            msg("Via diameter, drill and spacing must be numbers (mm).",
                wx.OK | wx.ICON_ERROR)
            return

        dlg.Hide()

        busy = wx.BusyCursor()
        try:
            count = stitch(kicad, board, parent=dlg, **params)
        except Exception as exc:  # surface any failure to the user
            del busy
            msg(str(exc), wx.OK | wx.ICON_ERROR)
            return
        del busy

        if count:
            msg(f"Placed {count} stitching vias on net '{params['net_name']}'.",
                wx.OK | wx.ICON_INFORMATION)
    finally:
        dlg.Destroy()


if __name__ == "__main__":
    main()
