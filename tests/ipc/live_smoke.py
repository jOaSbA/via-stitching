# Drives the IPC plugin against a *running* KiCad, which is the only way to
# find out whether it works on a given KiCad version: everything else in
# tests/ipc/ runs offline against a fake board.
#
# Needs a KiCad with the API server enabled and a board open, then:
#
#   python3 tests/ipc/live_smoke.py
#
# Prints one line per step and exits non-zero on the first failure.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugins"))

from kipy import KiCad  # noqa: E402
from kipy.board_types import BoardLayer, ViaType  # noqa: E402


def main():
    kicad = KiCad()
    print("kicad      " + str(kicad.get_version()))
    print("kipy built against " + str(kicad.get_api_version()))
    try:
        kicad.check_version()
        print("handshake  ok")
    except Exception as exc:
        # Only raised when KiCad is newer than the library, so an older KiCad
        # gets here silently. That is exactly the case worth reporting.
        print("handshake  {}: {}".format(type(exc).__name__, exc))

    board = kicad.get_board()
    nets = sorted({n.name for n in board.get_nets() if n.name})
    print("board      open, nets: " + ", ".join(nets))

    zones = board.get_zones()
    poured = [z for z in zones if not z.is_rule_area() and z.filled_polygons]
    print("zones      {} total, {} with fill".format(len(zones), len(poured)))

    import via_stitching_action as vs

    print("plugin     imported")

    before = len(board.get_vias())
    placed, grouped = vs.stitch(
        board, ViaType.VT_THROUGH, BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, "GND",
        0.6, 0.3, 2.0, "Square", 0.0, 0.0,
    )
    after = len(board.get_vias())
    print("stitch     placed {}, grouped={}, board went from {} to {} vias".format(
        placed, grouped, before, after))
    assert placed > 0, "nothing was placed"
    assert after == before + placed, (before, after, placed)

    print("RESULT     the IPC build works against this KiCad")


if __name__ == "__main__":
    main()
