"""Command strings given to batch.py and benchmark.py: a Windows backslash is part of a path, a POSIX one an escape."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import _command
import batch
import benchmark

WINDOWS_PYTHON = r'C:\Users\me\gis runtime\Scripts\python.exe'


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(_command, 'WINDOWS', True)


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(_command, 'WINDOWS', False)


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
    assert _command.split_command(command) == parts


@pytest.mark.parametrize('command', [
    'tool.py --where "status == \'active\'"',
    r'a\ b "c\"d" \\e',
    "x 'y z' # not a comment",
    'tool.py ""',
    '',
])
def test_posix_splits_as_shlex_does(posix, command):
    assert _command.split_command(command) == shlex.split(command)


def test_template_command_on_windows(windows):
    command = batch.build_template_command(Path('in/a b.gpkg'), Path('out'), f'"{WINDOWS_PYTHON}" C:\\tools\\clip.py {{input}} --output {{output}}')
    assert command == [WINDOWS_PYTHON, r'C:\tools\clip.py', str(Path('in/a b.gpkg')), '--output', str(Path('out/a b.gpkg'))]


def test_legacy_command_on_windows(windows):
    command = batch.build_legacy_command(Path('in/a.gpkg'), Path('out'), 'clip.py', [], f'"{WINDOWS_PYTHON}" -B')
    assert command[:3] == [WINDOWS_PYTHON, '-B', 'clip.py']


def test_batch_args_on_windows(windows, tmp_path, monkeypatch, capsys):
    (tmp_path / 'a.gpkg').touch()
    monkeypatch.setattr(sys, 'argv', ['batch.py', '--input-dir', str(tmp_path), '--output-dir', str(tmp_path / 'out'), '--script', 'clip.py',
                                      '--args', r'--mask C:\data\mask.gpkg', '--runner', f'"{WINDOWS_PYTHON}"', '--dry-run'])
    batch.main()
    assert r' --mask C:\data\mask.gpkg ' in capsys.readouterr().out


def test_dry_run_prints_what_windows_will_run(windows, tmp_path, monkeypatch, capsys):
    (tmp_path / 'a.gpkg').touch()
    monkeypatch.setattr(sys, 'argv', ['batch.py', '--input-dir', str(tmp_path), '--output-dir', str(tmp_path / 'out'),
                                      '--command', rf'"{WINDOWS_PYTHON}" C:\tools\clip.py', '--dry-run'])
    batch.main()
    assert rf'"{WINDOWS_PYTHON}" C:\tools\clip.py' in capsys.readouterr().out


def test_dry_run_on_posix_quotes_for_the_shell(posix):
    assert _command.show_command(['python3', 'a b.gpkg']) == "python3 'a b.gpkg'"


def test_benchmark_case_on_windows(windows):
    assert benchmark.parse_case(rf'clip="{WINDOWS_PYTHON}" C:\tools\clip.py') == ('clip', [WINDOWS_PYTHON, r'C:\tools\clip.py'])


def test_batch_runs_this_interpreter_through_a_template(tmp_path):
    """On Windows the real thing: sys.executable and the tool are backslash paths, and they must reach the process intact."""
    inputs, outputs = tmp_path / 'in dir', tmp_path / 'out dir'
    inputs.mkdir()
    (inputs / 'a.txt').write_text('payload', encoding='utf-8')
    tool = tmp_path / 'copy_file.py'
    tool.write_text('import shutil, sys\nshutil.copy(sys.argv[1], sys.argv[2])\n', encoding='utf-8')
    report = tmp_path / 'report.json'
    command = [sys.executable, str(SCRIPTS / 'batch.py'), '--input-dir', str(inputs), '--output-dir', str(outputs), '--pattern', '*.txt', '--workers', '1',
               '--command', f'"{sys.executable}" "{tool}" {{input}} {{output}}', '--report', str(report)]
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    dry = subprocess.run([*command, '--dry-run'], capture_output=True, encoding='utf-8', errors='replace', env=env)
    assert dry.returncode == 0, dry.stderr
    assert str(tool) in dry.stdout
    result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace', env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(report.read_text(encoding='utf-8'))['results'][0]['status'] == 'succeeded'
    assert (outputs / 'a.txt').read_text(encoding='utf-8') == 'payload'
