# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
# -*- coding: utf-8 -*-
'''core/updater.py - SagaAI self-update pipeline: check, stage, apply, rollback.

Downloads the public update manifest and file payloads from the configured
channel (unauthenticated raw.githubusercontent.com URLs) and stages them
under .dev_agent/updates/pending/. The actual file replacement is delegated
to core.updater_apply, which runs at cold start (app.py in Step 5) so a
running process never replaces its own code.

The install root is derived from this file location: <root>/core/updater.py.

CLI (run from the project root):
    python3 core/updater.py check       - compare installed files vs manifest
    python3 core/updater.py stage       - download changed files into pending/
    python3 core/updater.py apply       - apply staged files (cold start)
    python3 core/updater.py rollback    - restore the last applied run

Pure standard library plus core.updater_apply and core.version. Never raises:
every command returns a JSON-serializable report.
'''

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

# Keep the project root importable when executed directly:
#   python3 core/updater.py <command>
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.updater_apply import (
    apply_pending,
    load_state,
    pending_dir,
    read_pending,
    rollback as _rollback_store,
    updates_dir,
)
from core.version import __version__

DEFAULT_CHANNEL = 'https://raw.githubusercontent.com/TVD2100/sagaai-platform/main'
MANIFEST_NAME = 'file_versions.json'
RUNNING_FILE_REL = '.dev_agent/running.json'
PENDING_REL = '.dev_agent/updates/pending.json'  # must match updater_apply constants
HEALTH_REL = '.dev_agent/updates/health.json'
USER_AGENT = 'SagaAI-Updater/1.0'
DOWNLOAD_TIMEOUT = 30


# ─── small helpers ────────────────────────────────────────────────────────────

def default_root():
    '''Return the install root (parent of the core/ package).'''
    return str(Path(__file__).resolve().parent.parent)


def _fail(error):
    return {'ok': False, 'error': error}


def _read_json_file(path, default=None):
    '''Read and parse a JSON file; return default on any error.'''
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_text(root, rel, text):
    '''Atomically write a text file: temp file + os.replace.'''
    dst = os.path.join(root, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + '.tmp-%d' % os.getpid()
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _manifest_path(root):
    return os.path.join(updates_dir(root), MANIFEST_NAME)


def running_marker_path(root):
    '''Path of the live marker written by app.py (Step 5).'''
    return os.path.join(root, RUNNING_FILE_REL)


def write_running_marker(root):
    '''Write the live marker (pid + started time); used by app.py after the
    cold-start apply so CLI commands know the app is live.'''
    _atomic_write_text(
        root,
        RUNNING_FILE_REL,
        json.dumps(
            {"pid": os.getpid(), "at": datetime.now(timezone.utc).isoformat()}
        ),
    )


def same_process_marker(root):
    '''True when the running marker for <root> was written by THIS process.

    streamlit re-executes app.py on every rerun inside the same OS process,
    so app.py uses this helper to run the cold-start apply hook only once.
    On Windows this also matters for safety: os.kill(pid, 0) is not an
    existence probe there, it sends CTRL_C_EVENT to our own console group.
    '''
    marker = running_marker_path(root)
    if not os.path.isfile(marker):
        return False
    data = _read_json_file(marker, None)
    if not isinstance(data, dict):
        return False
    pid = data.get('pid')
    return isinstance(pid, int) and pid == os.getpid()


def _pid_state(pid):
    """Return the ps state letter (uppercase) of <pid>, or None."""
    if os.name != 'posix':
        return None
    try:
        out = subprocess.run(['ps', '-p', str(pid), '-o', 'state='], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    state = (out.stdout or '').strip().upper()
    return state or None


def _pid_start_time(pid):
    """Best-effort process start time (Unix time) of <pid>, or None."""
    if os.name != 'posix':
        return None
    try:
        out = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or '').strip()
    if not text:
        return None
    try:
        stamp = datetime.strptime(text, '%a %b %d %H:%M:%S %Y')
    except ValueError:
        return None
    return stamp.timestamp()


def _pid_alive_windows(pid):
    """True when process <pid> exists on Windows; never sends signals.

    OpenProcess + GetExitCodeProcess is a real existence check. Unlike
    os.kill(pid, 0), it never delivers a console control event: CPython on
    Windows maps signal 0 to CTRL_C_EVENT for its own console group, which
    kills the running streamlit server. Returns False on any failure
    (permission denied, process gone, kernel call failed).
    """
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259  # a living process reports this pseudo exit code
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _marker_unixtime(value):
    """Parse an ISO timestamp into Unix time (UTC), or None."""
    if not isinstance(value, str) or not value:
        return None
    stamp = None
    value_iso = value.rstrip()
    if value_iso.endswith('Z'):
        value_iso = value_iso[:-1] + '+00:00'
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S'):
        try:
            stamp = datetime.strptime(value_iso, fmt)
            break
        except ValueError:
            continue
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def is_app_running(root):
    '''True when app.py has written a live running marker for <root>.

    Detection rules:
    - no marker / broken marker -> not running;
    - invalid or dead pid -> not running;
    - alive pid whose process is stopped (state T, e.g. Ctrl+Z) or a
      zombie (state Z) -> not running (it cannot serve the app);
    - alive pid whose process start time is much older than the marker
      'at' -> the pid was reused by an unrelated process -> not running;
    - ps unavailable -> conservative fallback: an alive pid is running.
    '''
    marker = running_marker_path(root)
    if not os.path.isfile(marker):
        return False
    data = _read_json_file(marker, None)
    if not isinstance(data, dict):
        return False
    pid = data.get('pid')
    if not (isinstance(pid, int) and pid > 0):
        return False
    if pid == os.getpid():
        # This process wrote the marker; it is certainly alive. Also avoids
        # os.kill on Windows, where signal 0 is CTRL_C_EVENT (console kill).
        return True
    state = _pid_state(pid)
    if state is None:
        # ps unavailable: conservative fallback.
        if os.name == 'nt':
            # NEVER use os.kill on Windows: it maps to a console-control
            # event for the current console group, not an existence probe.
            return _pid_alive_windows(pid)
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    if state == 'Z' or state.startswith('T'):
        return False
    started = _pid_start_time(pid)
    marker_at = _marker_unixtime(data.get('at'))
    if started is not None and marker_at is not None:
        # A process older than the marker (minus a 300 s clock-skew window)
        # could not have written it: the pid must have been reused.
        if started < marker_at - 300:
            return False
    return True


def _parse_semver(value):
    '''Parse X.Y.Z[-pre] into a comparable tuple, or None when invalid.'''
    if not isinstance(value, str):
        return None
    s = value.strip().lstrip('vV')
    core_part, _, pre = s.partition('-')
    parts = core_part.split('.')
    if len(parts) != 3:
        return None
    try:
        nums = tuple(int(p) for p in parts)
    except ValueError:
        return None
    pre_t = tuple(pre.split('.')) if pre else ()
    return nums, pre_t


def _compare_versions(a, b):
    '''Compare two semver strings: 1/0/-1/None (None = unordered).'''
    pa, pb = _parse_semver(a), _parse_semver(b)
    if pa is None or pb is None:
        return None
    if pa[0] != pb[0]:
        return 1 if pa[0] > pb[0] else -1
    pra, prb = pa[1], pb[1]
    if not pra and prb:
        return 1
    if pra and not prb:
        return -1
    return (pra > prb) - (pra < prb)


def _effective_channel(manifest, requested):
    '''Prefer the channel declared inside the manifest when the caller
    asked for the built-in default; an explicit custom channel (a local
    mirror or test server) wins and stays consistent with the fetch.
    '''
    declared = manifest.get('channel')
    if declared and requested == DEFAULT_CHANNEL:
        return declared
    return requested


# ─── manifest access ──────────────────────────────────────────────────────────

def fetch_manifest(channel=DEFAULT_CHANNEL):
    '''Download and parse the update manifest; returns (dict, error).'''
    url = channel.rstrip('/') + '/' + MANIFEST_NAME
    request = Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            raw = response.read()
    except Exception as exc:  # noqa: BLE001 - any network/protocol failure
        return None, 'cannot fetch manifest from %s: %s' % (url, exc)
    try:
        return json.loads(raw.decode('utf-8')), None
    except (ValueError, UnicodeDecodeError) as exc:
        return None, 'manifest is not valid JSON: %s' % exc


def _validate_manifest(manifest):
    if not isinstance(manifest, dict):
        return None, 'manifest is not a JSON object'
    units = manifest.get('units')
    selectable = manifest.get('selectable')
    if not isinstance(units, dict) or not isinstance(selectable, dict):
        return None, 'manifest must contain object fields units and selectable'
    return manifest, None


def _manifest_entries(manifest):
    '''Flatten units + selectable into {rel: {version, sha256}} plus an
    error string. Every entry must carry a non-empty sha256.
    '''
    entries = {}
    for unit_name, unit in manifest.get('units', {}).items():
        files = unit.get('files') if isinstance(unit, dict) else None
        if not isinstance(files, dict):
            return None, 'unit %r has no files object' % unit_name
        for rel, entry in files.items():
            if not isinstance(entry, dict) or not entry.get('sha256'):
                return None, 'bad entry for %s in unit %s' % (rel, unit_name)
            entries[rel] = entry
    for rel, entry in manifest.get('selectable', {}).items():
        if not isinstance(entry, dict) or not entry.get('sha256'):
            return None, 'bad selectable entry for %s' % rel
        entries[rel] = entry
    return entries, None


# ─── commands ─────────────────────────────────────────────────────────────────

def check_updates(root, manifest=None, channel=DEFAULT_CHANNEL):
    '''Compare installed files against the update manifest.

    Returns {ok, error, app_version, available, up_to_date, source}.
    Each available item: {path, action=new|update, version,
    local_version, sha256}. Files are compared by sha256; version is
    informational. local_version comes from state.json (last applied).
    '''
    if manifest is None:
        manifest, err = fetch_manifest(channel)
        if err is not None:
            return _fail(err)
    manifest, err = _validate_manifest(manifest)
    if err is not None:
        return _fail(err)
    entries, err = _manifest_entries(manifest)
    if err is not None:
        return _fail(err)
    channel = _effective_channel(manifest, channel)
    state_entries = load_state(root).get('entries', {})
    available = []
    for rel in sorted(entries):
        entry = entries[rel]
        sha = entry.get('sha256', '')
        local_path = os.path.join(root, rel)
        if not os.path.isfile(local_path):
            action = 'new'
        elif _sha256_file(local_path) != sha:
            action = 'update'
        else:
            continue
        local_version = (state_entries.get(rel) or {}).get('version')
        available.append({
            'path': rel,
            'action': action,
            'version': entry.get('version', ''),
            'local_version': local_version,
            'sha256': sha,
        })
    remote_version = manifest.get('app_version')
    cmp_value = _compare_versions(__version__, str(remote_version)) if remote_version else None
    return {
        'ok': True,
        'error': None,
        'app_version': {
            'local': __version__,
            'remote': remote_version,
            'newer_remote': bool(cmp_value is not None and cmp_value < 0),
        },
        'available': available,
        'up_to_date': not available,
        'source': channel,
    }


def stage_updates(root, selection=None, manifest=None, channel=DEFAULT_CHANNEL):
    '''Download selected (or all available) files into pending/ and write
    pending.json.

    Each download is sha256-checked against the manifest before it is moved
    into the pending store; on any mismatch the file is discarded and the
    command fails. selection defaults to every file reported by
    check_updates. Existing pending.json entries are merged, so several
    stage commands accumulate into one apply run.
    '''
    if manifest is None:
        manifest, err = fetch_manifest(channel)
        if err is not None:
            return _fail(err)
    manifest, err = _validate_manifest(manifest)
    if err is not None:
        return _fail(err)
    entries, err = _manifest_entries(manifest)
    if err is not None:
        return _fail(err)
    channel = _effective_channel(manifest, channel)
    if selection is None:
        check = check_updates(root, manifest=manifest, channel=channel)
        if not check.get('ok'):
            return _fail(check.get('error') or 'check failed')
        selection = [item['path'] for item in check['available']]
    selection = list(dict.fromkeys(selection))
    if not selection:
        return {
            'ok': True,
            'error': None,
            'staged': [],
            'pending_path': _manifest_path(root),
        }
    staged = []
    errors = []
    for rel in selection:
        entry = entries.get(rel)
        if entry is None:
            errors.append('%s is not in the manifest' % rel)
            continue
        dst = os.path.join(pending_dir(root), rel)
        err = _download_file(channel, rel, dst, entry['sha256'])
        if err is not None:
            errors.append('%s: %s' % (rel, err))
            continue
        staged.append(rel)
    if errors:
        return {
            'ok': False,
            'error': '; '.join(errors),
            'staged': staged,
            'pending_path': _manifest_path(root),
        }
    current = read_pending(root)[0] or {}
    files = dict(current.get('files') or {})
    for rel in staged:
        files[rel] = {
            'version': entries[rel].get('version', ''),
            'sha256': entries[rel]['sha256'],
        }
    selected = list(current.get('selected') or [])
    for rel in staged:
        if rel not in selected:
            selected.append(rel)
    _atomic_write_text(
        root,
        PENDING_REL,
        json.dumps({'files': files, 'selected': selected},
                   ensure_ascii=False, indent=2) + '\n',
    )
    return {
        'ok': True,
        'error': None,
        'staged': staged,
        'pending_path': _manifest_path(root),
    }


def _download_file(channel, rel, dst, expected_sha):
    '''Download one raw file, verify its sha256, move it to dst.

    Returns None on success or an error message. Parent directories are
    created as needed; a failed download never leaves a partial file.
    '''
    url = channel.rstrip('/') + '/' + quote(rel, safe='/')
    request = Request(url, headers={'User-Agent': USER_AGENT})
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + '.tmp-%d' % os.getpid()
    hasher = hashlib.sha256()
    try:
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            with open(tmp, 'wb') as out:
                for chunk in iter(lambda: response.read(65536), b''):
                    hasher.update(chunk)
                    out.write(chunk)
    except Exception as exc:  # noqa: BLE001 - transport failure
        if os.path.exists(tmp):
            os.remove(tmp)
        return 'download failed: %s' % exc
    actual = hasher.hexdigest()
    if actual != expected_sha:
        os.remove(tmp)
        return 'sha256 mismatch (expected %s, actual %s)' % (expected_sha, actual)
    os.replace(tmp, dst)
    return None


def apply_updates(root, force=False, logger=None):
    '''Apply staged updates via core.updater_apply.apply_pending.

    Refuses while the app is running (a live running marker in .dev_agent/)
    unless force=True - the normal path is the cold-start hook in app.py,
    which clears the marker before applying.
    '''
    if is_app_running(root) and not force:
        error = 'the app is running; apply at cold start or use force'
        report = {
            'ok': False,
            'applied': [],
            'failed': True,
            'error': error,
            'logs': ['refused: ' + error],
            'health_path': None,
        }
        try:
            health = {
                'ok': False,
                'at': datetime.now(timezone.utc).isoformat(),
                'applied': [],
                'error': error,
                'details': ['refused: ' + error],
            }
            _atomic_write_text(
                root,
                HEALTH_REL,
                json.dumps(health, ensure_ascii=False, indent=2) + '\n',
            )
            report['health_path'] = os.path.join(root, HEALTH_REL)
        except OSError:
            pass
        if logger is not None:
            logger(error)
        return report
    return apply_pending(root, logger=logger)


def rollback_updates(root, rel=None, run_id=None, force=False):
    '''Delegate to core.updater_apply.rollback (single file or whole run).

    Refuses while the app is running unless force=True (rollback replaces
    files the running process may already have loaded).
    '''
    if is_app_running(root) and not force:
        return {
            'ok': False,
            'restored': [],
            'error': 'the app is running; rollback at cold start or use force',
        }
    return _rollback_store(root, rel=rel, run_id=run_id)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _build_cli():
    parser = argparse.ArgumentParser(
        prog='updater',
        description='SagaAI update pipeline (check / stage / apply / rollback)',
    )
    parser.add_argument(
        '--root',
        default=default_root(),
        help='install root (default: parent of core/)',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    check_parser = sub.add_parser('check', help='compare against remote manifest')
    check_parser.add_argument('--channel', default=DEFAULT_CHANNEL)
    check_parser.add_argument('--manifest', help='local manifest JSON file (offline)')

    stage_parser = sub.add_parser('stage', help='download files into pending/')
    stage_parser.add_argument('--channel', default=DEFAULT_CHANNEL)
    stage_parser.add_argument('--manifest', help='local manifest JSON file (offline)')
    stage_parser.add_argument(
        '--only',
        nargs='*',
        help='stage only these paths (default: all available)',
    )

    apply_parser = sub.add_parser('apply', help='apply staged files (cold start)')
    apply_parser.add_argument(
        '--force',
        action='store_true',
        help='allow applying while the app is running',
    )

    rollback_parser = sub.add_parser('rollback', help='restore previous versions')
    rollback_parser.add_argument(
        'rel',
        nargs='?',
        help='single file path, or none for the whole run',
    )
    return parser


def _load_local_manifest(path):
    '''Load a manifest from a local JSON file for offline use.

    Returns the parsed dict, an error STRING when loading fails, or None
    when no path was given (callers distinguish by type).
    '''
    if not path:
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except OSError as exc:
        return 'cannot read manifest file: %s' % exc
    except ValueError as exc:
        return 'invalid manifest JSON: %s' % exc


def main(argv=None):
    '''CLI entry point; prints the command report as JSON and returns 0 on
    success, 1 on failure (argparse exits 2 on usage errors).
    '''
    parser = _build_cli()
    args = parser.parse_args(argv)
    root = args.root
    if args.command == 'check':
        manifest = _load_local_manifest(getattr(args, 'manifest', None))
        report = _fail(manifest) if isinstance(manifest, str) else check_updates(
            root, manifest=manifest, channel=args.channel)
    elif args.command == 'stage':
        manifest = _load_local_manifest(getattr(args, 'manifest', None))
        report = _fail(manifest) if isinstance(manifest, str) else stage_updates(
            root,
            selection=args.only or None,
            manifest=manifest,
            channel=args.channel,
        )
    elif args.command == 'apply':
        report = apply_updates(root, force=args.force)
    else:
        report = rollback_updates(root, rel=args.rel)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
