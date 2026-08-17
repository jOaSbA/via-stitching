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

import wx

from kipy import KiCad
from kipy.errors import ConnectionError as KiCadConnectionError
from kipy.board_types import Group, Via, ArcTrack, BoardLayer, PadType, PSS_CIRCLE
from kipy.geometry import Vector2
from kipy.util import from_mm

# Make the sibling helper modules importable regardless of how KiCad launches us.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mac_dialog import attach_to_stage_manager, prepare_app  # noqa: E402
from _win_dialog import make_tool_window  # noqa: E402

VERSION = "1.0.2"

# shapely (plus the numpy and GEOS it drags in) costs about 1.6 s to import, which
# is most of the plugin's start-up time, so the geometry functions import it lazily.

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
DEFAULT_AVOID_OTHER_ZONES = True

# Safety margins applied silently (the dialog has no clearance field, by design).
# A via placed at least (via_radius + EDGE_EPS) inside the zone fill stays clear of
# other-net copper, because KiCad already pulled the fill back by the board
# clearance when filling. EDGE_EPS just absorbs polygon rounding.
EDGE_EPS_MM = 0.05
# Hole-to-hole margin kept between a new via drill and existing via/pad drills.
HOLE_MARGIN_MM = 0.25
# Clearance kept between a new via's copper and existing tracks of other nets.
# The IPC API has no call for the board's real clearance rules, so this is a
# fixed stand-in the same way HOLE_MARGIN_MM is, rather than the true value.
TRACK_MARGIN_MM = 0.2

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


def _keepout_region(board, new_via_radius_nm):
    """Union of drill keepouts around existing vias and through-hole pads.

    Prevents new stitching vias from violating hole-to-hole clearance.
    """
    from shapely.geometry import Point
    from shapely.ops import unary_union

    margin = from_mm(HOLE_MARGIN_MM)
    circles = []

    for via in board.get_vias():
        r = new_via_radius_nm + via.drill_diameter // 2 + margin
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


def _track_keepout_region(board, via_radius_nm, net_name):
    """Union of clearance areas around other-nets' tracks, on any copper layer.

    A through via's drill spans every copper layer of the board, not just the
    ones `net_name` happens to be poured on, so a track on a layer the pour
    never touches (an outer signal layer over an inner GND plane, say) still
    has to be avoided or the via drills straight through it.
    """
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    margin = from_mm(TRACK_MARGIN_MM)
    lines = []
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
        r = via_radius_nm + track.width // 2 + margin
        lines.append(LineString(coords).buffer(r, quad_segs=8))

    return unary_union(lines) if lines else None


def _zone_keepout_region(zones, net_name, via_radius_nm):
    """Union of clearance areas around other-nets' filled zones, on any layer.

    A through via's drill spans every copper layer, so a filled zone for a
    different net on a layer `net_name` never pours on (an inner power plane
    under a GND-poured outer layer, say) still has to be avoided or the via
    shorts into it.
    """
    from shapely.ops import unary_union

    margin = from_mm(TRACK_MARGIN_MM)
    polys = []
    for zone in zones:
        net = zone.net
        if net is not None and net.name == net_name:
            continue
        for shapes in zone.filled_polygons.values():
            for pwh in shapes:
                poly = _polygon_with_holes_to_shapely(pwh)
                if poly is not None:
                    polys.append(poly.buffer(via_radius_nm + margin, quad_segs=8))

    return unary_union(polys) if polys else None


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
    net = next((n for n in board.get_nets() if n.name == net_name), None)
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

    # Avoid existing via/pad drill holes and other-net tracks on any layer.
    keepout = _keepout_region(board, via_radius_nm)
    track_keepout = _track_keepout_region(board, via_radius_nm, net_name)
    if track_keepout is not None:
        keepout = track_keepout if keepout is None else keepout.union(track_keepout)
    if avoid_other_zones:
        zone_keepout = _zone_keepout_region(zones, net_name, via_radius_nm)
        if zone_keepout is not None:
            keepout = zone_keepout if keepout is None else keepout.union(zone_keepout)
    if keepout is None:
        points = candidates
    else:
        blocked = prep(keepout)
        points = [(x, y) for (x, y) in candidates if not blocked.intersects(Point(x, y))]
    if not points:
        raise RuntimeError(
            f"All {len(candidates)} candidate positions are blocked by existing "
            "vias, pads, tracks, or zones of other nets.\n"
            "If these zones are already stitched, delete the previous vias first. "
            "Otherwise try a smaller spacing, or untick 'Avoid other nets' zones' "
            "if that option is what's blocking them."
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

        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(grid, 1, wx.EXPAND | wx.ALL, 12)
        outer.Add(self.avoid_zones, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
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
        # The server answered, so it is running. Something else went wrong, most
        # often no board open in the PCB editor.
        _report(
            None,
            "KiCad's API server answered but did not hand over a board.\n\n"
            "Open a board in the PCB editor and run this again.",
            exc,
        )
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
            _report(dlg, "Via Stitching failed while placing vias.", exc)
            return
        del busy

        if count:
            text = f"Placed {count} stitching vias on net '{params['net_name']}'."
            if not grouped:
                text += (
                    "\n\nThis KiCad version did not group them, so they delete "
                    "individually rather than as a set."
                )
            _msg(dlg, text, wx.OK | wx.ICON_INFORMATION)
    finally:
        dlg.Destroy()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # nothing may exit silently
        _report(None, "Via Stitching hit an unexpected error.", exc)
