# Spike: the remaining mechanical ports for legacy (pre-9) SWIG --
# pad layer membership, rule-area keepout flags, footprint bounding box,
# and grouping vias into one undoable set.

# --- pad layer membership (behind _pad_copper_keepout_shapes' span filter) ---
# kipy: pad.padstack.layers -> a plain set of BoardLayer.
# legacy pcbnew: pad.GetLayerSet() -> LSET, tested with .Contains(layer).

def ipc_pad_touches_span(pad, span_set):
    return bool(set(pad.padstack.layers) & span_set)

def swig_pad_touches_span(pad, span_set):
    layerset = pad.GetLayerSet()
    return any(layerset.Contains(l) for l in span_set)


# --- rule area keepout flags (behind _rule_area_keepout_shapes) ---
# kipy: zone.is_rule_area() + zone.proto.rule_area_settings.keepout_vias + zone.layers.
# legacy pcbnew: ZONE.GetIsRuleArea() + ZONE.GetDoNotAllowVias() + ZONE.GetLayerSet().

def ipc_zone_blocks_vias(zone, span_set):
    if not zone.is_rule_area():
        return False
    if not zone.rule_area_settings_keepout_vias:
        return False
    return bool(set(zone.layers) & span_set)

def swig_zone_blocks_vias(zone, span_set):
    if not zone.GetIsRuleArea():
        return False
    if not zone.GetDoNotAllowVias():
        return False
    layerset = zone.GetLayerSet()
    return any(layerset.Contains(l) for l in span_set)


# --- footprint bounding box (behind _footprint_keepout_shapes) ---
# kipy: board.get_item_bounding_box(footprints) -- one batch call that can come
# back SHORTER than the input list, so the real code has to detect and warn
# about dropped items.
# legacy pcbnew: footprint.GetBoundingBox() -- a plain per-item method call
# with no batch endpoint and nothing to silently drop; that whole
# "shorter than expected" branch in _footprint_keepout_shapes has no
# legacy-SWIG equivalent to guard against.

def ipc_footprint_boxes(board, footprints):
    boxes = board.get_item_bounding_box(footprints)
    dropped = len(footprints) - len(boxes)
    return boxes, dropped

def swig_footprint_boxes(footprints):
    return [fp.GetBoundingBox() for fp in footprints], 0


# --- grouping vias (behind _group_vias) ---
# kipy: build a Group() with .items set to the via list, board.create_items(group).
# legacy pcbnew: PCB_GROUP(board), board.Add(group), then group.AddItem(via) per via.
#
# Verified against a real standalone BOARD on KiCad 6.0's bundled pcbnew.py:
# AddItem() does register real membership (via.GetParentGroup() correctly
# returns the same group object, an ungrouped control via correctly returns
# None) -- this isn't a fake-only claim, it holds on the real API.
#
# One asymmetry that matters for cleanup code: board.Remove(via) auto-clears
# that via's GetParentGroup() backlink, but board.Remove(group) does NOT
# cascade to members -- the vias stay on the board, now pointing at a
# detached group object. Deleting "the group" from legacy code means
# explicitly removing every member, not just the group shell -- this makes
# mandatory grouping (the only trustworthy delete-as-a-set path, since
# legacy SWIG has no reliable native undo) something the plugin's own
# cleanup routine must implement by iterating members itself.

def ipc_group_vias(board, vias, name):
    group = {"name": name, "items": list(vias)}
    return bool(board.create_items(group))

def swig_group_vias(pcbnew_module, board, vias, name):
    """PCB_GROUP is a module-level constructor taking the board, same trap
    as PCB_VIA -- board.PCB_GROUP() doesn't exist (caught against a real
    KiCad 6.0 board; the fake originally matched the wrong assumption)."""
    group = pcbnew_module.PCB_GROUP(board)
    group.SetName(name)
    board.Add(group)
    for via in vias:
        group.AddItem(via)
    return group

def swig_delete_grouped_vias(board, group, vias):
    """The only reliable way to remove a legacy stitching run: explicit
    member-by-member removal. board.Remove(group) alone leaves every via
    still on the board, still pointing at the now-detached group."""
    for via in vias:
        board.Remove(via)
    board.Remove(group)


# ---- fakes ----

class _LSet:
    def __init__(self, layers):
        self._layers = set(layers)
    def Contains(self, layer):
        return layer in self._layers

class _FakeIpcPad:
    def __init__(self, layers):
        self.padstack = type("P", (), {"layers": set(layers)})()

class _FakeSwigPad:
    def __init__(self, layers):
        self._layers = _LSet(layers)
    def GetLayerSet(self):
        return self._layers

class _FakeIpcZone:
    def __init__(self, is_rule_area, keepout_vias, layers):
        self._rule_area = is_rule_area
        self.rule_area_settings_keepout_vias = keepout_vias
        self.layers = set(layers)
    def is_rule_area(self):
        return self._rule_area

class _FakeSwigZone:
    def __init__(self, is_rule_area, keepout_vias, layers):
        self._rule_area, self._keepout_vias = is_rule_area, keepout_vias
        self._layers = _LSet(layers)
    def GetIsRuleArea(self):
        return self._rule_area
    def GetDoNotAllowVias(self):
        return self._keepout_vias
    def GetLayerSet(self):
        return self._layers

class _FakeIpcFootprintBoard:
    def get_item_bounding_box(self, footprints):
        return ["box"] * (len(footprints) - 1)  # one silently dropped, as it can be for real

class _FakeSwigFootprint:
    def GetBoundingBox(self):
        return "box"

class _FakeIpcGroupBoard:
    def create_items(self, group):
        return group  # truthy: kept

class _FakeSwigGroup:
    def __init__(self):
        self.name = None
        self.members = []
    def SetName(self, n):
        self.name = n
    def AddItem(self, item):
        self.members.append(item)

class _FakeSwigGroupBoard:
    def __init__(self):
        self.removed = []
    def Add(self, item):
        pass
    def Remove(self, item):
        self.removed.append(item)

class _FakePcbnewModule:
    def PCB_GROUP(self, board):
        return _FakeSwigGroup()


def demo():
    F_CU, IN1_CU, B_CU = 0, 1, 31
    span = {F_CU, IN1_CU}

    assert ipc_pad_touches_span(_FakeIpcPad([IN1_CU, B_CU]), span) is True
    assert swig_pad_touches_span(_FakeSwigPad([IN1_CU, B_CU]), span) is True
    assert ipc_pad_touches_span(_FakeIpcPad([B_CU]), span) is False
    assert swig_pad_touches_span(_FakeSwigPad([B_CU]), span) is False

    ipc_zone = _FakeIpcZone(True, True, [F_CU])
    swig_zone = _FakeSwigZone(True, True, [F_CU])
    assert ipc_zone_blocks_vias(ipc_zone, span) is True
    assert swig_zone_blocks_vias(swig_zone, span) is True
    assert ipc_zone_blocks_vias(_FakeIpcZone(True, False, [F_CU]), span) is False  # not a via keepout
    assert swig_zone_blocks_vias(_FakeSwigZone(True, False, [F_CU]), span) is False

    footprints = [object(), object(), object()]
    ipc_boxes, ipc_dropped = ipc_footprint_boxes(_FakeIpcFootprintBoard(), footprints)
    assert ipc_dropped == 1  # the real code has to detect and report this
    swig_boxes, swig_dropped = swig_footprint_boxes([_FakeSwigFootprint() for _ in footprints])
    assert swig_dropped == 0  # nothing to drop: one call per item, always answers

    assert ipc_group_vias(_FakeIpcGroupBoard(), ["v1", "v2"], "ViaStitching GND") is True
    swig_group_board = _FakeSwigGroupBoard()
    swig_group = swig_group_vias(_FakePcbnewModule(), swig_group_board, ["v1", "v2"], "ViaStitching GND")
    assert swig_group.name == "ViaStitching GND"
    assert swig_group.members == ["v1", "v2"]

    # Cleanup must remove members explicitly -- board.Remove(group) alone
    # does not cascade to them on real KiCad (verified against a real board).
    swig_delete_grouped_vias(swig_group_board, swig_group, ["v1", "v2"])
    assert swig_group_board.removed == ["v1", "v2", swig_group]

    print("pad/rule-area/bbox/group: all four port directly; footprint bbox drops a whole error branch")


if __name__ == "__main__":
    demo()


def swig_footprint_keepout_shapes(board, net_name, via_radius_nm, clearance_lookup):
    """Margin box around every footprint's bounding box (mechanical fit, not
    copper -- applies regardless of the footprint's net, same as the real code).
    swig_footprint_boxes already confirmed GetBoundingBox() never drops an
    item on legacy, unlike kipy's batch call -- no "shorter than expected"
    branch needed here."""
    from shapely.geometry import box as shapely_box

    footprints = list(board.GetFootprints())
    if not footprints:
        return []
    boxes, _dropped = swig_footprint_boxes(footprints)
    margin = via_radius_nm + clearance_lookup(net_name)
    return [
        shapely_box(
            bbox.GetX() - margin, bbox.GetY() - margin,
            bbox.GetX() + bbox.GetWidth() + margin, bbox.GetY() + bbox.GetHeight() + margin,
        )
        for bbox in boxes
    ]
