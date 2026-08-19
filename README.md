# Via Stitching

A KiCad 10 action plugin that fills the overlap of a net's copper zones (the top
and bottom GND pours, for example) with a grid of stitching vias. Through,
micro, blind, and buried vias are all supported, spanning whichever two copper
layers you pick.

It's built on KiCad's IPC API (`kicad-python` / `kipy`) rather than the old SWIG
`pcbnew` bindings, so it keeps working on KiCad 11 and later, where those
bindings are gone.

![The Via Stitching parameters dialog](docs/dialog.png?v=2)

## Requirements

- KiCad 10.0 or newer, with the IPC API server enabled under
  *Preferences > Plugins*.
- `kicad-python`, `wxPython`, and `shapely`, which KiCad installs for you on first
  run from `requirements.txt`.

## Install

### From the Plugin and Content Manager

*Tools > Plugin and Content Manager > Plugins*, find Via Stitching, install it,
and restart the PCB editor.

### Manually

1. Download the latest `via-stitching-x.y.z.zip` from the
   [Releases](https://github.com/jOaSbA/via-stitching/releases) page.
2. In the PCB editor: *Tools > Plugin and Content Manager > Install from File...*
   and pick the zip. You can also just unzip the `plugins/` contents into
   `Documents/KiCad/10.0/plugins/via_stitching/`.
3. Enable the IPC API server, then *Tools > External Plugins > Refresh Plugins*.

## Usage

1. Open a board and fill the zones first (`B`) so the pours have copper to read.
2. Optionally select an existing via first. Its type, layers, net, diameter,
   and drill are used to pre-fill the dialog, so cloning an existing via's
   settings is a matter of selecting it and running the plugin.
3. Run Via Stitching and set:
   - Via Type: Through, Micro, Blind, Buried, or Blind/Buried. Start Layer and
     End Layer narrow to what the chosen type can actually be (a Through via
     is always F.Cu/B.Cu; a microvia's end layer follows automatically once
     you pick which outer layer it starts on).
   - Via Diameter (mm) and Drill (mm) for the via size.
   - Via Pattern: Hexagonal (densest), Square, or Staggered, and Spacing (mm)
     for the centre-to-centre grid pitch.
   - X-Offset / Y-Offset (mm), for shifting the grid on a second pass so it
     doesn't land on top of a first one. Only relevant when stitching more
     than one via pattern onto the same net and area, for example a separate
     front-side and back-side microvia pass; disabled for Through vias, which
     only need one pass.
   - Net Name, the net to stitch (defaults to `GND`).
   - Avoid zones of other nets, off by default. See below.
   - Avoid footprints, off by default. Keeps vias out from under component
     bodies, for mechanical fit rather than clearance. Leave it off if you want
     thermal-via arrays under a QFN or BGA ground pad, which is a normal use of
     via stitching.
4. Click OK. Vias go only where the net is poured on both layers the via
   connects (and on any layer in between, where the net is also poured there),
   inset far enough to stay DRC-clean, and the zones are refilled for you.

### Grouping

All placed vias go into one group named `ViaStitching <net>`:

- To delete them all, click any via so the whole group selects, then `Delete`.
- To delete one, right-click a via, choose *Grouping > Remove from Group*, then
  delete it.

## How clearance is handled

There's no clearance field, on purpose. Five things keep the vias legal:

- **The fill inset.** A via is placed only where it sits at least its own radius
  (plus a small epsilon) inside the fill on every layer the net is poured on.
  KiCad has already pulled those fills back by the board clearance, so the inset
  keeps the via off other-net copper *on those layers*.
- **Other nets' tracks.** The inset says nothing about layers the net isn't
  poured on, and a through via's drill crosses those too, so tracks of other nets
  are avoided on every copper layer. This is unconditional: a via sitting on
  another net's track is a real DRC violation.
- **Rule areas.** A rule area that forbids vias is respected on every copper
  layer, whichever layers it was drawn on, since a through via crosses all of
  them.
- **Existing holes.** Via and pad drills are avoided with a 0.25 mm
  hole-to-hole margin. A milled slot is measured across its long axis. An
  existing via only counts if its own layer span overlaps the new via's, so a
  front-side microvia pass doesn't block positions a back-side pass needs.
- **Clearance values** come from the board's netclasses, taking the larger of the
  two nets involved the way KiCad's own rules do. A netclass that just inherits
  the board minimum reports no value over the IPC API, and those fall back to
  0.2 mm.

The **Avoid other nets' zones** checkbox is off by default, because a via through
another net's zone is not a DRC error: KiCad pulls the fill back around it during
the refill this plugin already triggers. Tick it if you'd rather not perforate an
inner power plane at all. Expect far fewer vias, since on a typical 4-layer board
that plane covers most of the board.

If you see "No filled copper for net ...", fill the zones (`B`) and run again.

## Building the package

Run `python build.py`. It produces `dist/via-stitching-<version>.zip` in the
layout the Plugin and Content Manager expects and writes the archive's SHA-256
and sizes into `metadata.json`.

## License

[GPL-3.0-or-later](LICENSE). Independent IPC re-implementation of the
via-stitching idea from JS Reynaud's earlier plugin.
