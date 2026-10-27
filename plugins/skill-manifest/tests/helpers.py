"""Shared test helpers (importable in both pytest and plain-module layouts)."""
from __future__ import annotations

import os
import shutil


def _writable_tmp_base():
    """Prefer /root on the small-VM host; fall back to the tempfile default
    (system tmp) where /root is not writable — e.g. GitHub CI runners."""
    if not os.path.isdir("/root"):
        return None
    probe = os.path.join("/root", ".skm-tmp-probe")
    try:
        os.mkdir(probe)
        return "/root"
    except OSError:
        return None
    finally:
        shutil.rmtree(probe, ignore_errors=True)
