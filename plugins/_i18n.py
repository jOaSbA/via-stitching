# Loads this plugin's own UI string catalog, matching KiCad's configured
# language.
#
# Deliberately not GNU gettext: a handful of languages and well under a
# hundred strings don't need a .po/.mo compile step (an external msgfmt
# toolchain) -- a flat JSON dict per language, keyed by the literal English
# source string, is simpler and needs no new dependency or build step.
#
# License: GPL-3.0-or-later

import json
import os

from _kicad_config import ui_language  # noqa: E402

# KiCad's own display name for each language it can show its UI in (some are
# English names, some native). Extend as catalogs are added to locale/.
_LANGUAGE_TO_CODE = {
    "English": "en",
    "Dutch": "nl",
    "Nederlands": "nl",
    "German": "de",
    "Deutsch": "de",
    "French": "fr",
    "Français": "fr",
    "Chinese": "zh",
}

_CATALOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
_catalog_cache = {}


def _catalog(code):
    if code not in _catalog_cache:
        path = os.path.join(_CATALOG_DIR, f"{code}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                _catalog_cache[code] = json.load(fh)
        except Exception:
            _catalog_cache[code] = {}
    return _catalog_cache[code]


_active_catalog = _catalog(_LANGUAGE_TO_CODE.get(ui_language(), "en"))


def _(text):
    """Translate `text` if the active catalog has it, else return it as-is.

    English is never a separate catalog file -- the source strings passed to
    this function *are* the English fallback.
    """
    return _active_catalog.get(text, text)
