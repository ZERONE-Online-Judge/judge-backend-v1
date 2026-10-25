#!/usr/bin/env python3
"""Consistent, encrypted ZOJ service backups and isolated restore drills."""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from common import atomic_json, file_hash, locked, now, read_env, run

TAG = 'zoj-services-complete-v1'
PENDING = 'zoj-services-pending-v1'
DEFAULT_CONFIG = Path('/etc/zoj-backup/config.json')
DEFAULT_STATE = Path('/var/lib/zoj-backup')
SNAP_NAME = 'zoj_service_backup'
LV_TAG = 'zoj_backup_owned'
DB_REFERENCES = '''SELECT DISTINCT key FROM (
 SELECT storage_key AS key FROM problem_assets
 UNION ALL SELECT input_storage_key FROM testcases
 UNION ALL SELECT output_storage_key FROM testcases
 UNION ALL SELECT json_array_elements_text(storage_keys) FROM editor_judge_runs
 UNION ALL SELECT json_array_elements_text(storage_keys) FROM verification_executions
) refs WHERE key IS NOT NULL AND key <> '' ORDER BY key;'''


def latest(snapshots):
    rows = [row for row in snapshots if TAG in row.get('tags', [])]
    if not rows:
        raise RuntimeError('No complete ZOJ service backup exists')
    return max(rows, key=lambda row: (row['time'], row['id']))


def health(status):
    if any(status.get(action + '_error') for action in ('backup', 'drill', 'maintain', 'cleanup')):
        return 'failed'
    stamp = status.get('last_backup_at')
    if not stamp:
        return 'not_configured'
    if (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(stamp)).total_seconds() > 2700:
        return 'stale'
    stamp = status.get('last_restore_verified_at')
    if not stamp or (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(stamp)).total_seconds() > 172800:
        return 'unverified'
    return 'low_space' if status.get('disk_used_percent', 0) >= 85 else 'ok'


def valid_image(value):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', value):
        raise RuntimeError('Expected an immutable Docker image ID')
    return value


def lvs(path=None):
    command = ['lvs', '--reportformat', 'json', '--units', 'b', '--nosuffix',
               '-o', 'vg_name,lv_name,lv_uuid,origin,lv_attr,data_percent,lv_tags']
    if path:
        command.append(str(path))
    return json.loads(run(command))['report'][0]['lv']


class Backup:
    def __init__(self, config):
        self.config = config
        self.mount = Path(config['mount'])
        self.root = Path(config.get('app_root', '/home/zoj/zerone-online-judge'))
        self.state = Path(config.get('state_dir', str(DEFAULT_STATE)))
        self.snap = self.state / 'snapshot'
        self.catalog = self.state / 'catalog'
        self.work = self.mount / 'work'
        self.images = self.work / 'images'
        self.repo = self.mount / 'restic'
        self.origin = config['origin_lv']
        self.snapshot_lv = f"/dev/{config['volume_group']}/{SNAP_NAME}"
        self.env = {**os.environ, 'RESTIC_REPOSITORY': str(self.repo),
                    'RESTIC_PASSWORD_FILE': config['password_file'],
                    'RESTIC_CACHE_DIR': str(self.work / 'cache'), 'GOMAXPROCS': '2'}
        self.status_path = self.state / 'status.json'

    def status(self, **fields):
        previous = json.loads(self.status_path.read_text()) if self.status_path.exists() else {}
        value = {**previous, **fields, 'updated_at': now()}
        value['health'] = health(value)
        atomic_json(self.status_path, value, 0o644)

    def restic(self, *args, timeout=3600):
        return run([self.config.get('restic', 'restic'), *args], env=self.env, timeout=timeout)

    def snapshots(self):
        return json.loads(self.restic('snapshots', '--tag', TAG, '--json'))

    def guard(self):
        if not self.mount.is_absolute() or self.mount.is_symlink() or self.mount == Path('/'):
            raise RuntimeError('Unsafe backup mount')
        rows = json.loads(run(['findmnt', '--json', '--mountpoint', str(self.mount), '-o', 'TARGET,UUID']))['filesystems']
        if len(rows) != 1 or rows[0].get('uuid') != self.config['filesystem_uuid']:
            raise RuntimeError('Backup disk is absent or its UUID differs')
        if self.mount.stat().st_dev == self.root.stat().st_dev:
            raise RuntimeError('Backup must be on a separate filesystem')
        for path in (self.state, self.snap, self.catalog, self.work, self.images, self.repo,
                     Path(self.config['password_file'])):
            if path.is_symlink():
                raise RuntimeError('Backup control paths must not be symlinks')
        if not (self.repo / 'config').is_file():
            raise RuntimeError('Restic repository is not initialized')
        rows = lvs(self.origin)
        if len(rows) != 1 or rows[0]['lv_uuid'].strip() != self.config['origin_uuid']:
            raise RuntimeError('Source LVM volume identity changed')
        if Path(run(['findmnt', '-n', '-o', 'SOURCE', '-T', str(self.root)]).decode().strip()).resolve() != Path(self.origin).resolve():
            raise RuntimeError('Application is no longer on the configured LVM volume')

    def inventory(self):
        ids = run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=deploy']).decode().split()
        if not ids:
            raise RuntimeError('No running ZOJ services found')
        rows = json.loads(run(['docker', 'inspect', *ids]))
        services = {}
        roots = {str(self.root)}
        compose_files = set()
        for row in rows:
            service = row['Config']['Labels']['com.docker.compose.service']
            compose_files.update(row['Config']['Labels'].get('com.docker.compose.project.config_files', '').split(','))
            if service in services:
                raise RuntimeError('Multiple containers for one service; retry after deployment')
            mounts = []
            for item in row['Mounts']:
                source = Path(item['Source'])
                if source == Path('/var/run/docker.sock'):
                    continue
                if source.is_symlink() or not source.exists():
                    raise RuntimeError('A service mount is missing or a symlink')
                fs = run(['findmnt', '-n', '-o', 'SOURCE', '-T', str(source)]).decode().strip()
                if Path(fs).resolve() != Path(self.origin).resolve():
                    raise RuntimeError('All service data must share the atomic snapshot volume')
                roots.add(str(source))
                mounts.append({key: item[key] for key in ('Type', 'Source', 'Destination')})
            services[service] = {'image': valid_image(row['Image']), 'mounts': mounts}
        for required in ('postgres', 'minio', 'redis', 'nginx'):
            if required not in services:
                raise RuntimeError(f'Required service {required} is not running')
        if not any(name in services for name in ('api-blue', 'api-green')):
            raise RuntimeError('No active API found')
        if any(not Path(path).is_file() for path in filter(None, compose_files)):
            raise RuntimeError('A running service Compose configuration is missing')
        # Include stopped project volumes too (for example legacy database dumps).
        volumes = [name for name in run(['docker', 'volume', 'ls', '-q']).decode().split() if name.startswith('deploy_')]
        if volumes:
            for row in json.loads(run(['docker', 'volume', 'inspect', *volumes])):
                path = Path(row['Mountpoint'])
                if path.is_symlink() or Path(run(['findmnt', '-n', '-o', 'SOURCE', '-T', str(path)]).decode().strip()).resolve() != Path(self.origin).resolve():
                    raise RuntimeError('A Docker volume is outside the snapshot volume')
                roots.add(str(path))
        for optional in [*self.config.get('system_paths', []), *filter(None, compose_files)]:
            if Path(optional).exists():
                if Path(run(['findmnt', '-n', '-o', 'SOURCE', '-T', optional]).decode().strip()).resolve() != Path(self.origin).resolve():
                    raise RuntimeError('A host configuration path is on another filesystem')
                roots.add(optional)
        self.runtime_inventory = rows
        return services, sorted(roots)

    def cache_images(self, services):
        self.images.mkdir(mode=0o700, parents=True, exist_ok=True)
        image_ids = {row['image'] for row in services.values()}
        # Also preserve the runner image used by the playground control service.
        for name in self.config.get('extra_images', []):
            image_ids.add(valid_image(run(['docker', 'image', 'inspect', '--format', '{{.Id}}', name]).decode().strip()))
        result = {}
        for image_id in sorted(image_ids):
            path = self.images / (image_id.split(':')[1] + '.tar')
            if not path.exists():
                temporary = path.with_suffix('.pending')
                try:
                    with temporary.open('wb') as target:
                        run(['docker', 'image', 'save', image_id], stdout=target)
                        target.flush(); os.fsync(target.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            result[image_id] = {'file': str(path), 'sha256': file_hash(path)}
        return result

    def cleanup(self):
        # Only the explicitly named, tagged snapshot of the expected origin can be removed.
        rows = [row for row in lvs() if row['vg_name'].strip() == self.config['volume_group']
                and row['lv_name'].strip() == SNAP_NAME]
        if rows:
            row = rows[0]
            origins = lvs(self.origin)
            if len(origins) != 1 or origins[0]['lv_uuid'].strip() != self.config['origin_uuid']:
                raise RuntimeError('Refusing cleanup after the source LVM identity changed')
            if row['origin'].strip() != self.config['origin_name'] or LV_TAG not in row['lv_tags'].strip().split(','):
                raise RuntimeError('Refusing to remove a snapshot not owned by ZOJ backup')
        if self.snap.is_mount():
            actual = Path(run(['findmnt', '-n', '--mountpoint', str(self.snap), '-o', 'SOURCE']).decode().strip()).resolve()
            if actual != Path(self.snapshot_lv).resolve() or not rows:
                raise RuntimeError('Refusing to unmount an unexpected filesystem')
            run(['umount', str(self.snap)])
        if rows:
            run(['lvremove', '--yes', self.snapshot_lv])
        ids = run(['docker', 'ps', '-aq', '--filter', 'label=zoj.restore-drill=true']).decode().split()
        if ids:
            for row in json.loads(run(['docker', 'inspect', *ids])):
                if not row['Name'].startswith('/zoj-restore-'):
                    raise RuntimeError('Refusing to remove an unexpected restore container')
                run(['docker', 'rm', '-f', row['Id']])
        # A killed restore may leave plaintext scratch files; only delete our own
        # private directories on the mounted, UUID-verified backup filesystem.
        if self.mount.is_mount():
            actual = run(['findmnt', '-n', '--mountpoint', str(self.mount), '-o', 'UUID']).decode().strip()
            if actual == self.config['filesystem_uuid']:
                for path in (self.work / 'drills').glob('drill-*'):
                    if path.is_symlink() or not path.is_dir():
                        raise RuntimeError('Unexpected restore scratch path')
                    shutil.rmtree(path)

    @contextlib.contextmanager
    def frozen(self):
        self.cleanup()
        self.snap.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            # LVM suspends/flushed I/O while establishing the atomic origin snapshot.
            # Do not manually fsfreeze the root filesystem around userspace lvcreate.
            run(['lvcreate', '--snapshot', '--size', '16G', '--name', SNAP_NAME,
                 '--addtag', LV_TAG, self.origin], timeout=120)
            run(['mount', '-o', 'ro,nodev,nosuid,noexec', self.snapshot_lv, str(self.snap)], timeout=120)
            yield
            rows = lvs(self.snapshot_lv)
            if not rows or rows[0]['lv_attr'][4] != 'a' or float(rows[0]['data_percent'] or '100') >= 90:
                raise RuntimeError('Temporary LVM snapshot filled up or became invalid')
        finally:
            self.cleanup()

    def backup(self, pin=False):
        self.guard()
        usage = shutil.disk_usage(self.mount)
        if usage.free < max(10 * 1024**3, usage.total // 20):
            raise RuntimeError('Backup disk has less than 5% or 10 GiB free')
        with locked(self.root / 'service-backup-deploy.lock'):
            services, roots = self.inventory()
            images = self.cache_images(services)
            with self.frozen():
                paths = [self.snap / Path(path).relative_to('/') for path in roots]
                if any(not path.exists() for path in paths):
                    raise RuntimeError('A required service source is absent from the snapshot')
                db = next(item for item in services['postgres']['mounts'] if item['Destination'] == '/var/lib/postgresql/data')
                data = self.snap / Path(db['Source']).relative_to('/')
                if not (data / 'PG_VERSION').is_file() or not (data / 'pg_wal').is_dir():
                    raise RuntimeError('Required PostgreSQL cluster files/WAL are missing')
                if (data / 'pg_wal').is_symlink() or any((data / 'pg_tblspc').iterdir()):
                    raise RuntimeError('External PostgreSQL WAL/tablespaces need an explicit backup design')
                self.catalog.mkdir(mode=0o700, parents=True, exist_ok=True)
                manifest = {'format': 'zoj-services-v1', 'captured_at': now(), 'services': services,
                            'images': images, 'sources': roots, 'snapshot_prefix': str(self.snap),
                            'app_root': str(self.root)}
                atomic_json(self.catalog / 'manifest.json', manifest)
                # Includes exact command/env/ports/labels for disaster recovery, encrypted
                # inside restic and never exposed in public status or the service journal.
                atomic_json(self.catalog / 'runtime-inspect.json', self.runtime_inventory)
                result = self.restic('backup', '--json', '--host', 'zoj-app', '--tag', PENDING,
                                     *map(str, paths), str(self.catalog), *[row['file'] for row in images.values()])
                summaries = [json.loads(line) for line in result.decode().splitlines() if line.strip()]
                summary = next((row for row in summaries if row.get('message_type') == 'summary'), {})
                snapshot_id = summary.get('snapshot_id', '')
                if not re.fullmatch(r'[a-f0-9]{8,64}', snapshot_id):
                    raise RuntimeError('Restic did not confirm a complete snapshot')
            # Promotion happens AFTER snapshot validity and cleanup, never for partial files.
            args = ['tag', '--remove', PENDING, '--add', TAG]
            if pin:
                args += ['--add', 'pinned']
            self.restic(*args, snapshot_id)
            snapshot = latest(self.snapshots())
        keep = {Path(row['file']).name for row in images.values()}
        for path in self.images.glob('*.tar'):
            if path.name not in keep:
                path.unlink()
        usage = shutil.disk_usage(self.mount)
        self.status(last_backup_at=now(), backup_error=None, cleanup_error=None, snapshot=snapshot['id'],
                    services=sorted(services), disk_used_percent=round(100 * (1 - usage.free / usage.total), 1))
        print(json.dumps({'snapshot': snapshot['short_id'], 'services': sorted(services)}))

    def maintain(self):
        self.guard()
        latest(self.snapshots())
        self.restic('check', f'--read-data-subset={dt.date.today().weekday() + 1}/7')
        self.restic('forget', '--tag', TAG, '--group-by', 'host', '--keep-within', '1d',
                    '--keep-hourly', '72', '--keep-daily', '30', '--keep-weekly', '12', '--keep-monthly', '12', '--keep-tag', 'pinned')
        self.restic('forget', '--tag', PENDING, '--keep-within', '7d')
        self.restic('prune')
        self.status(last_maintenance_at=now(), maintain_error=None)

    def drill(self):
        self.guard()
        snapshot = latest(self.snapshots())
        directory = self.work / 'drills'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(self.mount).free < 20 * 1024**3:
            raise RuntimeError('At least 20 GiB free is required for an isolated restore drill')
        with tempfile.TemporaryDirectory(prefix='drill-', dir=directory) as scratch:
            target = Path(scratch)
            self.restic('restore', snapshot['id'], '--target', str(target), '--verify')
            manifest = json.loads((target / self.catalog.relative_to('/') / 'manifest.json').read_text())
            if manifest['format'] != 'zoj-services-v1':
                raise RuntimeError('Unsupported backup format')
            for image_id, row in manifest['images'].items():
                valid_image(image_id)
                archive = target / Path(row['file']).relative_to('/')
                if file_hash(archive) != row['sha256']:
                    raise RuntimeError('A saved runtime image checksum differs')
                # Loading by ID archives cannot overwrite a mutable production tag.
                # Always exercise the saved archives, even if the local image exists.
                run(['docker', 'load', '-i', str(archive)])
                run(['docker', 'image', 'inspect', image_id])
            metrics = self.verify_services(target, manifest)
        self.status(last_restore_verified_at=now(), verified_snapshot=snapshot['id'], drill_error=None, **metrics)
        print(json.dumps({'verified_snapshot': snapshot['short_id'], **metrics}))

    def verify_services(self, target, manifest):
        services = manifest['services']
        base = target / Path(manifest['snapshot_prefix']).relative_to('/')
        env = base / Path(manifest['app_root']).relative_to('/') / 'backend-v1/deploy/env'
        database = read_env(env / 'db.env')
        backend = read_env(env / 'backend.env')
        credentials = read_env(env / 'minio.env')
        names = []

        def restored(service, destination):
            source = next(row['Source'] for row in services[service]['mounts'] if row['Destination'] == destination)
            path = base / Path(source).relative_to('/')
            # No original live database, storage volume, or socket can be mounted.
            if not path.resolve().is_relative_to(target.resolve()) or path.is_symlink() or not path.is_dir():
                raise RuntimeError('Unsafe restored service data path')
            return path

        def start(service, destination, command, envfile=None):
            name = 'zoj-restore-' + service + '-' + uuid.uuid4().hex[:12]
            names.append(name)
            args = ['docker', 'run', '-d', '--rm', '--network', 'none', '--memory', '1g', '--cpus', '1',
                    '--pids-limit', '256', '--name', name, '--label', 'zoj.restore-drill=true',
                    '--mount', f'type=bind,src={restored(service, destination)},dst={destination}']
            if envfile:
                args += ['--env-file', str(envfile)]
            run([*args, services[service]['image'], *command])
            return name

        def sql(db, statement):
            return run(['docker', 'exec', '-i', db, 'psql', '-XqAt', '-v', 'ON_ERROR_STOP=1',
                        '-U', database['POSTGRES_USER'], '-d', database['POSTGRES_DB']], input=statement.encode()).decode()

        try:
            (restored('postgres', '/var/lib/postgresql/data') / 'postmaster.pid').unlink(missing_ok=True)
            db = start('postgres', '/var/lib/postgresql/data', ['postgres'], env / 'db.env')
            for _ in range(90):
                if subprocess.run(['docker', 'exec', db, 'pg_isready', '-U', database['POSTGRES_USER']],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('Restored PostgreSQL did not recover')
            tables = sql(db, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;").splitlines()
            if not {'contests', 'problems', 'submissions', 'identity_accounts', 'testcases'} <= set(tables):
                raise RuntimeError('Restored database is missing required tables')
            counts = {}
            for table in tables:
                if not re.fullmatch(r'[a-z_0-9]+', table):
                    raise RuntimeError('Unexpected database table identifier')
                counts[table] = int(sql(db, f'SELECT count(*) FROM "{table}";').strip())
            # Read every table, not just startup/catalog/counts.
            with open(os.devnull, 'wb') as sink:
                run(['docker', 'exec', db, 'pg_dump', '-U', database['POSTGRES_USER'], '-d', database['POSTGRES_DB'], '-Fc'], stdout=sink)
            references = sql(db, DB_REFERENCES).splitlines()
            minio = start('minio', '/data', ['server', '/data'], env / 'minio.env')
            api = next(services[name]['image'] for name in ('api-blue', 'api-green') if name in services)
            request = {'access': credentials['MINIO_ROOT_USER'], 'secret': credentials['MINIO_ROOT_PASSWORD'],
                       'bucket': backend.get('OBJECT_STORAGE_BUCKET', 'zerone'), 'references': references}
            helper = 'zoj-restore-object-reader-' + uuid.uuid4().hex[:12]
            names.append(helper)
            object_metrics = json.loads(run(['docker', 'run', '--rm', '-i', '--name', helper,
                 '--label', 'zoj.restore-drill=true', '--network', 'container:' + minio,
                 '--memory', '512m', '--cpus', '1', '--pids-limit', '128', api, 'python', '-c', OBJECT_DRILL],
                 input=json.dumps(request).encode()))
            redis = start('redis', '/data', ['redis-server', '--appendonly', 'yes', '--dir', '/data'])
            for _ in range(60):
                result = subprocess.run(['docker', 'exec', redis, 'redis-cli', 'PING'], capture_output=True)
                if result.returncode == 0 and result.stdout.strip() == b'PONG':
                    break
                time.sleep(1)
            else:
                raise RuntimeError('Restored Redis did not recover its persisted state')
            key_count = int(run(['docker', 'exec', redis, 'redis-cli', '--raw', 'DBSIZE']).strip())
            return {'database_counts': counts, 'redis_keys': key_count, **object_metrics}
        finally:
            for name in reversed(names):
                # Removing a nonexistent container is harmless after an earlier startup failure.
                subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


OBJECT_DRILL = r'''
import hashlib, json, sys, time
from minio import Minio
from urllib3 import PoolManager, Timeout
request = json.load(sys.stdin)
client = Minio('127.0.0.1:9000', access_key=request['access'], secret_key=request['secret'], secure=False,
               http_client=PoolManager(timeout=Timeout(connect=3, read=30), retries=False))
for attempt in range(60):
    try:
        buckets = client.list_buckets(); break
    except Exception:
        if attempt == 59: raise RuntimeError('Restored object storage did not become ready') from None
        time.sleep(1)
seen = set(); total = 0; count = 0; digest = hashlib.sha256()
for bucket in sorted(buckets, key=lambda value: value.name):
    for obj in client.list_objects(bucket.name, recursive=True):
        response = client.get_object(bucket.name, obj.object_name)
        sha = hashlib.sha256(); size = 0
        try:
            for part in response.stream(1024 * 1024):
                sha.update(part); size += len(part)
        finally:
            response.close(); response.release_conn()
        if size != obj.size: raise RuntimeError('Restored object size differs')
        digest.update(json.dumps([bucket.name,obj.object_name,size,sha.hexdigest()], ensure_ascii=True).encode())
        if bucket.name == request['bucket']: seen.add(obj.object_name)
        total += size; count += 1
missing = set(request['references']) - seen
if missing: raise RuntimeError('Restored database references missing object files: ' + str(len(missing)))
print(json.dumps({'verified_objects':count, 'verified_object_bytes':total,
                  'verified_references':len(set(request['references'])), 'objects_digest':digest.hexdigest()}))
'''


def public_status():
    path = DEFAULT_STATE / 'status.json'
    value = json.loads(path.read_text()) if path.exists() else {'health': 'not_configured'}
    value['health'] = health(value)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value['health'] == 'ok' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup', 'drill', 'maintain', 'cleanup', 'status'))
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--pin', action='store_true')
    args = parser.parse_args()
    if args.action == 'status':
        return public_status()
    if os.geteuid() != 0:
        parser.error('Use sudo for backup operations; status is readable without sudo')
    tool = Backup(json.loads(args.config.read_text()))
    def interrupted(signum, frame):
        raise RuntimeError('Backup service was interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        with locked('/run/lock/zoj-backup.lock'):
            if args.action == 'backup':
                tool.backup(pin=args.pin)
            else:
                getattr(tool, args.action)()
    except Exception as error:
        tool.status(**{args.action + '_error': str(error)}, failed_action=args.action, failed_at=now())
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
