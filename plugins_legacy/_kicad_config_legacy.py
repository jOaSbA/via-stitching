# Shared reader for KiCad's own per-version config directory.
#
# Same logic as the IPC version's _kicad_config.py (kept separate since the
# two backends ship as independent packages, not a shared dependency) --
# used to find the active color theme for the layer-color swatches and to
# pick a writable location for this dialog's own remembered settings.
#
# License: GPL-3.0-or-later

import os
import sys


def kicad_config_dirs():
    """KiCad's per-version config directories, newest version first."""
    root = os.environ.get("KICAD_CONFIG_HOME")
    if not root:
        if sys.platform == "win32":
            root = os.path.join(os.environ.get("APPDATA", ""), "kicad")
        elif sys.platform == "darwin":
            root = os.path.expanduser("~/Library/Preferences/kicad")
        else:
            root = os.path.join(
                os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                "kicad",
            )
    try:
        versions = sorted(
            (d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))),
            key=lambda d: [int(p) for p in d.split(".") if p.isdigit()] or [0],
            reverse=True,
        )
    except Exception:
        return []
    return [os.path.join(root, name) for name in versions]
