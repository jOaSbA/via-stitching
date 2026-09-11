# TEMPORARY, delete with .github/workflows/legacy-verify.yml.
#
# Prints what the two APIs the legacy backend depends on actually look like on
# whatever KiCad this runs against: the point/size constructors, and the
# netclass containers. CI runs this on KiCad 6 and 7 so the compatibility fix
# is written from what those versions really expose, not from guesses.

import pcbnew

print("build:", pcbnew.GetBuildVersion())
print("has wxPoint:", hasattr(pcbnew, "wxPoint"), "| has VECTOR2I:", hasattr(pcbnew, "VECTOR2I"))

board = pcbnew.BOARD()
board.SetCopperLayerCount(2)
net = pcbnew.NETINFO_ITEM(board, "GND")
board.Add(net)


def members(obj, limit=40):
    return [m for m in dir(obj) if not m.startswith("_")][:limit]


# --- what the setters accept -------------------------------------------------
for ctor_name in ("wxPoint", "VECTOR2I"):
    ctor = getattr(pcbnew, ctor_name, None)
    if ctor is None:
        print(f"setters with {ctor_name}: not present")
        continue
    via = pcbnew.PCB_VIA(board)
    try:
        via.SetPosition(ctor(1000, 2000))
        print(f"setters with {ctor_name}: SetPosition OK")
    except Exception as exc:
        print(f"setters with {ctor_name}: SetPosition {type(exc).__name__}: {exc}")

# --- netclasses --------------------------------------------------------------
try:
    ncs = board.GetNetClasses()
    print("GetNetClasses ->", type(ncs).__name__, members(ncs))
except Exception as exc:
    print("GetNetClasses FAILED:", type(exc).__name__, exc)

ds = board.GetDesignSettings()
print("design settings, netclass-ish:", [m for m in members(ds, 200) if "lass" in m or "NetSettings" in m])

ns = getattr(ds, "m_NetSettings", None)
if ns is not None:
    print("m_NetSettings ->", type(ns).__name__, members(ns))
    inner = getattr(ns, "m_NetClasses", None)
    if inner is not None:
        print("  m_NetSettings.m_NetClasses ->", type(inner).__name__, members(inner))
    default = getattr(ns, "m_DefaultNetClass", None)
    if default is not None:
        print("  m_DefaultNetClass ->", type(default).__name__,
              "clearance:", getattr(default, "GetClearance", lambda: "n/a")())

print("board, netclass-ish:", [m for m in members(board, 300) if "lass" in m])
print("net.GetNetClassName():", net.GetNetClassName())
for name in ("GetNetClassSlow", "GetEffectiveNetClass", "GetNetClass"):
    fn = getattr(net, name, None)
    if fn is None:
        continue
    try:
        nc = fn()
        print(f"net.{name}() ->", type(nc).__name__, members(nc, 15))
    except Exception as exc:
        print(f"net.{name}() FAILED:", type(exc).__name__, exc)
