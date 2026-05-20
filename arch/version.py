"""arch/version.py -- the Archimedes release marker.

Bumped by the maintainer on every public release. Reads back through
``arch.__version__`` and surfaces in ``.ai arch status`` and the bot banner.
"""
from __future__ import annotations

ARCH_VERSION = "3.0.0"
ARCH_CODENAME = "Pivot"
