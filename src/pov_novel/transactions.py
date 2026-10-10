"""Recoverable file-set commits. Call with the project's OS lock held.

Readers also take that lock: renaming several paths is recoverable, but cannot
be made simultaneously visible to unrelated processes that ignore the lock.
"""
import json
import os
import shutil
import uuid
from pathlib import Path


def _sync_directory(path):
    # Windows has no portable directory fsync; file contents/journals are flushed.
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _cleanup(root, folder):
    base = (root / '.runtime' / 'transactions').resolve()
    target = folder.resolve()
    if not base.is_relative_to(root) or target == base or not target.is_relative_to(base) or folder.is_symlink():
        raise ValueError('事务清理路径超出项目')
    shutil.rmtree(target)


def _durable(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _journal(folder, data):
    temporary = folder / 'journal.tmp'
    _durable(temporary, json.dumps(data).encode('utf-8'))
    os.replace(temporary, folder / 'journal.json')
    _sync_directory(folder)


def _target(root, relative):
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError('事务路径超出项目')
    return target


def _rollback(root, folder, journal):
    # Backups stay intact so interrupted recovery is itself repeatable.
    for entry in reversed(journal['entries']):
        target = _target(root, entry['path'])
        if entry['existed']:
            temporary = target.with_name(target.name + '.restore-' + folder.name)
            _durable(temporary, (folder / 'backup' / entry['slot']).read_bytes())
            os.replace(temporary, target)
            _sync_directory(target.parent)
        else:
            target.unlink(missing_ok=True)


def recover_transactions(root):
    root = Path(root).resolve()
    directory = root / '.runtime' / 'transactions'
    if not directory.resolve().is_relative_to(root):
        raise ValueError('事务目录超出项目')
    if not directory.exists():
        return
    for folder in sorted(directory.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        path = folder / 'journal.json'
        if path.exists():
            journal = json.loads(path.read_text(encoding='utf-8'))
            if journal['phase'] == 'applying':
                _rollback(root, folder, journal)
            elif journal['phase'] not in ('prepared', 'committed'):
                raise ValueError('未知事务阶段，未自动清理')
        _cleanup(root, folder)


def atomic_file_set(root, files, *, fault=None):
    """files maps project-relative names to bytes/text/JSON; JSON is explicit."""
    root = Path(root).resolve()
    folder = root / '.runtime' / 'transactions' / uuid.uuid4().hex
    if not folder.resolve().is_relative_to(root):
        raise ValueError('事务目录超出项目')
    folder.mkdir(parents=True)
    entries = []
    targets = set()
    try:
        for index, (relative, value) in enumerate(files.items()):
            target = _target(root, relative)
            if target in targets:
                raise ValueError('重复事务目标')
            targets.add(target)
            data = value if isinstance(value, bytes) else value.encode('utf-8')
            slot = str(index)
            _durable(folder / 'stage' / slot, data)
            if target.exists():
                _durable(folder / 'backup' / slot, target.read_bytes())
            entries.append({'path': str(target.relative_to(root)), 'slot': slot, 'existed': target.exists()})
        journal = {'schema_version': 2, 'pid': os.getpid(), 'phase': 'prepared', 'entries': entries}
        _journal(folder, journal)
        journal['phase'] = 'applying'
        _journal(folder, journal)
        for index, entry in enumerate(entries):
            target = _target(root, entry['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(folder / 'stage' / entry['slot'], target)
            _sync_directory(target.parent)
            if fault:
                fault(index)
        journal['phase'] = 'committed'
        _journal(folder, journal)
    except Exception:
        if (folder / 'journal.json').exists():
            saved = json.loads((folder / 'journal.json').read_text())
            if saved['phase'] == 'applying':
                _rollback(root, folder, saved)
        _cleanup(root, folder)
        raise
    # BaseException simulates process death; the next lock holder recovers it.
    _cleanup(root, folder)


def json_files(files):
    return {path: json.dumps(value, ensure_ascii=False, indent=2) + '\n' for path, value in files.items()}
