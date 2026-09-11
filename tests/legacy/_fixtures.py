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
    """An LSET holding these layers. KiCad 9 stopped taking a bare
    PCB_LAYER_ID here and wants a sequence instead."""
    try:
        return pcbnew.LSET(*layers)
    except TypeError:
        return pcbnew.LSET(list(layers))
