"""Small publication helpers for complete, immutable delivery directories."""
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def fingerprint(path, *, raster_sidecars=False):
    """Hash an explicit single-file input without importing a GIS backend."""
    path = Path(path)
    if raster_sidecars and any(Path(str(path) + suffix).exists()
                               for suffix in ('.msk', '.aux.xml', '.ovr')):
        raise ValueError('External raster sidecars unsupported in recipes')
    if not path.is_file():
        raise ValueError('Daily input requires a single-file dataset')
    if path.suffix.lower() == '.shp':
        raise ValueError('Convert multifile Shapefile to GPKG before fingerprinted daily processing')
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('Input has active SQLite sidecars')
    return digest(path)


def implementation_modules(entry):
    """The script and every sibling it imports or names through with_name(), transitively."""
    entry = Path(entry).resolve()
    found = {}
    pending = [entry]
    while pending:
        path = pending.pop()
        if path.name in found:
            continue
        found[path.name] = path
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'), filename=path.name)):
            if isinstance(node, ast.Import):
                names = [alias.name + '.py' for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module + '.py']
            elif isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'with_name' \
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names = [node.args[0].value]
            else:
                continue
            pending += [entry.with_name(name) for name in names if entry.with_name(name).is_file()]
    return dict(sorted(found.items()))


def implementation_hashes(entry):
    """Hash what actually runs, so a new shared module cannot fall out of a record unnoticed."""
    return {name: digest(path) for name, path in implementation_modules(entry).items()}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


@contextmanager
def bundle(output):
    output = Path(output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_name('.' + output.name + '.publish-lock')
    lock.mkdir()
    staging = None
    try:
        if output.exists() or output.is_symlink():
            raise ValueError('Output must be a new directory')
        staging = Path(tempfile.mkdtemp(prefix='.delivery-', dir=output.parent))
        yield staging
        if output.exists() or output.is_symlink():
            raise ValueError('Output appeared during processing')
        os.rename(staging, output)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        lock.rmdir()
