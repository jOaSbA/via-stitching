#!/usr/bin/env python3
"""Build a PCM release archive for this plugin.

Run from the repository root:

    python build.py            # the IPC build, for KiCad 10 and later
    python build.py --legacy   # the SWIG build, for KiCad 6, 7 and 8

It produces ``dist/<repo>-<version>.zip`` (or ``<repo>-legacy-<version>.zip``)
laid out the way the KiCad Plugin and Content Manager expects (``plugins/``,
``resources/icon.png``, ``metadata.json``), then fills ``download_sha256``,
``download_size`` and ``install_size`` back into the top-level ``metadata.json``
for the newest version of that runtime -- that file is what you submit to
https://gitlab.com/kicad/addons/metadata.

Both runtimes are one PCM package under a single identifier, told apart by the
``runtime`` field on each version entry and by the KiCad versions each one
declares. The plugin sources live in different directories but both install as
``plugins/``, which is the only place KiCad looks.

The copy of ``metadata.json`` placed *inside* the archive has the download_*
fields stripped, as required for packaged metadata.
"""

import argparse
import copy
import hashlib
import json
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.basename(HERE)
DIST = os.path.join(HERE, "dist")

# Source dir -> name inside the archive. The legacy backend lives in
# plugins_legacy/ so the two never collide in the repo, but it has to install as
# plugins/ like any other action plugin.
LAYOUTS = {
    "ipc": {"plugins": "plugins", "resources": "resources"},
    "swig": {"plugins_legacy": "plugins", "resources": "resources"},
}
# Never ship these.
EXCLUDE_NAMES = {"__pycache__", ".DS_Store"}
EXCLUDE_EXTS = (".pyc", ".pyo")

# A zip entry carries its file's mtime, so the same sources checked out twice
# produce archives with different bytes and different checksums. Stamping every
# entry with the zip epoch instead makes the archive depend only on its content,
# which is what lets CI rebuild a tagged release and get the checksum already
# committed in metadata.json.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _write(zf, arcname, data):
    info = zipfile.ZipInfo(arcname, date_time=ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def _collect(layout):
    """Yield (abs_path, arcname) for every file to place in the archive."""
    for top, packaged_as in layout.items():
        base = os.path.join(HERE, top)
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_NAMES]
            for fn in files:
                if fn in EXCLUDE_NAMES or fn.endswith(EXCLUDE_EXTS):
                    continue
                abs_path = os.path.join(root, fn)
                inside = os.path.relpath(abs_path, base).replace(os.sep, "/")
                yield abs_path, "{}/{}".format(packaged_as, inside)


def newest(meta, runtime):
    """The last version entry for this runtime. The PCM schema treats a missing
    runtime as swig, so read it the same way."""
    entries = [v for v in meta["versions"] if v.get("runtime", "swig") == runtime]
    if not entries:
        raise SystemExit("metadata.json has no {} version entry".format(runtime))
    return entries[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", action="store_true",
                        help="build the SWIG package for KiCad 6, 7 and 8")
    args = parser.parse_args()
    runtime = "swig" if args.legacy else "ipc"

    meta_path = os.path.join(HERE, "metadata.json")
    with open(meta_path, "r", encoding="utf-8") as fh:
        meta = json.load(fh)

    latest = newest(meta, runtime)
    version = latest["version"]

    os.makedirs(DIST, exist_ok=True)
    name = "{}-legacy-{}".format(REPO, version) if args.legacy else "{}-{}".format(REPO, version)
    zip_path = os.path.join(DIST, "{}.zip".format(name))

    # metadata.json inside the archive: only the version being packaged, with the
    # download_* fields stripped. PCM requires the in-package metadata to contain
    # exactly one version.
    packaged = copy.deepcopy(meta)
    built = copy.deepcopy(latest)
    for k in ("download_url", "download_sha256", "download_size", "install_size"):
        built.pop(k, None)
    packaged["versions"] = [built]

    install_size = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for abs_path, arcname in sorted(_collect(LAYOUTS[runtime]), key=lambda p: p[1]):
            with open(abs_path, "rb") as fh:
                _write(zf, arcname, fh.read())
            install_size += os.path.getsize(abs_path)
        packaged_bytes = json.dumps(packaged, indent=2).encode("utf-8")
        _write(zf, "metadata.json", packaged_bytes)
        install_size += len(packaged_bytes)

    with open(zip_path, "rb") as fh:
        data = fh.read()
    sha256 = hashlib.sha256(data).hexdigest()
    download_size = len(data)

    latest["download_sha256"] = sha256
    latest["download_size"] = download_size
    latest["install_size"] = install_size
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
        fh.write("\n")

    print("Built {}".format(zip_path))
    print("  runtime        {}".format(runtime))
    print("  version        {}".format(version))
    print("  download_size  {} bytes".format(download_size))
    print("  install_size   {} bytes".format(install_size))
    print("  download_sha256 {}".format(sha256))
    print()
    print("metadata.json updated. Attach the zip to the GitHub release at:")
    print("  {}".format(latest.get("download_url", "<set download_url in metadata.json>")))


if __name__ == "__main__":
    main()
