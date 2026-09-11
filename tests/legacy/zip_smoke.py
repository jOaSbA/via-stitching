# Does the built PCM archive actually work on this KiCad?
#
# Installs the zip the way the Plugin and Content Manager does (its plugins/
# directory becomes a scripting plugin directory), imports the plugin from
# there rather than from the repo, and stitches a real board. Run it with the
# python.exe of whichever KiCad you want to check:
#
#   "C:/Program Files/KiCad/6.0/bin/python.exe" tests/legacy/zip_smoke.py dist/via-stitching-swig-1.2.0.zip
#
# Prints one line per check and exits non-zero on the first failure.

import glob
import os
import sys
import tempfile
import zipfile


def main(zip_path):
    workdir = tempfile.mkdtemp(prefix="via-stitching-pcm-")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(workdir)
    plugins = os.path.join(workdir, "plugins")
    if not os.path.isdir(plugins):
        raise SystemExit("the archive has no plugins/ directory: " + str(os.listdir(workdir)))
    sys.path.insert(0, plugins)

    import wx

    wx.DisableAsserts()  # ActionPlugin.register() can otherwise pop a modal assert

    import pcbnew

    version = pcbnew.GetBuildVersion()
    print("kicad      " + version)

    try:
        import shapely

        print("shapely    " + shapely.__version__)
    except ImportError:
        print("shapely    MISSING (the plugin cannot run without it)")
        raise

    import via_stitching_action_legacy as vsl

    print("import     ok, version " + vsl.VERSION)

    # The toolbar icon is shared with the IPC build and only lands next to the
    # plugin at package time, so a broken layout shows up here and nowhere else.
    icon = vsl._icon_path()
    assert icon.startswith(plugins) and os.path.exists(icon), icon
    print("icon       " + os.path.relpath(icon, workdir).replace(os.sep, "/"))

    import _geometry_legacy as geo

    mm = 1_000_000
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(gnd)
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        zone = pcbnew.ZONE(board)
        zone.SetNet(gnd)
        zone.SetLayer(layer)
        board.Add(zone)
        polyset = pcbnew.SHAPE_POLY_SET()
        chain = pcbnew.SHAPE_LINE_CHAIN()
        for (x, y) in [(0, 0), (10 * mm, 0), (10 * mm, 10 * mm), (0, 10 * mm)]:
            chain.Append(pcbnew.VECTOR2I(int(x), int(y)))
        chain.SetClosed(True)
        polyset.AddOutline(chain)
        zone.SetFilledPolysList(layer, polyset)
    print("board      built, 2 layers poured on GND")

    placed, grouped = vsl.stitch(
        board, pcbnew.VIATYPE_THROUGH, pcbnew.F_Cu, pcbnew.B_Cu, "GND",
        via_dia_mm=0.6, drill_mm=0.3, spacing_mm=2.0, pattern="Square",
        x_offset_mm=0, y_offset_mm=0,
    )
    vias = [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    assert placed > 0 and len(vias) == placed, (placed, len(vias))
    print("stitch     placed {} vias, grouped={}".format(placed, grouped))

    runs = vsl.stitching_runs(board)
    assert len(runs) == 1, runs
    group, grouped_vias = runs[0]
    geo.delete_grouped_vias(board, group, grouped_vias)
    left = [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    assert not left, left
    print("reset      removed all {} vias again".format(len(grouped_vias)))

    print("RESULT     the archive works on KiCad " + version)


if __name__ == "__main__":
    args = sys.argv[1:] or sorted(glob.glob("dist/via-stitching-swig-*.zip"))
    if not args:
        raise SystemExit("no archive given and none found in dist/")
    main(args[-1])
