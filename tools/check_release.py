#!/usr/bin/env python3
"""Check that a release tag and metadata.json agree, before anything is published.

    python3 tools/check_release.py v2.2.0

Prints the two archive names for the workflow to attach, as GITHUB_OUTPUT
lines, and exits non-zero with a reason if the tag cannot be released.

One tag ships both runtimes: the tag names the IPC version, and both archives
are attached to that one release, so both download_urls have to point at it.
Pointing one at an older release is the easy mistake, and without this it
surfaces at PCM validation weeks later instead of here.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def newest(versions, runtime):
    entries = [v for v in versions if v.get("runtime", "swig") == runtime]
    if not entries:
        sys.exit("metadata.json has no {} version entry".format(runtime))
    return entries[-1]


def main(tag):
    with open(os.path.join(HERE, "metadata.json"), encoding="utf-8") as fh:
        versions = json.load(fh)["versions"]

    ipc = newest(versions, "ipc")
    swig = newest(versions, "swig")

    if tag != "v" + ipc["version"]:
        sys.exit("tag {} does not match metadata.json's newest IPC version {}"
                 .format(tag, ipc["version"]))

    for entry in (ipc, swig):
        url = entry.get("download_url", "")
        if "/download/{}/".format(tag) not in url:
            sys.exit("the {} download_url does not point at {}: {}"
                     .format(entry.get("runtime", "swig"), tag, url))

    print("ipc_archive=via-stitching-{}.zip".format(ipc["version"]))
    print("swig_archive=via-stitching-swig-{}.zip".format(swig["version"]))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
