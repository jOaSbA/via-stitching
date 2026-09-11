# Spike: pad copper-size-on-a-given-layer lookup, behind _pad_copper_keepout_shapes.
#
# Three real shapes turned up here, not two:
#  - kipy: pad.padstack.copper_layers -> list of PadStackLayer(layer, size),
#    one entry per copper layer the pad has its own shape on (PST_CUSTOM etc).
#  - pcbnew KiCad 9/10 (SWIG): confirmed via docs.kicad.org/doxygen-python-10.0
#    that PCB_VIA (and, per the PAD 9.0 docs, PAD too) now expose Padstack(),
#    the same unified per-layer model kipy uses. KiCad only unified the two
#    APIs onto one underlying padstack recently -- this did NOT always exist.
#  - pcbnew KiCad <=8 (SWIG, pre-unification): PAD only has GetSize() (one
#    size for the whole pad) and GetLayerSet() (which layers it's on) -- no
#    per-layer copper shape at all, because per-layer pad shapes are
#    themselves a KiCad 9+ feature. A legacy board can never actually have a
#    pad whose copper differs by layer, so answering with the one GetSize()
#    is not an approximation on that board -- there's nothing it could be
#    hiding.
#
# So "support old KiCad" doesn't mean one SWIG backend, it means picking
# between the modern-padstack shape and the legacy flat shape too.

def ipc_pad_copper_size(pad, span):
    layers = pad.padstack.copper_layers
    return next((l.size for l in layers if l.layer in span), layers[0].size)


def swig_modern_pad_copper_size(pad, span):
    layers = pad.Padstack().copper_layers
    return next((l.size for l in layers if l.layer in span), layers[0].size)


def swig_legacy_pad_copper_size(pad, span):
    return pad.GetSize()  # one size, whichever spanned layer asked


# ---- fakes for the three shapes ----

class _Size:
    def __init__(self, x, y):
        self.x, self.y = x, y
    def __eq__(self, other):
        return (self.x, self.y) == (other.x, other.y)

class _PadStackLayer:
    def __init__(self, layer, size):
        self.layer, self.size = layer, size

class _FakePadstack:
    def __init__(self, per_layer):
        self.copper_layers = [_PadStackLayer(l, s) for l, s in per_layer]

class _FakeIpcPad:
    def __init__(self, per_layer):
        self.padstack = _FakePadstack(per_layer)

class _FakeModernSwigPad:
    def __init__(self, per_layer):
        self._padstack = _FakePadstack(per_layer)
    def Padstack(self):
        return self._padstack

class _FakeLegacySwigPad:
    def __init__(self, size):
        self._size = size
    def GetSize(self):
        return self._size


F_CU, IN1_CU = "F.Cu", "In1.Cu"

def demo():
    per_layer = [(F_CU, _Size(500, 500)), (IN1_CU, _Size(300, 300))]

    # Modern shapes: both backends resolve to the copper actually on In1.Cu,
    # not the front-layer default -- the real bug _pad_copper_keepout_shapes'
    # comment warns about ("index 0 is F.Cu, the wrong ring for a blind via").
    assert ipc_pad_copper_size(_FakeIpcPad(per_layer), {IN1_CU}) == _Size(300, 300)
    assert swig_modern_pad_copper_size(_FakeModernSwigPad(per_layer), {IN1_CU}) == _Size(300, 300)

    # Legacy shape: one size for the whole pad regardless of which spanned
    # layer is asked about -- correct for this shape, not a shortcut, because
    # a pre-9 board has no way to have a pad whose copper differs by layer.
    legacy = _FakeLegacySwigPad(_Size(400, 400))
    assert swig_legacy_pad_copper_size(legacy, {F_CU}) == _Size(400, 400)
    assert swig_legacy_pad_copper_size(legacy, {IN1_CU}) == _Size(400, 400)

    print("pad copper size: modern kipy/pcbnew agree per-layer; legacy pcbnew is flat by construction")


if __name__ == "__main__":
    demo()
