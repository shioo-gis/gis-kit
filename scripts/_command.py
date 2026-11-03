"""Command lines given as one string: split into arguments, and shown back, the way this platform writes them."""
from __future__ import annotations

import os
import shlex
import subprocess

WINDOWS = os.name == "nt"


def split_command(command: str) -> list[str]:
    """shlex.split, except that on Windows a backslash is part of a path, not an escape."""
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if WINDOWS:
        lexer.escape = ""
    return list(lexer)


def show_command(command: list[str]) -> str:
    # Windows gets the command line subprocess will really run; shlex.join would wrap every backslash path in single quotes.
    return subprocess.list2cmdline(command) if WINDOWS else shlex.join(command)
