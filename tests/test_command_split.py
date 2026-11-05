"""Command strings given to benchmark.py: a Windows backslash is part of a path, a POSIX one an escape."""
from pathlib import Path
import shlex
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark

WINDOWS_PYTHON = r'C:\Users\me\gis runtime\Scripts\python.exe'


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(benchmark, 'WINDOWS', True)


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(benchmark, 'WINDOWS', False)


@pytest.mark.parametrize('command, parts', [
    (r'C:\Users\me\python.exe tool.py', [r'C:\Users\me\python.exe', 'tool.py']),
    (r'"C:\Program Files\py.exe" tool.py', [r'C:\Program Files\py.exe', 'tool.py']),
    (r"'C:\Program Files\py.exe' tool.py", [r'C:\Program Files\py.exe', 'tool.py']),
    (r'tool.py "C:\out dir\" --flag', ['tool.py', 'C:\\out dir\\', '--flag']),
    (r'tool.py \\server\share\a.gpkg', ['tool.py', r'\\server\share\a.gpkg']),
    (r'tool.py --where "a # b" # c', ['tool.py', '--where', 'a # b', '#', 'c']),
    ('tool.py ""', ['tool.py', '']),
])
def test_windows_keeps_backslashes(windows, command, parts):
    assert benchmark.split_command(command) == parts


@pytest.mark.parametrize('command', [
    'tool.py --where "status == \'active\'"',
    r'a\ b "c\"d" \\e',
    "x 'y z' # not a comment",
    'tool.py ""',
    '',
])
def test_posix_splits_as_shlex_does(posix, command):
    assert benchmark.split_command(command) == shlex.split(command)


def test_benchmark_case_on_windows(windows):
    assert benchmark.parse_case(rf'clip="{WINDOWS_PYTHON}" C:\tools\clip.py') == ('clip', [WINDOWS_PYTHON, r'C:\tools\clip.py'])
