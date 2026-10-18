import gzip
import hashlib
import hmac
import base64
import io
import json
import time
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import zipfile
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.main import app
from app.models import now_utc
from app.orm_models import EditorJudgeRunRow as Run, IdentityAccountRow, JudgeJobRow, JudgeNodeRow, SubmissionRow
from app.orm_models import ContestRow, ContestDivisionRow, ProblemRow
from app.routers import editor_judging as routes
from app.services import editor_judging as judging, editor_bridge
from app.services.storage import object_storage
from app.services.errors import AppError
from app.services.store import store
from app.settings import settings

client = TestClient(app)
KEY = "editor-judge-bridge-test-key-over-32-characters"
SECRET = "editor-judge-node-test-secret-over-32-characters"
EMAIL = "editor-author@example.com"


@pytest.fixture
def database(tmp_path, monkeypatch):
    url = os.environ.get("EDITOR_JUDGE_TEST_DATABASE_URL")
    if url and not url.endswith("/editor_judge_test"):
        raise RuntimeError("Use a disposable editor_judge_test database")
    engine = create_engine(url or "sqlite:///" + str(tmp_path / "judge.db"), **({} if url else {"connect_args": {"check_same_thread": False}}))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (judging, routes, editor_bridge):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(store, "_session", sessions)
    monkeypatch.setattr(settings, "editor_bridge_secret", SecretStr(KEY))
    monkeypatch.setattr(settings, "local_object_storage_root", str(tmp_path / "objects"))
    monkeypatch.setattr(object_storage, "backend", "local")
    with sessions() as db:
        db.add_all([IdentityAccountRow(email=EMAIL, display_name="Author"), IdentityAccountRow(email="other@example.com", display_name="Other")])
        db.commit()
    yield sessions
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def short_lock_timeout(database):
    engine = database.kw['bind']
    if engine.dialect.name != 'postgresql':
        pytest.skip('The upload lock regression requires real PostgreSQL locks')
    def configure(connection, _):
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '200ms'")
        connection.commit()
    engine.dispose()
    event.listen(engine, 'connect', configure)
    yield database
    event.remove(engine, 'connect', configure)


def create_direct(body):
    return judging.create(EMAIL, io.BytesIO(body), hashlib.sha256(body).hexdigest())


def test_twelve_solutions_register_while_one_upload_is_slow(short_lock_timeout, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    write = object_storage.write_bytes
    def slow_first(key, *args, **kwargs):
        with lock:
            first = not entered.is_set()
            if first:
                entered.set()
        if first:
            assert release.wait(10), 'Test did not release the paused upload'
        return write(key, *args, **kwargs)
    monkeypatch.setattr(object_storage, 'write_bytes', slow_first)
    node_id = node()
    project = uuid4().hex
    def body():
        return snapshot(change=lambda manifest, files: manifest.update(project_id=project))
    with ThreadPoolExecutor(max_workers=12) as pool:
        first = pool.submit(create_direct, body())
        try:
            assert entered.wait(5)
            assert judging.claim_jobs(node_id, SECRET, 1) == []
            others = [pool.submit(create_direct, body()) for _ in range(11)]
            results = [future.result(timeout=5) for future in others]
            assert all(row['status'] == 'waiting' for row in results)
            with short_lock_timeout() as db:
                assert db.scalar(select(func.count()).select_from(Run).where(Run.status == 'uploading')) == 1
        finally:
            release.set()
        results.append(first.result(timeout=5))
    assert len({row['id'] for row in results}) == 12
    assert all(row['status'] == 'waiting' for row in results)
    with short_lock_timeout() as db:
        rows = db.scalars(select(Run)).all()
        assert len({row.dataset_key for row in rows}) == 1
        assert all(object_storage.size_bytes(key) is not None for row in rows for key in row.storage_keys)


def test_storage_failure_resumes_same_reservation_and_keeps_shared_objects(database, monkeypatch):
    body = snapshot()
    write = object_storage.write_bytes
    calls = []
    def fail_after_bundle(key, *args, **kwargs):
        calls.append(key)
        if len(calls) == 2:
            raise OSError('temporary storage outage')
        return write(key, *args, **kwargs)
    monkeypatch.setattr(object_storage, 'write_bytes', fail_after_bundle)
    with pytest.raises(AppError) as failure:
        create_direct(body)
    assert failure.value.status_code == 503
    with database() as db:
        row = db.scalar(select(Run))
        assert row.status == 'uploading'
        run_id, bundle = row.run_id, row.payload['bundle_key']
    assert object_storage.size_bytes(bundle) is not None
    monkeypatch.setattr(object_storage, 'write_bytes', write)
    result = create_direct(body)
    assert result['id'] == run_id and result['status'] == 'waiting'
    assert create_direct(body)['id'] == run_id
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        row = db.get(Run, run_id)
        assert all(object_storage.size_bytes(key) is not None for key in row.storage_keys)


def test_upload_reservations_count_towards_personal_quota(database, monkeypatch):
    with database() as db:
        account = db.scalar(select(IdentityAccountRow).where(IdentityAccountRow.email == EMAIL))
        for _ in range(32):
            db.add(Run(operation_key=uuid4().hex, owner_id=account.account_id, project_id='a' * 32,
                       fingerprint='b' * 64, payload={}, storage_keys=[], testcase_count=1, status='uploading'))
        db.commit()
    monkeypatch.setattr(object_storage, 'write_bytes', lambda *args, **kwargs: pytest.fail('Quota must precede storage'))
    with pytest.raises(AppError) as failure:
        create_direct(snapshot())
    assert failure.value.status_code == 429


def test_cancel_during_upload_never_requeues_it(database, monkeypatch):
    write = object_storage.write_bytes
    def cancel_then_write(key, *args, **kwargs):
        with database() as db:
            run_id = db.scalar(select(Run.run_id))
        judging.get_run(EMAIL, run_id, cancel=True)
        return write(key, *args, **kwargs)
    monkeypatch.setattr(object_storage, 'write_bytes', cancel_then_write)
    assert create_direct(snapshot())['status'] == 'cancelled'


def test_cancel_operation_prevents_late_or_lost_upload(database):
    operation = uuid4().hex
    path = '/api/editor/judge/run-operations/' + operation
    cancelled = client.delete(path, headers=headers('DELETE', path)).json()['data']
    result = create_direct(snapshot(operation=operation))
    assert result['id'] == cancelled['id'] and result['status'] == 'cancelled'
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1


def test_concurrent_claims_assign_each_run_once(database):
    if database.kw['bind'].dialect.name != 'postgresql':
        pytest.skip('Row locking requires the disposable PostgreSQL check')
    expected = {submit(snapshot()).json()['data']['id'] for _ in range(8)}
    credentials = []
    for i in range(8):
        secret = SECRET + str(i)
        row = store.provision_node('editor-concurrent-' + str(i), secret, 1)
        store.register_node(row.node_name, secret, 1, '0.2.21')
        credentials.append((row.judge_node_id, secret))
    def take(credential):
        return judging.claim_jobs(*credential, 1, session_factory=database)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [job for batch in pool.map(take, credentials) for job in batch]
    assert len(jobs) == 8
    assert {job['judge_job_id'] for job in jobs} == expected
    assert len({job['lease_token'] for job in jobs}) == 8


def headers(method, path, body=b"", email=EMAIL):
    encoded = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    stamp, nonce = str(int(time.time())), uuid4().hex
    digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(["zoj-editor-v1", method, path, stamp, nonce, encoded, digest])
    return {"X-Editor-Identity": encoded, "X-Editor-Timestamp": stamp, "X-Editor-Nonce": nonce,
            "X-Editor-Content-Sha256": digest, "X-Editor-Signature": hmac.new(KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest(), "Content-Type": "application/zip"}


def snapshot(operation=None, change=None):
    files = {}
    def blob(value):
        content = value.encode()
        sha = hashlib.sha256(content).hexdigest()
        files["blobs/" + sha] = content
        return sha
    source = blob('int main(){return 0;}')
    checker = blob('int main(){return 0;}')
    manifest = {"format": "zoj-editor-judge-v1", "operation_id": operation or uuid4().hex,
                "project_id": uuid4().hex, "context_hash": "a" * 64,
                "source": {"name": "main.cpp", "language": "cpp17", "sha256": source},
                "time_limit_ms": 1000, "memory_limit_mb": 128,
                "resources": [{"name": "checker.cpp", "role": "checker", "sha256": checker}],
                "testcases": [{"index": 2, "input_sha": blob("1 2\n"), "answer_sha": blob("3\n")},
                              {"index": 7, "input_sha": blob("2 3\n"), "answer_sha": blob("5\n")}]}
    if change:
        change(manifest, files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for name, value in files.items():
            z.writestr(name, value)
    return output.getvalue()


def submit(body):
    path = "/api/editor/judge/runs"
    return client.post(path, content=body, headers=headers("POST", path, body))


def node():
    row = store.provision_node("editor-test-node", SECRET, 2)
    store.register_node(row.node_name, SECRET, 2, "0.2.21")
    return row.judge_node_id


def claim(node_id, count=1):
    return client.post(f"/api/internal/judge/nodes/{node_id}/assignments:claim", json={"node_secret": SECRET, "max_count": count}).json()["data"]["jobs"]


def report(run_id, lease, **changes):
    values = {"node_secret": SECRET, "lease_token": lease, "final_status": "accepted", "runtime_ms": 17, "memory_kb": 1024}
    values.update(changes)
    return client.post(f"/api/internal/judge/jobs/{run_id}/result", json=values)


def full_run():
    parent = submit(snapshot(change=lambda manifest, files: manifest.update(collect_all_results=True))).json()['data']['id']
    node_id = node()
    assert claim(node_id) == []  # Old agents must not silently stop at a failure.
    registered = client.post('/api/internal/judge/nodes/register', json={
        'node_name': 'editor-test-node', 'node_secret': SECRET, 'total_slots': 2, 'agent_version': '0.2.23',
        'execution_capabilities': [judging.FULL_VERIFICATION], 'execution_revision': judging.FULL_VERIFICATION})
    assert registered.status_code == 200, registered.text
    job = claim(node_id)[0]
    assert job['collect_all_results'] and job['submission']['submission_kind'] == 'editor_verification'
    return parent, job


def case_result(index, status='accepted', output=b'answer\n'):
    return {'index': index, 'final_status': status, 'runtime_ms': index, 'memory_kb': index * 10,
            'judge_message': '', 'output_base64': base64.b64encode(output).decode(),
            'output_total_bytes': len(output), 'output_truncated': False}


def progress_case(parent, job, case, count=1):
    return client.post(f'/api/internal/judge/jobs/{parent}/progress', json={
        'node_secret': SECRET, 'lease_token': job['lease_token'], 'status': 'judging',
        'progress_current': count, 'progress_total': 2, 'testcase_result': case})


def test_full_suite_reports_live_results_and_downloadable_binary_output_with_owner_acl(database):
    parent, job = full_run()
    bad = case_result(2, 'wrong_answer', b'\x00\xff\r\n')
    good = case_result(7, output=b'')
    response = progress_case(parent, job, bad)
    assert response.status_code == 200, response.text
    path = f'/api/editor/judge/runs/{parent}'
    live = client.get(path, headers=headers('GET', path)).json()['data']
    assert live['status'] == 'judging' and live['progress_current'] == 1
    saved = live['result']['testcase_results'][0]
    assert saved['final_status'] == 'wrong_answer' and 'output_base64' not in saved
    with database() as db:
        row = db.get(Run, parent)
        assert saved['output_sha256'] == hashlib.sha256(b'\x00\xff\r\n').hexdigest()
        assert all('output_base64' not in case for case in row.result['testcase_results'])
        assert object_storage.read_bytes(judging.output_key(row, 2, saved['output_sha256'])) == b'\x00\xff\r\n'
    assert progress_case(parent, job, bad).status_code == 200
    assert progress_case(parent, job, good, 2).status_code == 200
    values = {'final_status': 'wrong_answer', 'failed_testcase_order': 2, 'testcase_results': [bad, good]}
    final = report(parent, job['lease_token'], **values)
    assert final.status_code == 200, final.text
    assert report(parent, job['lease_token'], **values).status_code == 200
    result = final.json()['data']['editor_run']['result']
    assert result['full_suite_complete']
    assert [case['final_status'] for case in result['testcase_results']] == ['wrong_answer', 'accepted']
    output_path = path + '/testcases/2/output'
    data = client.get(output_path, headers=headers('GET', output_path)).json()['data']
    assert base64.b64decode(data['base64']) == b'\x00\xff\r\n' and data['size'] == 4 and not data['truncated']
    assert client.get(output_path, headers=headers('GET', output_path, email='other@example.com')).status_code == 404
    assert client.get(output_path).status_code == 401
    remaining = path + '/remaining?operation_id=' + uuid4().hex
    assert client.post(remaining, headers=headers('POST', remaining)).status_code == 409


def test_full_suite_rejects_missing_duplicate_foreign_or_contradictory_results(database):
    parent, job = full_run()
    for cases in [[case_result(2)], [case_result(2), case_result(2)], [case_result(2), case_result(99)]]:
        assert report(parent, job['lease_token'], testcase_results=cases).status_code == 409
    assert report(parent, job['lease_token'], final_status='wrong_answer', failed_testcase_order=2,
                  testcase_results=[case_result(2), case_result(7)]).status_code == 409
    with database() as db:
        assert db.get(Run, parent).status in judging.RUNNING
    assert report(parent, job['lease_token'], testcase_results=[case_result(2), case_result(7)]).status_code == 200


def test_compile_error_keeps_unexecuted_tests_unknown_and_retries_do_not_replace_outputs(database):
    parent, job = full_run()
    assert report(parent, job['lease_token'], final_status='compile_error', compile_message='compile failed', testcase_results=[]).status_code == 200
    path = f'/api/editor/judge/runs/{parent}'
    result = client.get(path, headers=headers('GET', path)).json()['data']
    assert result['progress_current'] == 0
    assert result['result']['testcase_results'] == [] and not result['result']['full_suite_complete']


def test_output_validation_prevents_silent_truncation_changed_evidence_and_budget_amplification(database, monkeypatch):
    parent, job = full_run()
    bad = case_result(2, output=b'1234')
    bad['output_total_bytes'] = 8
    assert progress_case(parent, job, bad).status_code == 409
    bad['output_truncated'] = True
    assert progress_case(parent, job, bad).status_code == 200
    assert progress_case(parent, job, case_result(2, output=b'changed')).status_code == 409
    monkeypatch.setattr(judging, 'MAX_RUN_OUTPUT_BYTES', 5)
    assert progress_case(parent, job, case_result(7, output=b'1234')).status_code == 409
    with database() as db:
        result = db.get(Run, parent).result['testcase_results']
        assert len(result) == 1 and result[0]['output_size'] == 4 and result[0]['output_truncated']


def test_batched_case_progress_persists_each_output_without_completing_the_run(database):
    parent, job = full_run()
    cases = [case_result(2, 'wrong_answer'), case_result(7)]
    response = client.post(f'/api/internal/judge/jobs/{parent}/progress', json={
        'node_secret': SECRET, 'lease_token': job['lease_token'], 'status': 'judging',
        'progress_current': 2, 'progress_total': 2, 'testcase_results': cases})
    assert response.status_code == 200, response.text
    run = response.json()['data']['editor_run']
    assert run['status'] == 'judging' and run['progress_current'] == 2
    assert len(run['result']['testcase_results']) == 2 and not run['result']['full_suite_complete']
    assert all(case['output_available'] for case in run['result']['testcase_results'])


def test_signed_snapshot_retry_acl_and_no_contest_submission(database):
    body = snapshot()
    r = submit(body)
    assert r.status_code == 202, r.text
    run_id = r.json()["data"]["id"]
    assert submit(body).json()["data"]["id"] == run_id
    path = "/api/editor/judge/runs/" + run_id
    assert client.get(path, headers=headers("GET", path, email="other@example.com")).status_code == 404
    h = headers("GET", path)
    assert client.get(path, headers=h).status_code == 200
    assert client.get(path, headers=h).status_code == 401
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        assert db.scalar(select(func.count()).select_from(SubmissionRow)) == 0
        assert db.scalar(select(func.count()).select_from(JudgeJobRow)) == 0


def test_invalid_zip_paths_hashes_and_languages_are_rejected(database):
    for change in (
        lambda m, f: f.update({"../escape": b"no"}),
        lambda m, f: f.update({"blobs/" + m["source"]["sha256"]: b"corrupted"}),
        lambda m, f: m["source"].update(language="java17"),
        lambda m, f: m["resources"][0].update(name="../checker.cpp"),
        lambda m, f: m["testcases"][1].update(index=2),
    ):
        assert submit(snapshot(change=change)).status_code == 422
    body = snapshot()
    path = "/api/editor/judge/runs"
    assert client.post(path, content=body, headers=headers("POST", path, b"other")).status_code == 401
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 0


def test_changed_idempotent_request_cannot_overwrite_snapshot(database):
    body = snapshot()
    created = submit(body).json()["data"]
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        manifest = json.loads(z.read("manifest.json"))
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as changed:
            manifest["time_limit_ms"] = 2000
            changed.writestr("manifest.json", json.dumps(manifest))
            for filename in z.namelist():
                if filename != "manifest.json":
                    changed.writestr(filename, z.read(filename))
    assert submit(output.getvalue()).status_code == 409
    with database() as db:
        row = db.get(Run, created["id"])
        assert row.payload["time_limit_ms"] == 1000
        assert json.loads(gzip.decompress(object_storage.read_bytes(row.payload["bundle_key"])))['testcases'][1]['output_text'] == '5\n'


def test_existing_contest_work_wins_claim_and_real_protocol_reports(database):
    run_id = submit(snapshot()).json()["data"]["id"]
    node_id = node()
    with database() as db:
        db.add(ContestRow(contest_id='contest', title='Test', organization_name='Test', overview='', status='running', start_at=now_utc(), end_at=now_utc() + timedelta(hours=1), freeze_at=now_utc()))
        db.flush()
        db.add(ContestDivisionRow(division_id='division', contest_id='contest', code='test', name='Test'))
        db.flush()
        db.add(ProblemRow(problem_id='problem', contest_id='contest', division_id='division', problem_code='A', title='Test', statement='', time_limit_ms=1000, memory_limit_mb=128, display_order=1))
        db.flush()
        db.add(SubmissionRow(submission_id="contest-submission", contest_id="contest", division_id="division", problem_id="problem", submission_kind="participant", language="cpp17", source_code="int main(){}", status="waiting"))
        db.flush()
        db.add(JudgeJobRow(judge_job_id="contest-job", submission_id="contest-submission", contest_id="contest", division_id="division", status="pending", queue_position=1))
        db.commit()
    assert claim(node_id)[0]["judge_job_id"] == "contest-job"
    job = claim(node_id)[0]
    assert job["judge_job_id"] == run_id
    assert job["submission"]["submission_kind"] == "editor_verification"
    assert job["submission"]["language"] == "cpp17"
    lease = job["lease_token"]
    assert report(run_id, "wrong-lease").status_code == 409
    assert report(run_id, lease, node_secret="wrong-secret").status_code == 403
    p = client.post(f"/api/internal/judge/jobs/{run_id}/progress", json={"node_secret": SECRET, "lease_token": lease, "status": "judging", "progress_current": 1, "progress_total": 2})
    assert p.status_code == 200, p.text
    r = report(run_id, lease)
    assert r.status_code == 200, r.text
    assert report(run_id, lease).status_code == 200
    with database() as db:
        row = db.get(Run, run_id)
        assert row.result["runtime_ms"] == 17 and row.progress_current == 2
        assert row.node_name == "editor-test-node" and row.agent_version == "0.2.21"
        assert db.scalar(select(func.count()).select_from(SubmissionRow)) == 1


def test_cancel_and_expired_lease_reject_old_results(database):
    run_id = submit(snapshot()).json()["data"]["id"]
    node_id = node()
    original = claim(node_id)[0]["lease_token"]
    with database() as db:
        db.get(Run, run_id).leased_at = now_utc() - timedelta(seconds=settings.judge_lease_timeout_seconds + 1)
        db.commit()
    current = claim(node_id)[0]["lease_token"]
    assert current != original
    assert report(run_id, original).status_code == 409
    path = "/api/editor/judge/runs/" + run_id
    assert client.delete(path, headers=headers("DELETE", path)).json()["data"]["status"] == "cancelled"
    assert report(run_id, current).status_code == 409


def test_disabled_node_and_admission_limit(database):
    run_id = submit(snapshot()).json()["data"]["id"]
    node_id = node()
    lease = claim(node_id)[0]["lease_token"]
    with database() as db:
        db.get(JudgeNodeRow, node_id).schedulable = False
        owner_id = db.get(Run, run_id).owner_id
        for _ in range(31):
            db.add(Run(operation_key=uuid4().hex, owner_id=owner_id, project_id=uuid4().hex,
                       fingerprint="0" * 64, payload={}, storage_keys=[], testcase_count=1))
        db.commit()
    assert report(run_id, lease).status_code == 403
    assert submit(snapshot()).status_code == 429


def test_remaining_tests_reuse_snapshot_and_keep_fallback_data(database):
    parent = submit(snapshot()).json()["data"]["id"]
    node_id = node()
    job = claim(node_id)[0]
    assert len(job["testcases"]) == 2
    assert all(c["input_url"] and c["output_url"] for c in job["testcases"])
    assert report(parent, job["lease_token"], final_status="wrong_answer", failed_testcase_order=2).status_code == 200
    path = f"/api/editor/judge/runs/{parent}/remaining?operation_id={uuid4().hex}"
    first = client.post(path, headers=headers("POST", path))
    assert first.status_code == 202, first.text
    child = first.json()["data"]["id"]
    assert client.post(path, headers=headers("POST", path)).json()["data"]["id"] == child
    job = claim(node_id)[0]
    assert job["judge_job_id"] == child
    assert [c["display_order"] for c in job["testcases"]] == [7]
    with database() as db:
        original = db.get(Run, parent)
        variant = db.get(Run, child)
        assert original.dataset_key == variant.dataset_key
        assert len(json.loads(gzip.decompress(object_storage.read_bytes(variant.payload["bundle_key"])))['testcases']) == 1
        assert variant.payload["resources"] == original.payload["resources"]
        variant_key = variant.payload["bundle_key"]
        original_key = original.payload["bundle_key"]
    assert report(child, job["lease_token"]).status_code == 200
    assert object_storage.size_bytes(variant_key) is None
    assert object_storage.size_bytes(original_key) is not None
    with database() as db:
        original = db.get(Run, parent)
        variant = db.get(Run, child)
        all_keys = list(original.payload["dataset_storage_keys"])
        original.created_at = variant.created_at = now_utc() - timedelta(hours=50)
        db.commit()
    judging._last_maintenance = 0
    judging.maintenance(database)
    assert all(object_storage.size_bytes(key) is None for key in all_keys)
    with database() as db:
        assert db.get(Run, parent) is None and db.get(Run, child) is None


@pytest.mark.parametrize('language',['pypy3','kotlin'])
def test_new_judge_languages_do_not_run_on_legacy_agents(database,language):
    content=snapshot(change=lambda manifest,files:manifest['source'].update(language=language))
    result=create_direct(content)
    n=node()
    assert judging.claim_jobs(n,SECRET,1)==[]
    with database() as db:
        row=db.get(JudgeNodeRow,n)
        row.execution_capabilities=[judging.FULL_VERIFICATION,'judge-icpc-languages-v1']
        db.commit()
    assert judging.claim_jobs(n,SECRET,1)[0]['submission']['language']==language


def test_signed_snapshot_accepts_40_mib_testcase_and_preserves_input(database):
    content = b'1234567\n' * (5 * 1024**2)
    sha = hashlib.sha256(content).hexdigest()
    def change(manifest, files):
        case = manifest['testcases'][0]
        previous = case['input_sha']
        case['input_sha'] = sha
        files['blobs/'+sha] = content
        del files['blobs/'+previous]
    response = submit(snapshot(change=change))
    assert response.status_code == 202, response.text
    with database() as db:
        run = db.get(Run, response.json()['data']['id'])
        key = run.payload['testcases'][0]['input_storage_key']
        assert object_storage.read_bytes(key) == content
    assert judging.MAX_ARCHIVE == 512 * 1024**2
    assert judging.MAX_CONTENT == 512 * 1024**2
