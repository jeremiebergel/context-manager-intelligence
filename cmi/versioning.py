"""Which CMI is running, and which one is installed.

An update replaces the files on disk, but a process that already imported CMI (Hermes gateway or
desktop backend, a long CLI session, ``cmi-proxy``) keeps running the old code until it restarts.
Its sessions then run the old engine and nothing shows it. These helpers let the savings ledger,
``/cmi``, ``get_status()`` and ``cmi-proxy status`` say which version ran and when a restart is due.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_INIT = Path(__file__).with_name("__init__.py")
_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)


def running_version() -> str:
    from . import __version__
    return __version__


def installed_version() -> Optional[str]:
    """Version declared by the CMI files on disk next to the running code; None if unreadable."""
    try:
        m = _VERSION_RE.search(_INIT.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return m.group(1) if m else None


def stale_install() -> Optional[str]:
    """The installed version when it differs from the code this process loaded, else None."""
    disk = installed_version()
    return disk if disk and disk != running_version() else None
