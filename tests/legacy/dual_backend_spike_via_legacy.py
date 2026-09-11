# Spike: via creation on pre-9 (legacy) SWIG, no unified Padstack() -- the
# actual audience from the "leave IPC-reachable KiCad 9/10 alone, target the
# distro-locked 6/7/8 crowd" call. Confirmed via docs.kicad.org/doxygen-python-6.0
# that PCB_VIA has SetViaType/SetLayerPair/SetWidth/SetDrill/SetNetCode --
# imperative setters on a plain object, not a padstack to build once and copy.
#
# That kills the perf trick the real code leans on: _via_template() in
# via_stitching_action.py exists because kipy's Via.type setter rebuilds a
# 32-entry protobuf layer dict every assignment (measured: 95% of a 2401-via
# run). Legacy SWIG has no padstack and no protobuf, so there is nothing
# to memoize -- a stitching run there is a plain loop of setter calls, no
# lru_cache, no "must copy the proto, never mutate it" contract to honor.

VIA_THROUGH = "through"


def ipc_make_via(via_type, start_layer, end_layer, diameter_nm, drill_nm, net, x, y):
    """Mirrors the real code: build once via a cached template, copy per via."""
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def template(via_type, start_layer, end_layer, diameter_nm, drill_nm):
        return {
            "type": via_type, "start": start_layer, "end": end_layer,
            "diameter": diameter_nm, "drill": drill_nm,
        }

    via = dict(template(via_type, start_layer, end_layer, diameter_nm, drill_nm))
    via["position"] = (x, y)
    via["net"] = net
    return via


def swig_legacy_make_via(pcbnew_module, board, via_type, start_layer, end_layer, diameter_nm, drill_nm, net, x, y):
    """No padstack, no template to copy -- just the setters, once per via.

    PCB_VIA is a module-level constructor taking the board, not a factory
    method on the board itself (board.PCB_VIA() doesn't exist), and
    SetPosition needs an actual wxPoint, not a bare (x, y) tuple -- both
    caught by running this against a real KiCad 6.0 board; the fake below
    originally matched both wrong assumptions and passed anyway.

    Setter ORDER matters and is silently wrong the other way: SetViaType
    must come before SetLayerPair. Confirmed on a real board -- calling
    SetLayerPair(F_Cu, In1_Cu) then SetViaType(VIATYPE_BLIND_BURIED) resets
    TopLayer/BottomLayer back to a full F_Cu-B_Cu through-via span, silently
    discarding the custom span with no error. This order (type first) is
    the only one that keeps a blind/buried/micro via's actual requested
    span."""
    via = pcbnew_module.PCB_VIA(board)
    via.SetPosition(pcbnew_module.wxPoint(x, y))
    via.SetViaType(via_type)
    via.SetLayerPair(start_layer, end_layer)
    via.SetWidth(diameter_nm)
    via.SetDrill(drill_nm)
    via.SetNetCode(net)
    return via


# ---- fake standing in for the real pcbnew module surface ----

class _FakeLegacyVia:
    def __init__(self):
        self.position = self.via_type = self.layers = None
        self.width = self.drill = self.net = None
    def SetPosition(self, pos): self.position = pos
    def SetViaType(self, t): self.via_type = t
    def SetLayerPair(self, s, e): self.layers = (s, e)
    def SetWidth(self, w): self.width = w
    def SetDrill(self, d): self.drill = d
    def SetNetCode(self, n): self.net = n

class _FakePcbnewModule:
    def PCB_VIA(self, board):
        return _FakeLegacyVia()
    def wxPoint(self, x, y):
        return (x, y)


def demo():
    F_CU, B_CU = 0, 31

    ipc_via = ipc_make_via(VIA_THROUGH, F_CU, B_CU, 600_000, 300_000, "GND", 1_000_000, 2_000_000)
    assert ipc_via == {
        "type": VIA_THROUGH, "start": F_CU, "end": B_CU,
        "diameter": 600_000, "drill": 300_000,
        "position": (1_000_000, 2_000_000), "net": "GND",
    }

    swig_via = swig_legacy_make_via(
        _FakePcbnewModule(), object(), VIA_THROUGH, F_CU, B_CU, 600_000, 300_000, "GND", 1_000_000, 2_000_000
    )
    assert swig_via.position == (1_000_000, 2_000_000)
    assert swig_via.via_type == VIA_THROUGH
    assert swig_via.layers == (F_CU, B_CU)
    assert swig_via.width == 600_000
    assert swig_via.drill == 300_000
    assert swig_via.net == "GND"

    print("via creation: same resulting via, but no template/cache makes sense on legacy SWIG")


if __name__ == "__main__":
    demo()
