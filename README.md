# Via Stitching

A KiCad action plugin that fills the overlap of a net's copper zones (the top
and bottom GND pours, for example) with a grid of stitching vias. Through,
micro, blind, and buried vias are all supported, spanning whichever two copper
layers you pick.

Works on KiCad 6, 7, 8, 9, 10, and later. One package, two builds: the Plugin
and Content Manager hands your KiCad the one it can run, and both have the same
dialog. See [Two builds](#two-builds) if you want to know which one you have.

![The Via Stitching parameters dialog](docs/dialog.png?v=3)

## Requirements

On KiCad 10 and later:

- The IPC API server enabled under *Preferences > Plugins*.
- `kicad-python`, `wxPython`, and `shapely`, which KiCad installs for you on
  first run from `requirements.txt`.

On KiCad 6 to 9:

- `shapely`. KiCad ships wxPython itself but not shapely, so install it into
  KiCad's own Python if the plugin reports it missing. On Windows that is
  `"C:/Program Files/KiCad/<version>/bin/python.exe" -m pip install shapely`.
  On Linux KiCad uses the system Python, so install it from your package
  manager: `sudo apt install python3-shapely` on Debian and Ubuntu. The
  Flatpak build of KiCad has not been tested.

## Install

### From the Plugin and Content Manager

*Tools > Plugin and Content Manager > Plugins*, find Via Stitching, install it,
and restart the PCB editor.

### Manually

1. From the [Releases](https://github.com/jOaSbA/via-stitching/releases) page,
   download `via-stitching-x.y.z.zip` on KiCad 10 or later, or
   `via-stitching-swig-x.y.z.zip` on KiCad 6 to 9. Both are attached to the
   same release.
2. In the PCB editor: *Tools > Plugin and Content Manager > Install from File...*
   and pick the zip. You can also unzip the `plugins/` contents into
   `Documents/KiCad/<version>/3rdparty/plugins/via_stitching/`.
3. On KiCad 10 and later, enable the IPC API server. Then
   *Tools > External Plugins > Refresh Plugins*.

## Usage

1. Open a board and fill the zones first (`B`) so the pours have copper to read.
2. Optionally select an existing via first. Its type, layers, net, diameter,
   and drill are used to pre-fill the dialog, so cloning an existing via's
   settings is a matter of selecting it and running the plugin.
3. Run Via Stitching and set:
   - Via Type: Through, Micro, Blind, Buried, or Blind/Buried. Start Layer and
     End Layer are free for every type except Through, which is always
     F.Cu/B.Cu. Picking a combination that isn't the standard shape for the
     chosen type (say a microvia that doesn't touch an outer layer) doesn't
     block you; a warning explaining why appears inline in the dialog, but
     there's nothing to click through.
   - Via Diameter (mm) and Drill (mm) for the via size.
   - Via Pattern: Hexagonal (densest), Square, or Staggered, and Spacing (mm)
     for the centre-to-centre grid pitch.
   - X-Offset / Y-Offset (mm), for shifting the grid on a second pass so it
     doesn't land on top of a first one. Useful when stitching more than one
     via pattern onto the same net and area, for example a separate
     front-side and back-side microvia pass.
   - Net Name, the net to stitch (defaults to `GND`).
   - Avoid zones of other nets, off by default. See below.
   - Avoid footprints, off by default. Keeps vias out from under component
     bodies, for mechanical fit rather than clearance. Leave it off if you want
     thermal-via arrays under a QFN or BGA ground pad, which is a normal use of
     via stitching.
   - Avoid pads already on this net, off by default. See below.
4. Click OK. Vias go only where the net is poured on both layers the via
   connects (and on any layer in between, where the net is also poured there),
   inset far enough to stay DRC-clean, and the zones are refilled for you. A
   grid position blocked by a pad or a track is moved a short way to the
   nearest clear spot rather than skipped, so the grid keeps its coverage
   beside pads instead of leaving a hole there.

The dialog remembers what you last set (via type, layers, size, pattern,
offsets, net, and the avoid-* checkboxes) and pre-fills the next run with it,
unless a via was selected on the board first. **Reset settings**, at the
bottom, clears that and puts the built-in defaults back. **Reset last run**, at
the top, is about the board rather than the dialog: see below.

### Grouping, and undoing a run

All the vias from one run go into a group named after it, for example
`ViaStitching GND F.Cu:B.Cu`. That makes a run one thing rather than four
hundred:

- **Reset last run**, at the top of the dialog, deletes the newest run and
  refills the zones. It names the run and its via count beside the button, asks
  before deleting, and only ever touches vias this plugin placed and grouped.
- Or click any via so the whole group selects, then `Delete`.
- To delete one via, right-click it, choose *Grouping > Remove from Group*,
  then delete it.

On KiCad 10 and later `Ctrl+Z` also undoes a whole run, but only until the
board is saved and reopened. Reset last run works at any point after that, and
it is the only undo on KiCad 6 to 9, where a plugin cannot put anything on
KiCad's undo stack at all.

## How clearance is handled

There's no clearance field, on purpose. Six things keep the vias legal:

- **The fill inset.** A via is placed only where it sits at least its own radius
  (plus a small epsilon) inside the fill on every layer the net is poured on.
  KiCad has already pulled those fills back by the board clearance, so the inset
  keeps the via off other-net copper *on those layers*.
- **Other nets' tracks.** The inset says nothing about layers the net isn't
  poured on, and a via's drill crosses whatever copper layers it spans, so
  tracks of other nets are avoided on every layer within that span. This is
  unconditional: a via sitting on another net's track is a real DRC violation.
  A through via spans the whole board, so this still means every layer for it;
  a microvia or blind/buried via only checks the layers it actually connects.
- **Other nets' pad copper.** The same reasoning as tracks, applied to pads.
  The keepout follows the pad's rotated rectangle rather than a circle drawn
  around it, so a large rectangular pad doesn't also block the good pour at its
  corners. A through-hole pad blocks any span; an SMD pad only blocks a span
  that includes a layer it actually has copper on.
- **Rule areas.** A rule area that forbids vias is respected on every layer
  within the via's span, whichever layers it was drawn on. Same as above: a
  through via crosses the whole board, a microvia or blind/buried via only
  crosses the layers it spans.
- **Existing holes.** Via and pad drills are avoided with a 0.25 mm
  hole-to-hole margin. A milled slot gets a capsule following its long axis,
  rather than a circle as wide as the slot is long. An existing via only counts
  if its own layer span overlaps the new via's, so a front-side microvia pass
  doesn't block positions a back-side pass needs.
- **Clearance values** come from the board's netclasses, taking the larger of the
  two nets involved the way KiCad's own rules do. A netclass that just inherits
  the board minimum reports no value of its own, and those fall back to 0.2 mm.

The **Avoid other nets' zones** checkbox is off by default, because a via through
another net's zone is not a DRC error: KiCad pulls the fill back around it during
the refill this plugin already triggers. Tick it if you'd rather not perforate an
inner power plane at all. Expect far fewer vias, since on a typical 4-layer board
that plane covers most of the board.

The **Avoid pads already on this net** checkbox is off by default, so a via array
can land straight on a QFN or BGA ground pad, which is a normal use of stitching.
Tick it to keep vias off same-net pad copper as well. Pads on every other net are
avoided either way, with their full clearance.

Where a position is blocked, the via moves to the nearest clear spot within a
quarter of the spacing rather than being dropped. That limit is capped again by
the hole-to-hole margin, so two neighbours moved toward each other still clear
each other's drills, and a grid with no room to spare is left exactly on pitch.

If you see "No filled copper for net ...", fill the zones (`B`) and run again.

## Localization

The dialog follows whatever language KiCad itself is configured to show
(Preferences > General). Supported languages:

- English (source)
- Dutch
- German
- French
- Chinese (Simplified)

Anything without a catalog falls back to English. Catalogs live in
`plugins/locale/`, keyed by the English source strings, and are shared by both
builds.

## Two builds

KiCad has two plugin APIs, and this plugin ships one build for each:

| KiCad | Build | Sources | Version numbers |
| --- | --- | --- | --- |
| 10 and later | IPC (`kicad-python` / `kipy`) | `plugins/` | 2.x |
| 6, 7, 8, 9 | SWIG (`pcbnew` bindings) | `plugins_legacy/` | 1.x |

KiCad is retiring the SWIG bindings. KiCad 10 has already dropped parts of
them, including the via type constants this plugin needs, and KiCad 11 removes
them altogether, so the IPC build takes over from 10 onward. Below that, KiCad
6, 7 and 8 have no API server for it to talk to, and KiCad 9's is missing the
call it uses to check that the board kept the vias it was given.

The two APIs differ down to how copper layers are numbered, so each build is
its own implementation of the same dialog. Both are tested against the KiCad
versions they claim, the SWIG build on real KiCad 6, 7, 8 and 9 in CI.

Version numbers stay on separate tracks, the SWIG one always lower, so that
upgrading from KiCad 9 to 10 arrives as an ordinary package update.

## Building the packages

- `python build.py` produces `dist/via-stitching-<version>.zip`, the IPC build.
- `python build.py --legacy` produces `dist/via-stitching-swig-<version>.zip`,
  the SWIG build.

Both are laid out the way the Plugin and Content Manager expects, and each
writes its own archive's SHA-256 and sizes into `metadata.json`. One tag builds
and publishes both, and `tools/check_release.py` refuses the tag if the tag and
`metadata.json` disagree.

Tests live in `tests/ipc/` and `tests/legacy/`. The IPC suites run offline
against a fake board; the SWIG suites need a real `pcbnew`, so run those with
KiCad's own interpreter:

```
"C:/Program Files/KiCad/6.0/bin/python.exe" tests/legacy/test_helpers_legacy.py
```

## License

[GPL-3.0-or-later](LICENSE). Independent IPC re-implementation of the
via-stitching idea from JS Reynaud's earlier plugin.
