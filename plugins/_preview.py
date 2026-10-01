# Live preview of a stitching plan, shared by both builds.
#
# Neither plugin API can draw temporary items on KiCad's own canvas, so the
# preview is a panel inside the parameters dialog: the copper being stitched,
# and every via plan() would place, at its real diameter. Vias nudged off a
# blocked grid position get their own color, which is the part a user can't
# predict from the dialog's numbers alone.
#
# It updates itself a moment after the settings stop changing, rather than on
# every keystroke: planning a large board takes seconds, and it has to run on
# the UI thread because SWIG pcbnew is not safe to call from any other.
#
# Plain wx and the plan's own shapely geometry, no board API calls, so the IPC
# and SWIG builds use this file unchanged. build.py ships it in both packages.
#
# License: GPL-3.0-or-later

import wx

ON_GRID_RGB = (230, 230, 230)
MOVED_RGB = (255, 150, 30)
BACKGROUND_RGB = (16, 22, 34)  # close to KiCad's default board background
ERROR_RGB = (180, 95, 0)  # the dialog's own advisory color
# Each layer's copper is see-through, so where the layers overlap (where vias
# can go) reads darker than copper on one layer alone.
LAYER_ALPHA = 90
MIN_VIA_PX = 1.5  # a via stays visible however far out the view is zoomed
ZOOM_STEP = 1.25
DEBOUNCE_MS = 400


def _polygons(geom):
    """The polygons of a shapely Polygon or MultiPolygon."""
    return list(getattr(geom, "geoms", [geom]))


def _bounds(plan):
    """Bounds of every layer's copper, not just the overlap: the preview
    shows each layer whole."""
    boxes = [copper.bounds for _layer, copper in plan.layers] or [plan.region.bounds]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


class PreviewCanvas(wx.Panel):
    """Fit-to-window drawing of a plan. Mouse wheel zooms about the cursor,
    left-drag pans, double-click fits again."""

    def __init__(self, parent):
        super().__init__(parent, size=(520, 440))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.plan = None
        self.on_grid = self.moved = []
        self.via_r = 0
        self.scale, self.ox, self.oy = 1.0, 0.0, 0.0
        self._drag = None

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda evt: (self._fit(), self.Refresh()))
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_up)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEFT_DCLICK, lambda evt: (self._fit(), self.Refresh()))

    def set_plan(self, plan, via_diameter_nm, styles):
        """Show `plan`. `styles` is {layer: (name, (r, g, b))} for its layers.
        The view only refits when the copper itself changed, so a zoomed-in
        look at one corner survives tweaking the spacing."""
        refit = self.plan is None or _bounds(plan) != _bounds(self.plan)
        self.plan = plan
        self.via_r = via_diameter_nm / 2
        self.styles = styles
        candidates = set(plan.candidates)
        self.on_grid = [p for p in plan.points if p in candidates]
        self.moved = [p for p in plan.points if p not in candidates]
        if refit:
            self._fit()
        self.Refresh()

    # ---- view transform: screen = world * scale + offset ------------------

    def _fit(self):
        if self.plan is None:
            return
        minx, miny, maxx, maxy = _bounds(self.plan)
        # Pad by a via, so the vias on the edge are drawn whole.
        minx, miny = minx - self.via_r, miny - self.via_r
        maxx, maxy = maxx + self.via_r, maxy + self.via_r
        w, h = self.GetClientSize()
        margin = 12
        self.scale = min(
            max(w - 2 * margin, 1) / max(maxx - minx, 1),
            max(h - 2 * margin, 1) / max(maxy - miny, 1),
        )
        self.ox = w / 2 - (minx + maxx) / 2 * self.scale
        self.oy = h / 2 - (miny + maxy) / 2 * self.scale

    def to_screen(self, x, y):
        return x * self.scale + self.ox, y * self.scale + self.oy

    def zoom_at(self, mx, my, factor):
        """Zoom by `factor`, keeping the world point under (mx, my) put."""
        self.ox = mx - (mx - self.ox) * factor
        self.oy = my - (my - self.oy) * factor
        self.scale *= factor
        self.Refresh()

    def _on_wheel(self, evt):
        pos = evt.GetPosition()
        self.zoom_at(pos.x, pos.y, ZOOM_STEP if evt.GetWheelRotation() > 0 else 1 / ZOOM_STEP)

    def _on_down(self, evt):
        self._drag = evt.GetPosition()
        self.CaptureMouse()

    def _on_up(self, evt):
        self._drag = None
        if self.HasCapture():
            self.ReleaseMouse()

    def _on_motion(self, evt):
        if self._drag is None or not evt.Dragging():
            return
        pos = evt.GetPosition()
        self.ox += pos.x - self._drag.x
        self.oy += pos.y - self._drag.y
        self._drag = pos
        self.Refresh()

    # ---- drawing ----------------------------------------------------------

    def _on_paint(self, evt):
        self.draw(wx.AutoBufferedPaintDC(self))

    def draw(self, dc):
        """Draw onto any DC: the window's own on paint, a bitmap in tests."""
        dc.SetBackground(wx.Brush(wx.Colour(*BACKGROUND_RGB)))
        dc.Clear()
        if self.plan is None:
            return
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return

        # Back to front, so the via's start side ends up on top, as KiCad
        # draws the active layer.
        for layer, copper in reversed(self.plan.layers):
            _name, rgb = self.styles.get(layer, ("", (128, 128, 128)))
            path = gc.CreatePath()
            for poly in _polygons(copper):
                for ring in [poly.exterior, *poly.interiors]:
                    coords = list(ring.coords)
                    path.MoveToPoint(*self.to_screen(*coords[0]))
                    for x, y in coords[1:]:
                        path.AddLineToPoint(*self.to_screen(x, y))
                    path.CloseSubpath()
            gc.SetBrush(wx.Brush(wx.Colour(*rgb, LAYER_ALPHA)))
            gc.SetPen(wx.Pen(wx.Colour(*rgb), 1))
            gc.DrawPath(path, wx.ODDEVEN_RULE)

        r = max(self.via_r * self.scale, MIN_VIA_PX)
        gc.SetPen(wx.TRANSPARENT_PEN)
        for points, rgb in ((self.on_grid, ON_GRID_RGB), (self.moved, MOVED_RGB)):
            if not points:
                continue
            vias = gc.CreatePath()
            for x, y in points:
                sx, sy = self.to_screen(x, y)
                vias.AddCircle(sx, sy, r)
            gc.SetBrush(wx.Brush(wx.Colour(*rgb)))
            gc.FillPath(vias)


class PreviewPanel(wx.Panel):
    """The preview side of the parameters dialog.

    `compute` is the dialog's own: it reads the controls and returns
    (plan, via diameter in nm, {layer: (name, (r, g, b))}), or raises ValueError or
    RuntimeError worded for the user. The dialog calls schedule() whenever a
    setting changes.
    """

    def __init__(self, parent, compute, _):
        super().__init__(parent)
        self._compute = compute
        self._ = _
        self._timer = None
        self.canvas = PreviewCanvas(self)

        self.summary = wx.StaticText(self, label=_("Working out the preview..."))
        legend = wx.BoxSizer(wx.HORIZONTAL)
        self._add_swatches(legend, ((ON_GRID_RGB, _("On grid")), (MOVED_RGB, _("Moved"))))
        # Which layers the copper on the canvas is, rebuilt with every plan.
        self.layer_legend = wx.BoxSizer(wx.HORIZONTAL)

        # Ellipsized, so a long translation gives way to the controls beside it.
        hint = wx.StaticText(self, label=_("Scroll to zoom, drag to pan, double-click to fit."),
                             style=wx.ST_ELLIPSIZE_END)
        hint.SetToolTip(hint.GetLabel())
        hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

        self.auto = wx.CheckBox(self, label=_("Update automatically"))
        self.auto.SetValue(True)
        self.auto.SetToolTip(_(
            "Redraw the preview a moment after any setting changes. Untick it "
            "on a large board, where each update can take a few seconds, and "
            "use Update preview instead."
        ))
        self.auto.Bind(wx.EVT_CHECKBOX, lambda evt: self.auto.GetValue() and self.schedule())
        self.update_btn = wx.Button(self, label=_("Update preview"))
        self.update_btn.Bind(wx.EVT_BUTTON, lambda evt: self.update())

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(self.summary, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        top.Add(legend, 0, wx.ALIGN_CENTER_VERTICAL)
        bottom = wx.BoxSizer(wx.HORIZONTAL)
        bottom.Add(hint, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        bottom.Add(self.auto, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        bottom.Add(self.update_btn, 0, wx.ALIGN_CENTER_VERTICAL)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(top, 0, wx.EXPAND | wx.BOTTOM, 5)
        sizer.Add(self.layer_legend, 0, wx.EXPAND | wx.BOTTOM, 5)
        sizer.Add(self.canvas, 1, wx.EXPAND)
        sizer.Add(bottom, 0, wx.EXPAND | wx.TOP, 5)
        self.SetSizer(sizer)

    def _add_swatches(self, sizer, entries):
        for rgb, label in entries:
            # Bordered: the on-grid grey all but vanishes on a light dialog.
            swatch = wx.Panel(self, size=(12, 12), style=wx.BORDER_SIMPLE)
            swatch.SetBackgroundColour(wx.Colour(*rgb))
            sizer.Add(swatch, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
            sizer.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

    def schedule(self):
        """Update once the settings have been left alone for DEBOUNCE_MS."""
        if not self.auto.GetValue():
            return
        if self._timer is not None and self._timer.IsRunning():
            self._timer.Restart(DEBOUNCE_MS)
        else:
            self._timer = wx.CallLater(DEBOUNCE_MS, self.update)

    def update(self):
        if not self:  # the dialog closed while the timer was pending
            return
        _ = self._
        busy = wx.BusyCursor()
        try:
            plan, via_diameter_nm, styles = self._compute()
        except (ValueError, RuntimeError) as exc:
            self._say(str(exc), error=True)
            return
        except Exception as exc:
            self._say(_("The preview failed: {error}").format(error=exc), error=True)
            return
        finally:
            del busy
        self.canvas.set_plan(plan, via_diameter_nm, styles)
        self.layer_legend.Clear(delete_windows=True)
        named = [styles.get(layer, (str(layer), (128, 128, 128))) for layer, _copper in plan.layers]
        self._add_swatches(self.layer_legend, [(rgb, name) for name, rgb in named])
        text = _("{count} vias").format(count=len(plan.points))
        if self.canvas.moved:
            text += ", " + _("{moved} moved off the grid to clear an obstacle").format(
                moved=len(self.canvas.moved))
        self._say(text)

    def _say(self, text, error=False):
        """Summary line. On an error the last good drawing stays up, so a
        half-typed number doesn't blank the view mid-edit."""
        self.summary.SetLabel(text)
        self.summary.SetForegroundColour(
            wx.Colour(*ERROR_RGB) if error
            else wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        )
        self.summary.Wrap(max(self.canvas.GetSize().width - 180, 200))
        self.Layout()
