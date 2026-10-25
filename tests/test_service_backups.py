"""Host backup safety tests, runnable without application dependencies."""
import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1] / 'deploy/backup'
sys.path.insert(0, str(DIRECTORY))
import service_backup as backup
import disk_guard as disk

RESTIC = os.environ.get('RESTIC_TEST_BINARY') or shutil.which('restic')
IMAGE = 'sha256:' + 'a' * 64


class Safety(unittest.TestCase):
    def test_only_the_empty_300g_whole_disk_is_accepted(self):
        record = {'type': 'disk', 'size': disk.EXPECTED_SIZE, 'fstype': None, 'mountpoints': [None]}
        disk.validate_inventory(record, [])
        for changes, signatures in [({'size': 200 * 1024**3}, []), ({'type': 'part'}, []),
            ({'fstype': 'ext4'}, []), ({'mountpoints': ['/']}, []),
            ({'children': [{'name': '/dev/sdb1'}]}, []), ({}, [{'type': 'gpt'}])]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                disk.validate_inventory({**record, **changes}, signatures)

    def test_pending_backups_are_never_restore_targets(self):
        rows = [{'id': '1', 'time': '2026-10-24', 'tags': [backup.TAG]},
                {'id': '2', 'time': '2026-10-25', 'tags': [backup.PENDING]}]
        self.assertEqual(backup.latest(rows)['id'], '1')
        with self.assertRaises(RuntimeError):
            backup.latest(rows[1:])

    def test_health_marks_stale_failed_unverified_and_low_space(self):
        self.assertEqual(backup.health({}), 'not_configured')
        status = {'last_backup_at': backup.now()}
        self.assertEqual(backup.health(status), 'unverified')
        status['last_restore_verified_at'] = backup.now()
        self.assertEqual(backup.health(status), 'ok')
        status['disk_used_percent'] = 90
        self.assertEqual(backup.health(status), 'low_space')
        status['last_backup_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        self.assertEqual(backup.health(status), 'stale')
        status['drill_error'] = 'failed'
        self.assertEqual(backup.health(status), 'failed')

    def test_immutable_image_ids_only(self):
        backup.valid_image(IMAGE)
        for value in ['postgres:16', '../archive', 'sha256:x']:
            with self.assertRaises(RuntimeError):
                backup.valid_image(value)

    def test_env_values_are_parsed_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'env'
            path.write_text('# comment\nKEY="has = spaces"\nINERT=$(false)\n')
            self.assertEqual(backup.read_env(path), {'KEY': 'has = spaces', 'INERT': '$(false)'})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / 'app'
        self.source.mkdir()
        self.mount = self.directory / 'backup'
        self.mount.mkdir()
        self.password = self.directory / 'password'
        self.password.write_text('test-repository-password\n')
        self.tool = backup.Backup({'mount': str(self.mount), 'app_root': str(self.source),
            'state_dir': str(self.directory / 'state'), 'password_file': str(self.password),
            'filesystem_uuid': 'uuid', 'origin_lv': '/dev/test/root', 'origin_uuid': 'origin-uuid',
            'volume_group': 'test', 'origin_name': 'root', 'restic': RESTIC or 'restic'})
        self.tool.state.mkdir()

    def test_inventory_includes_all_services_legacy_volumes_and_external_overrides(self):
        override = self.directory / 'capacity.override.yaml'
        override.write_text('services: {}')
        data = self.directory / 'minio'
        legacy = self.directory / 'legacy'
        data.mkdir(); legacy.mkdir()
        rows = [{'Image': IMAGE, 'Mounts': [], 'Config': {'Labels': {
            'com.docker.compose.service': name,
            'com.docker.compose.project.config_files': str(override)}}}
            for name in ('postgres', 'minio', 'redis', 'nginx', 'api-green', 'mail-worker')]
        rows[1]['Mounts'] = [{'Source': str(data), 'Destination': '/data', 'Type': 'volume'}]
        def command(args, **kwargs):
            if args[:2] == ['docker', 'ps']: return b'container'
            if args[:2] == ['docker', 'inspect']: return json.dumps(rows).encode()
            if args[:3] == ['docker', 'volume', 'ls']: return b'deploy_legacy unrelated'
            if args[:3] == ['docker', 'volume', 'inspect']:
                self.assertEqual(args[-1], 'deploy_legacy')
                return json.dumps([{'Mountpoint': str(legacy)}]).encode()
            if args[0] == 'findmnt': return b'/dev/test/root\n'
            raise AssertionError(args)
        with patch.object(backup, 'run', side_effect=command):
            services, roots = self.tool.inventory()
        self.assertEqual(len(services), 6)
        self.assertIn(str(data), roots)
        self.assertIn(str(legacy), roots)
        self.assertIn(str(override), roots)
        self.assertEqual(len(self.tool.runtime_inventory), 6)

    def test_a_service_on_another_filesystem_refuses_atomic_capture(self):
        rows = [{'Image': IMAGE, 'Mounts': [{'Source': str(self.source), 'Destination': '/data', 'Type': 'volume'}],
                 'Config': {'Labels': {'com.docker.compose.service': 'minio'}}}]
        def command(args, **kwargs):
            if args[:2] == ['docker', 'ps']: return b'container'
            if args[:2] == ['docker', 'inspect']: return json.dumps(rows).encode()
            if args[0] == 'findmnt': return b'/dev/another/filesystem\n'
            raise AssertionError(args)
        with patch.object(backup, 'run', side_effect=command), self.assertRaisesRegex(RuntimeError, 'share'):
            self.tool.inventory()

    def test_lock_serializes_backup_restore_and_deployment(self):
        lock = self.directory / 'lock'
        with backup.locked(lock), patch('common.time.monotonic', side_effect=[0, 2]), self.assertRaises(RuntimeError):
            with backup.locked(lock, timeout=1):
                self.fail('A second operation acquired the lock')

    def test_failed_drill_is_not_hidden_by_a_successful_backup(self):
        self.tool.status(drill_error='restored object missing', last_backup_at=backup.now())
        self.tool.status(backup_error=None, last_backup_at=backup.now())
        self.assertEqual(json.loads(self.tool.status_path.read_text())['health'], 'failed')
        self.tool.status(drill_error=None, last_restore_verified_at=backup.now())
        self.assertEqual(json.loads(self.tool.status_path.read_text())['health'], 'ok')
        self.assertEqual(self.tool.status_path.stat().st_mode & 0o777, 0o644)

    def test_absent_or_wrong_backup_disk_refuses_before_any_write(self):
        for rows in [[], [{'uuid': 'wrong'}]]:
            with patch.object(backup, 'run', return_value=json.dumps({'filesystems': rows}).encode()), \
                 self.assertRaisesRegex(RuntimeError, 'absent'):
                self.tool.guard()
        self.assertFalse(self.tool.repo.exists())

    def test_cleanup_refuses_foreign_snapshot_even_with_the_same_name(self):
        row = {'vg_name': 'test', 'lv_name': backup.SNAP_NAME, 'origin': 'root', 'lv_tags': 'foreign'}
        with patch.object(backup, 'lvs', side_effect=[[row], [{'lv_uuid': 'origin-uuid'}]]), \
             patch.object(backup, 'run') as run, self.assertRaisesRegex(RuntimeError, 'not owned'):
            self.tool.cleanup()
        run.assert_not_called()

    def test_cleanup_refuses_a_replaced_origin(self):
        row = {'vg_name': 'test', 'lv_name': backup.SNAP_NAME, 'origin': 'root', 'lv_tags': backup.LV_TAG}
        with patch.object(backup, 'lvs', side_effect=[[row], [{'lv_uuid': 'wrong'}]]), \
             patch.object(backup, 'run') as run, self.assertRaisesRegex(RuntimeError, 'identity'):
            self.tool.cleanup()
        run.assert_not_called()

    def test_snapshot_is_released_on_reader_failure(self):
        calls = []
        with patch.object(self.tool, 'cleanup', side_effect=lambda: calls.append('cleanup')), \
             patch.object(backup, 'run', side_effect=lambda command, **kw: calls.append(command)), \
             self.assertRaisesRegex(RuntimeError, 'reader failed'):
            with self.tool.frozen():
                raise RuntimeError('reader failed')
        self.assertEqual(calls[0], 'cleanup')
        self.assertEqual(calls[-1], 'cleanup')
        self.assertEqual(calls[1][0], 'lvcreate')
        self.assertIn(self.tool.origin, calls[1])
        self.assertEqual(calls[2][0], 'mount')
        self.assertIn('ro,nodev,nosuid,noexec', calls[2])

    def test_an_overflowed_snapshot_is_not_accepted(self):
        with patch.object(self.tool, 'cleanup'), patch.object(backup, 'run'), \
             patch.object(backup, 'lvs', return_value=[{'lv_attr': 'swi-a-s---', 'data_percent': '99'}]), \
             self.assertRaisesRegex(RuntimeError, 'filled up'):
            with self.tool.frozen():
                pass

    def test_maintenance_checks_before_retention_and_keeps_pinned_history(self):
        calls = []
        with patch.object(self.tool, 'guard'), patch.object(self.tool, 'snapshots', return_value=[{
            'id': '1', 'time': backup.now(), 'tags': [backup.TAG]}]), \
             patch.object(self.tool, 'restic', side_effect=lambda *args: calls.append(args)):
            self.tool.maintain()
        self.assertEqual(calls[0][0], 'check')
        self.assertEqual(calls[1][0], 'forget')
        self.assertIn('pinned', calls[1])
        self.assertIn('host', calls[1])  # Stable retention even when images/sources change.
        self.assertEqual(calls[-1], ('prune',))

    def test_a_corrupt_repository_is_not_pruned(self):
        with patch.object(self.tool, 'guard'), patch.object(self.tool, 'snapshots', return_value=[{
            'id': '1', 'time': backup.now(), 'tags': [backup.TAG]}]), \
             patch.object(self.tool, 'restic', side_effect=RuntimeError('corrupt')) as restic, self.assertRaises(RuntimeError):
            self.tool.maintain()
        self.assertEqual(restic.call_count, 1)

    def test_restore_mounts_cannot_escape_the_restore_directory(self):
        target = self.directory / 'scratch'
        env = target / 'snapshot/app/backend-v1/deploy/env'
        env.mkdir(parents=True)
        (env / 'db.env').write_text('POSTGRES_USER=test\nPOSTGRES_DB=test\n')
        (env / 'backend.env').touch(); (env / 'minio.env').touch()
        outside = target / 'snapshot/outside'
        outside.symlink_to(self.source, target_is_directory=True)
        manifest = {'snapshot_prefix': '/snapshot', 'app_root': '/app', 'services': {
            'postgres': {'image': IMAGE, 'mounts': [{'Source': '/outside', 'Destination': '/var/lib/postgresql/data'}]}}}
        with patch.object(backup, 'run') as run, self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            self.tool.verify_services(target, manifest)
        run.assert_not_called()


@unittest.skipUnless(RESTIC, 'Install restic for encrypted archive integration tests')
class Archive(Fixture):
    def prepare(self):
        self.tool.restic('init')
        self.services = {'postgres': {'image': IMAGE, 'mounts': [
            {'Source': '/data/postgres', 'Destination': '/var/lib/postgresql/data'}]}}
        self.database = self.tool.snap / 'data/postgres'
        (self.database / 'pg_wal').mkdir(parents=True)
        (self.database / 'pg_tblspc').mkdir()
        (self.database / 'PG_VERSION').write_text('16')
        (self.database / 'table').write_text('point-in-time database data')
        (self.database / 'pg_wal/wal').write_text('point-in-time WAL')
        private = self.tool.snap / self.source.relative_to('/') / 'backend.env'
        private.parent.mkdir(parents=True)
        private.write_text('PRIVATE_TOKEN=never-plaintext-in-repository\n')
        self.tool.images.mkdir(parents=True)
        image = self.tool.images / ('a' * 64 + '.tar')
        image.write_bytes(b'test image archive')
        self.images = {IMAGE: {'file': str(image), 'sha256': backup.file_hash(image)}}
        self.tool.runtime_inventory = [{'private': 'private-runtime-env'}]
        return private

    @contextlib.contextmanager
    def fake_capture(self):
        with patch.object(self.tool, 'guard'), patch.object(self.tool, 'inventory', return_value=(
            self.services, [str(self.source), '/data/postgres'])), patch.object(self.tool, 'cache_images', return_value=self.images), \
             patch.object(self.tool, 'frozen', return_value=contextlib.nullcontext()):
            yield

    def test_real_encrypted_backup_survives_original_data_deletion(self):
        private = self.prepare()
        with self.fake_capture():
            self.tool.backup(pin=True)
        snapshot = backup.latest(self.tool.snapshots())
        self.assertIn('pinned', snapshot['tags'])
        private.unlink(); (self.database / 'table').unlink()
        target = self.directory / 'restore'
        self.tool.restic('restore', snapshot['id'], '--target', str(target), '--verify')
        recovered = target / self.tool.snap.relative_to('/') / 'data/postgres'
        self.assertEqual((recovered / 'table').read_text(), 'point-in-time database data')
        self.assertEqual((recovered / 'pg_wal/wal').read_text(), 'point-in-time WAL')
        self.assertEqual((target / private.relative_to('/')).read_text(), 'PRIVATE_TOKEN=never-plaintext-in-repository\n')
        self.assertTrue((target / self.tool.catalog.relative_to('/') / 'runtime-inspect.json').is_file())
        for path in self.tool.repo.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'never-plaintext-in-repository', path.read_bytes())

    def test_missing_wal_and_external_tablespaces_cannot_be_promoted(self):
        self.prepare()
        shutil.rmtree(self.database / 'pg_wal')
        (self.database / 'pg_wal').symlink_to(self.source, target_is_directory=True)
        with self.fake_capture(), self.assertRaisesRegex(RuntimeError, 'External'):
            self.tool.backup()
        self.assertEqual(self.tool.snapshots(), [])

    def test_invalidated_snapshot_preserves_last_complete_backup(self):
        self.prepare()
        with self.fake_capture():
            self.tool.backup()
        old = backup.latest(self.tool.snapshots())['id']
        @contextlib.contextmanager
        def invalidated():
            yield
            raise RuntimeError('snapshot overflow')
        with self.fake_capture(), patch.object(self.tool, 'frozen', side_effect=invalidated), self.assertRaises(RuntimeError):
            self.tool.backup()
        self.assertEqual(backup.latest(self.tool.snapshots())['id'], old)
        all_rows = json.loads(self.tool.restic('snapshots', '--json'))
        self.assertTrue(any(backup.PENDING in row.get('tags', []) for row in all_rows))


class ObjectVerification(unittest.TestCase):
    def test_all_object_bytes_and_database_references_are_checked(self):
        import io
        import types
        seen = []
        class Response:
            def stream(self, size):
                seen.append('read'); yield b'abc'
            def close(self): pass
            def release_conn(self): pass
        class Client:
            def __init__(self, *args, **kwargs): pass
            def list_buckets(self): return [types.SimpleNamespace(name='zerone')]
            def list_objects(self, bucket, **kwargs):
                return [types.SimpleNamespace(object_name='input', size=3)]
            def get_object(self, *args): return Response()
        for references, fails in [(['input'], False), (['missing'], True)]:
            request = {'access': 'test', 'secret': 'secret', 'bucket': 'zerone', 'references': references}
            with patch.dict(sys.modules, {'minio': types.SimpleNamespace(Minio=Client),
                 'urllib3': types.SimpleNamespace(PoolManager=lambda **kw: None, Timeout=lambda **kw: None)}), \
                 patch('sys.stdin', io.StringIO(json.dumps(request))), patch('sys.stdout', io.StringIO()) as output:
                if fails:
                    with self.assertRaisesRegex(RuntimeError, 'missing object'):
                        exec(backup.OBJECT_DRILL, {})
                else:
                    exec(backup.OBJECT_DRILL, {})
                    self.assertEqual(json.loads(output.getvalue())['verified_object_bytes'], 3)
        self.assertEqual(seen, ['read', 'read'])


if __name__ == '__main__':
    unittest.main()
