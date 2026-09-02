# Shared readers for KiCad's own per-version config files.
#
# One place that knows where KiCad's config lives on each OS, and how to read
# the one preference (UI language) this plugin matches itself to, instead of
# every caller repeating the same directory-search and try/except dance.
#
# License: GPL-3.0-or-later

import json
import os
import sys


def kicad_config_dirs():
    """KiCad's per-version config directories, newest version first.

    KiCad keeps per-version settings (kicad_common.json, pcbnew.json,
    colors/) under one directory per platform.
    """
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
        # Version subdirectories, newest first, so KiCad 11 wins over 10.
        versions = sorted(
            (d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))),
            key=lambda d: [int(p) for p in d.split(".") if p.isdigit()] or [0],
            reverse=True,
        )
    except Exception:
        return []
    return [os.path.join(root, name) for name in versions]


def ui_language():
    """KiCad's configured UI language, as its own display-name string (e.g.
    "English", "Dutch"), or None if we cannot tell."""
    try:
        for config_dir in kicad_config_dirs():
            path = os.path.join(config_dir, "kicad_common.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh).get("system", {}).get("language")
    except Exception:
        pass
    return None
