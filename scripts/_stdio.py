"""Standard streams of a tool run directly: UTF-8 on pipes and files, like a run through gis.py, whatever the locale."""
from __future__ import annotations

import os
import sys


def utf8() -> None:
    """Call first in `if __name__ == '__main__'`. A character the stream cannot write comes out escaped, never as a crash."""
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, 'reconfigure'):
            continue
        # A terminal shows its own encoding (a Windows console is UTF-8 to Python already); PYTHONIOENCODING is the caller's choice.
        if stream.isatty() or os.environ.get('PYTHONIOENCODING'):
            stream.reconfigure(errors='backslashreplace')
        else:
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
