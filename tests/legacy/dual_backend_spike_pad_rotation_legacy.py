# Spike: rotated pad-copper keepout box, behind _pad_copper_keepout_shapes,
# on legacy SWIG.
#
# kipy's _pad_angle_degrees has to try two shapes (an Angle object with
# .degrees, or a plain number) because the padstack rotation's exact type
# has varied across kicad-python releases. Legacy SWIG has no such
# ambiguity: PAD.GetOrientationDegrees() is a plain float, confirmed on a
# real KiCad 6.0 PAD -- one less try/except branch than the IPC side needs.
#
# Sign convention carries over unchanged: KiCad angles run counter-clockwise
# on screen while the board y axis points down, so a KiCad +angle is a
# negative rotation in raw (shapely) coordinates, same as the IPC side's
# affinity.rotate(rect, -angle_deg, ...) call. Not re-derived here -- carried
# forward from the real code's own calibration note, since the sign
# convention is a property of the coordinate system, not the API generation.

def swig_pad_copper_keepout_shapes(pcbnew_module, board, net_name, via_radius_nm,
                                    clearance_lookup, span, avoid_same_net_pads=True):
    """Clearance areas around pad copper (legacy: one flat size per pad,
    since per-layer padstack copper is a KiCad 9+-only feature -- confirmed
    PAD has no Padstack() on a real KiCad 6.0 board)."""
    from shapely.geometry import box as shapely_box
    from shapely import affinity

    span_set = set(span)
    shapes = []
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            same_net = pad.GetNetname() == net_name
            if same_net and not avoid_same_net_pads:
                continue
            if pad.GetAttribute() not in (pcbnew_module.PAD_ATTRIB_PTH, pcbnew_module.PAD_ATTRIB_NPTH):
                layerset = pad.GetLayerSet()
                if not any(layerset.Contains(l) for l in span_set):
                    continue
            size = pad.GetSize()
            if size.x <= 0 or size.y <= 0:
                continue
            margin = via_radius_nm + (0 if same_net else clearance_lookup(pad.GetNetname()))
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


def demo():
    from shapely.geometry import box as shapely_box
    from shapely import affinity

    # Smoke-test the rotation math alone: a 2x1 rect rotated 90 degrees
    # should have its bounding box swap aspect (long axis flips from x to y).
    rect = shapely_box(-1000, -500, 1000, 500)
    rotated = affinity.rotate(rect, -90, origin="center")
    minx, miny, maxx, maxy = rotated.bounds
    assert abs((maxx - minx) - 1000) < 1  # was 2000 wide, now ~1000 (the old height)
    assert abs((maxy - miny) - 2000) < 1  # was 1000 tall, now ~2000 (the old width)
    print("pad rotation math: 90-degree rotation correctly swaps the bounding aspect")


if __name__ == "__main__":
    demo()
