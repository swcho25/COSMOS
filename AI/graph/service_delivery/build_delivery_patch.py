"""Preserve isolated service changes without touching the user's checkout.

Read a production Git commit, compare its product sources with the isolated
service-source directory, and emit a standard a/... b/... patch. Baseline bytes
and the original generated patch are retained in a unique artifact directory.
An independent nested Git repository checks and applies the patch; every result
must match the modified source bytes before the reviewable patch is published.
No root branch/index/commit, service data, dependency installation or deployment
is changed. Run again after integration-test fixes to regenerate the delivery.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
BASE = 'd2e482c043968865d4770010bd12f32481725f2a'
SCOPES = ('BackEnd', 'FrontEnd', 'docs/api-specification.md')
EXCLUDE_DIRS = frozenset({'build', 'node_modules', 'dist', '.gradle', '.git',
                          'frontend-verification', '.cache', 'coverage', 'test-results', 'playwright-report'})
EXCLUDE_SUFFIXES = ('.log', '.tmp', '.pyc', '.hprof', '.class', '.tsbuildinfo')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def included(name):
    path = Path(name)
    return not (set(path.parts) & EXCLUDE_DIRS) and not path.name.endswith(EXCLUDE_SUFFIXES) \
        and not path.name.startswith('frontend-verification')


def git(directory, *args):
    result = subprocess.run(['git', '-C', str(directory), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf8', errors='replace'))
    return result.stdout


def source_files(source):
    result = {}
    for scope in SCOPES:
        root = source / scope
        if not root.exists():
            continue
        if root.is_file():
            result[scope] = root.read_bytes()
            continue
        for directory, subdirs, names in os.walk(root):
            subdirs[:] = [n for n in subdirs if n not in EXCLUDE_DIRS]
            for name in names:
                path = Path(directory) / name
                relative = path.relative_to(source).as_posix()
                if included(relative):
                    require(not path.is_symlink(), 'Unexpected service-source symlink: ' + relative)
                    result[relative] = path.read_bytes()
    return result


def patch_for(name, before, after):
    require(not any(x in name for x in ('\n', '\r', '\t', '"', '\\')), 'Unsafe patch filename')
    require(b'\x00' not in (before or b'') and b'\x00' not in (after or b''),
            'Changed binary file needs an explicit binary-delivery workflow: ' + name)
    left = [] if before is None else before.decode('utf8').splitlines(keepends=True)
    right = [] if after is None else after.decode('utf8').splitlines(keepends=True)
    lines = [f'diff --git a/{name} b/{name}\n']
    if before is None:
        lines.append('new file mode 100644\n')
    if after is None:
        lines.append('deleted file mode 100644\n')
    for line in difflib.unified_diff(left, right, fromfile='/dev/null' if before is None else 'a/' + name,
                                     tofile='/dev/null' if after is None else 'b/' + name, n=3):
        lines.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
    return ''.join(lines).encode('utf8')


def build(source, destination, verification_root, base):
    source, destination, verification_root = map(lambda p: Path(p).resolve(),
                                                (source, destination, verification_root))
    require(destination.parent == HERE.resolve(), 'Delivery output must stay in service_delivery')
    require(verification_root.is_relative_to((REPO / 'AI/graph/artifacts').resolve()),
            'Verification must stay inside ignored AI artifacts')
    git(REPO, 'cat-file', '-e', base + '^{commit}')
    suffix = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    work = verification_root / suffix
    work.mkdir(parents=True)
    archive = work / 'production-baseline.zip'
    git(REPO, 'archive', '--format=zip', '--output=' + str(archive), base, *SCOPES)
    baseline, checker = work / 'baseline', work / 'apply-check'
    baseline.mkdir()
    checker.mkdir()
    original = {}
    with zipfile.ZipFile(archive) as stream:
        for info in stream.infolist():
            if info.is_dir() or not included(info.filename):
                continue
            path = (baseline / info.filename).resolve()
            require(path.is_relative_to(baseline.resolve()), 'Baseline archive path escapes artifact')
            data = stream.read(info)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            original[info.filename] = data
            copied = checker / info.filename
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_bytes(data)
    desired = source_files(source)
    changed = sorted(name for name in original.keys() | desired.keys() if original.get(name) != desired.get(name))
    require(changed, 'No isolated changes to deliver')
    require('BackEnd/src/main/resources/db/migration/V7__version_grounded_analysis.sql' in changed,
            'Expected versioning migration is missing from delivery')
    require('BackEnd/ANALYSIS_RUN_API.md' in changed, 'Expected analysis API contract is missing from delivery')
    patch = b''.join(patch_for(name, original.get(name), desired.get(name)) for name in changed)
    pending = work / 'grounded_20260919.patch'
    pending.write_bytes(patch)
    git(checker, 'init', '-q')
    git(checker, '-c', 'core.autocrlf=false', 'apply', '--check', '--whitespace=nowarn', str(pending))
    git(checker, '-c', 'core.autocrlf=false', 'apply', '--whitespace=nowarn', str(pending))
    actual = source_files(checker)
    require(actual == desired, 'Patch-applied bytes differ from isolated service source')
    require(source_files(source) == desired, 'Service source changed during patch creation; regenerate')
    files = [{'path': name, 'change': 'added' if name not in original else 'deleted' if name not in desired else 'modified',
              'before_sha256': sha(original[name]) if name in original else None,
              'after_sha256': sha(desired[name]) if name in desired else None}
             for name in changed]
    report = {'schema_version': 'grounded-service-delivery-patch-1', 'status': 'PASS',
              'production_base_commit': base, 'source_directory': str(source), 'scopes': SCOPES,
              'patch_file': destination.name, 'patch_sha256': sha(patch), 'patch_bytes': len(patch),
              'changed_files': files, 'baseline_file_count': len(original), 'result_file_count': len(desired),
              'verification_directory': str(work), 'baseline_archive_sha256': sha(archive.read_bytes()),
              'script_sha256': sha(Path(__file__).read_bytes()),
              'checks': {'git_apply_check': True, 'git_apply': True, 'all_result_bytes_equal_service_source': True,
                         'source_unchanged_during_build': True, 'root_checkout_not_changed': True},
              'deployment_status': 'not_asserted_by_patch_generation'}
    (work / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    destination.write_bytes(patch)
    destination.with_suffix('.manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print(json.dumps({'status': 'PASS', 'changed_files': len(files), 'patch_bytes': len(patch),
                      'patch_sha256': report['patch_sha256'], 'verification_directory': str(work)}, ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default=BASE)
    parser.add_argument('--source', type=Path, default=REPO / 'AI/graph/artifacts/service_db_grounded_20260919_v1/service-source')
    parser.add_argument('--output', type=Path, default=HERE / 'grounded_20260919.patch')
    parser.add_argument('--verification-root', type=Path,
                        default=REPO / 'AI/graph/artifacts/service_db_grounded_20260919_v1/delivery-verification')
    args = parser.parse_args()
    build(args.source, args.output, args.verification_root, args.base)
