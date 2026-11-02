"""Text files are UTF-8 on every platform; the locale (a Windows code page) must never decide."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
ASCII_LOCALE = {'LC_ALL': 'C', 'LANG': 'C', 'PYTHONCOERCECLOCALE': '0', 'PYTHONUTF8': '0'}


def text_calls_without_encoding(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or any(k.arg == 'encoding' for k in node.keywords):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in ('read_text', 'write_text'):
            yield node
        elif isinstance(node.func, ast.Name) and node.func.id == 'open':
            mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else 'r'
            if isinstance(mode, str) and 'b' not in mode and not any(k.arg == 'mode' for k in node.keywords):
                yield node


def test_scripts_name_the_encoding_of_every_text_file():
    found = [f'{path.name}:{node.lineno}' for path in sorted(SCRIPTS.glob('*.py'))
             for node in text_calls_without_encoding(ast.parse(path.read_text(encoding='utf-8')))]
    assert not found, found


def test_written_json_is_utf8_whatever_the_locale(tmp_path):
    # The command line stays ASCII: under LC_ALL=C on Linux, Python cannot even decode a Chinese -c argument.
    target = tmp_path / 'out.json'
    code = f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); from _delivery import write_json; write_json({str(target)!r}, {{'title': {ascii('用地分类')}}})"
    run = subprocess.run([sys.executable, '-B', '-X', 'utf8=0', '-c', code], capture_output=True, env={**os.environ, **ASCII_LOCALE})
    assert run.returncode == 0, run.stderr
    assert json.loads(target.read_bytes().decode('utf-8'))['title'] == '用地分类'


@pytest.mark.skipif(os.name == 'nt', reason='the fake tool is a shell script')
def test_seven_zip_listing_uses_forward_slashes(tmp_path, monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    import package
    fake = tmp_path / '7za'
    fake.write_text("#!/bin/sh\nprintf '%s\\n' 'Path = v107\\0000demo.lyr' 'Size = 3' 'Attributes = A' ''\n", encoding='utf-8')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    archive = tmp_path / 'a.7z'
    archive.write_bytes(b"7z\xbc\xaf'\x1c")
    rows, tool = package.list_members(archive, '7z')
    assert tool == '7za'
    assert rows[0][0] == 'v107/0000demo.lyr'
