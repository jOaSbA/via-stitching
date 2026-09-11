# Board fixtures whose pcbnew API changed across KiCad 6 to 9.
#
# The plugin itself does not build footprints or layer sets, so these renames
# only ever broke the tests: the suites would not start on KiCad 8 or 9 even
# though the plugin works there, which left the two newest supported versions
# with no regression coverage at all.

import pcbnew


def fp_shape(footprint):
    """A graphic item on a footprint. KiCad 8 merged FP_SHAPE into PCB_SHAPE."""
    ctor = getattr(pcbnew, "FP_SHAPE", None) or pcbnew.PCB_SHAPE
    return ctor(footprint)


def rectangle():
    """The rectangle value of the shape enum, renamed after KiCad 6."""
    for name in ("S_RECT", "SHAPE_T_RECTANGLE", "SHAPE_T_RECT"):
        value = getattr(pcbnew, name, None)
        if value is not None:
            return value
    raise AttributeError("this pcbnew has no rectangle shape enum")


def layer_set(*layers):
    """An LSET holding these layers.

    LSET(layer) builds one on KiCad 6 to 8 and raises on 9+, which wants a
    vector of PCB_LAYER_ID that a Python list of ints does not satisfy either.
    addLayer() is the one route that works on every version checked."""
    result = pcbnew.LSET()
    for layer in layers:
        result.addLayer(layer)
    return result
