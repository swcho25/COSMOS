"""Prepared operator helper. Default is read-only planning; mutation requires --execute.

production-check applies the new JAR's backward-compatible V7 migration to cosmos
through a separate loopback-only container. deploy replaces backend/frontend only.
Neither action switches analysis data versions, rewrites existing public rows,
restarts PostgreSQL/Redis, or prunes images. Run only after parent approval.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

WORK = Path('/home/ubuntu/cosmos-grounded-service-20260919-v1')
STATE = Path('/opt/cosmos/releases')
OLD_TAG = 'd2e482c04396-53'
NEW_TAG = 'grounded-20260919-v1'
EXPECTED_RUN = 'lake-grounded-20260919-v1'
CHECK_CONTAINER = 'cosmos-grounded-production-check'
DOCKER = ['docker']
HTTP = build_opener(ProxyHandler({}))


def command(args, label, *, env=None, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    if result.returncode:
        # Do not print captured Docker/Compose output: resolved environment
        # errors can contain production credentials.
        raise RuntimeError(f'{label} failed (exit {result.returncode})')
    return result.stdout


def inspect(name):
    return json.loads(command(DOCKER + ['inspect', name], 'inspect'))[0]


def container_identity(name):
    item = inspect(name)
    return {'id': item['Id'], 'image_id': item['Image'], 'image': item['Config']['Image'],
            'started_at': item['State']['StartedAt']}


@contextmanager
def deployment_lock():
    # Same lock as the existing operator/Jenkins deploy.sh, not a second lock.
    descriptor = os.open(STATE / 'deploy.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def postgres_value(sql):
    environment = dict(item.split('=', 1) for item in inspect('cosmos-postgres-1')['Config']['Env'])
    return command(DOCKER + ['exec', 'cosmos-postgres-1', 'psql', '-U', environment.get('POSTGRES_USER', 'postgres'),
        '-d', 'cosmos', '-v', 'ON_ERROR_STOP=1', '-Atc', sql], 'database read').strip()


def prerequisites():
    clone = json.loads((WORK / 'runtime-review-result-final.json').read_text())
    assert clone['status'] == 'PASS' and clone['flyway_v7'] == '7:true', 'Clone validation not complete'
    current = (STATE / 'current').read_text().strip()
    assert current == OLD_TAG, 'Current release changed; review this deployment again'
    backend = inspect('cosmos-backend-1')
    for component in ('backend', 'frontend'):
        identity = container_identity('cosmos-' + component + '-1')
        assert identity == clone['production'][component] or (
            component == 'backend' and identity == {key: value for key, value in clone['production'][component].items() if key != 'compose'}
        ), 'Production container changed since clone verification'
        command(DOCKER + ['image', 'inspect', f'cosmos-{component}:{NEW_TAG}'], 'prepared image check')
        old_image = json.loads(command(DOCKER + ['image', 'inspect', f'cosmos-{component}:{OLD_TAG}'], 'rollback image check'))[0]
        assert old_image['Id'] == identity['image_id'], 'Rollback tag no longer matches the running image'
    labels = backend['Config']['Labels']
    assert labels['com.docker.compose.project'] == 'cosmos'
    source = Path(labels['com.docker.compose.project.working_dir']).resolve()
    assert source == STATE / OLD_TAG
    files = [Path(value).resolve() for value in labels['com.docker.compose.project.config_files'].split(',')]
    assert files == [source / 'compose.yml', source / 'rdb-loader.compose.yml'], 'Unexpected deployed Compose files'
    environment_files = labels['com.docker.compose.project.environment_file'].split(',')
    assert environment_files == ['/etc/cosmos/app.env'], 'Unexpected environment override'
    assert all(path.is_file() for path in files) and Path(environment_files[0]).is_file()
    return backend, files, environment_files[0]


def api_json(port, path):
    with HTTP.open(f'http://127.0.0.1:{port}{path}', timeout=4) as response:
        return json.load(response)


def wait_health(port, container, attempts=40):
    for _ in range(attempts):
        if not inspect(container)['State']['Running']:
            return False
        try:
            if api_json(port, '/actuator/health').get('status') == 'UP':
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def receipt(name, result):
    result['checked_at'] = datetime.now(timezone.utc).isoformat()
    path = WORK / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result), flush=True)


def production_check():
    with deployment_lock():
        backend, _, _ = prerequisites()
        before = {name: container_identity(name) for name in ('cosmos-backend-1', 'cosmos-frontend-1', 'cosmos-postgres-1', 'cosmos-redis-1')}
        existing = subprocess.run(DOCKER + ['inspect', CHECK_CONTAINER], capture_output=True)
        if existing.returncode == 0:
            raise RuntimeError('Production-check container already exists; no replacement performed')
        environment = dict(item.split('=', 1) for item in backend['Config']['Env'])
        url = environment['DB_URL']
        assert url.startswith('jdbc:postgresql://') and urlsplit(url[5:]).path == '/cosmos'
        assert environment['SERVER_PORT'] == '18081'
        assert not any('\n' in value or '\r' in value for value in environment.values())
        env_path = WORK / 'production-check.env'
        descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write('\n'.join(f'{key}={value}' for key, value in sorted(environment.items())) + '\n')
        container_id = command(DOCKER + ['create', '--name', CHECK_CONTAINER, '--restart=no',
            '--memory', '1536m',
            '--network', 'cosmos_app', '-p', '127.0.0.1:18083:18081', '--env-file', str(env_path),
            f'cosmos-backend:{NEW_TAG}'], 'production-check create').strip()
        command(DOCKER + ['network', 'connect', 'cosmos_data', CHECK_CONTAINER], 'production-check data network')
        command(DOCKER + ['start', CHECK_CONTAINER], 'production-check start')
        healthy = wait_health(18083, CHECK_CONTAINER)
        version = postgres_value("SELECT version || ':' || success::text FROM public.flyway_schema_history WHERE version = '7'")
        after = {name: container_identity(name) for name in before}
        result = {'status': 'PASS' if healthy and version == '7:true' and before == after else 'FAIL',
            'action': 'production-check', 'database': 'cosmos', 'flyway_v7': version,
            'health_up': healthy, 'container_id': container_id, 'host_port': '127.0.0.1:18083',
            'production_containers_unchanged': before == after,
            'active_run_changed_by_helper': False}
        receipt('production-check-result.json', result)
        if result['status'] != 'PASS':
            raise RuntimeError('Production-check validation failed; live application was not replaced')


def compose_args(files, environment_file):
    args = DOCKER + ['compose', '--env-file', environment_file, '-p', 'cosmos']
    for path in files:
        args += ['-f', str(path)]
    return args


def compose_environment(tag):
    environment = os.environ.copy()
    environment['IMAGE_TAG'] = tag
    return environment


def resolved_config(files, environment_file, tag):
    return json.loads(command(compose_args(files, environment_file) + ['config', '--format', 'json'],
        'Compose validation', env=compose_environment(tag)))


def atomic_text(path, text):
    temporary = path.with_name(path.name + '.grounded-next')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(text + '\n')
    os.replace(temporary, path)


def application_health():
    if not wait_health(18081, 'cosmos-backend-1'):
        return False
    try:
        with HTTP.open('http://127.0.0.1:18080/healthz', timeout=4) as response:
            if response.status != 200:
                return False
        with HTTP.open('http://127.0.0.1:18080/', timeout=4) as response:
            index = response.read().decode()
        asset = re.search(r'/assets/[^"\s]+\.(?:js|css)', index)
        if not asset:
            return False
        with HTTP.open('http://127.0.0.1:18080' + asset.group(), timeout=4) as response:
            return response.status == 200
    except Exception:
        return False


def deploy():
    with deployment_lock():
        _, old_files, environment_file = prerequisites()
        production_check_result = json.loads((WORK / 'production-check-result.json').read_text())
        assert production_check_result['status'] == 'PASS'
        assert postgres_value('SELECT run_id FROM cosmos_analysis.active_run WHERE singleton') == EXPECTED_RUN, 'Validated analysis run is not selected'
        previous_infrastructure = {name: container_identity(name) for name in ('cosmos-postgres-1', 'cosmos-redis-1')}
        new_release = STATE / NEW_TAG
        if new_release.exists():
            raise RuntimeError('New release directory already exists; inspect before retrying')
        new_release.mkdir(mode=0o750)
        new_files = []
        for source in old_files:
            destination = new_release / source.name
            shutil.copy2(source, destination)
            assert destination.read_bytes() == source.read_bytes()
            new_files.append(destination)
        old_config = resolved_config(old_files, environment_file, OLD_TAG)
        new_config = resolved_config(new_files, environment_file, NEW_TAG)
        for component in ('backend', 'frontend'):
            assert old_config['services'][component]['image'] == f'cosmos-{component}:{OLD_TAG}'
            assert new_config['services'][component]['image'] == f'cosmos-{component}:{NEW_TAG}'
            new_config['services'][component]['image'] = old_config['services'][component]['image']
        assert new_config == old_config, 'Resolved Compose changed beyond the two application image tags'
        try:
            command(compose_args(new_files, environment_file) + ['up', '-d', '--no-deps', 'backend', 'frontend'],
                'application replacement', env=compose_environment(NEW_TAG), timeout=180)
            assert application_health(), 'Application health check failed'
            assert api_json(18081, '/api/analysis-runs/active')['runId'] == EXPECTED_RUN, 'Unexpected active analysis response'
            for component in ('backend', 'frontend'):
                assert inspect('cosmos-' + component + '-1')['Config']['Image'] == f'cosmos-{component}:{NEW_TAG}'
            assert {name: container_identity(name) for name in previous_infrastructure} == previous_infrastructure
            atomic_text(STATE / 'current', NEW_TAG)
            atomic_text(STATE / '.previous', OLD_TAG)
            receipt('application-deployment-result.json', {'status':'PASS', 'action':'deploy', 'current':NEW_TAG,
                'previous':OLD_TAG, 'run_id':EXPECTED_RUN, 'postgres_redis_unchanged':True, 'images_pruned':False})
        except Exception as failure:
            rollback_ok = False
            try:
                command(compose_args(old_files, environment_file) + ['up', '-d', '--no-deps', 'backend', 'frontend'],
                    'application image rollback', env=compose_environment(OLD_TAG), timeout=180)
                rollback_ok = application_health()
                if rollback_ok:
                    atomic_text(STATE / 'current', OLD_TAG)
            finally:
                receipt('application-deployment-failure.json', {'status':'FAIL', 'action':'deploy',
                    'failure_type':type(failure).__name__, 'old_images_restored_and_healthy':rollback_ok,
                    'database_migration_reversed':False, 'active_run_changed_by_helper':False, 'images_pruned':False})
            raise RuntimeError('Application deployment failed; consult sanitized rollback receipt') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=('plan', 'production-check', 'deploy'), default='plan')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Use sudo -n python3; root is needed for the existing deployment lock/environment file')
    if args.action == 'plan':
        _, files, environment_file = prerequisites()
        print(json.dumps({'action':'plan', 'mutation':False, 'old_tag':OLD_TAG, 'new_tag':NEW_TAG,
            'compose_files':[str(path) for path in files], 'environment_file':environment_file,
            'deployment_lock':str(STATE / 'deploy.lock'), 'planned_services':['backend','frontend'],
            'postgres_redis_restart':False, 'image_prune':False}))
        return
    if not args.execute:
        raise RuntimeError('Mutation requires explicit --execute after parent authorization')
    {'production-check':production_check, 'deploy':deploy}[args.action]()


if __name__ == '__main__':
    main()
