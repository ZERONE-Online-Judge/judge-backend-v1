import os
from datetime import timedelta
from uuid import uuid4

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.orm_models import ContestRow
from app.models import ContestStatus, now_utc
from app.services.errors import AppError
from app.services.store import store

client = TestClient(app)


@pytest.fixture
def context():
    contest = store.create_contest("변경 공지 시험", "Test", "Test", now_utc() + timedelta(days=2), status=ContestStatus.OPEN)
    cid = contest.contest_id
    store.upsert_contest_operator(cid, f"{uuid4().hex}@zoj.com", "Master", ["master"])
    division = store.create_contest_division(cid, "general", "General")
    problem = store.create_problem(cid, division.division_id, "A", "원래 제목", "원래 지문", 1000, 128, {}, 1)
    headers = {}
    for role in ("settings_manager", "problem_author", "notices_manager", "problems_viewer"):
        account = store.upsert_contest_operator(cid, f"{uuid4().hex}@zoj.com", role, [role])
        response = TestClient(app).post("/api/auth/general/otp/verify", json={"email": str(account.email), "otp_code": "", "force_new_session": True})
        assert response.status_code == 200, response.text
        headers[role] = {"Authorization": "Bearer " + response.json()["data"]["operator_session"]["access_token"]}
    yield {"cid": cid, "contest": contest, "problem": problem, "headers": headers,
            "settings": f"/api/operator/contests/{cid}/settings",
            "problem_path": f"/api/operator/contests/{cid}/problems/{problem.problem_id}"}
    with SessionLocal() as db:
        db.get(ContestRow, cid).deleted_at = now_utc()
        db.commit()


def approve(response, **overrides):
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "change_notice_required", response.text
    return {**error["details"], "reason": "운영진 확인에 따른 수정", **overrides}


def notices(c):
    return [notice for notice in store.contest_notices.values() if notice.contest_id == c["cid"]]


def live(c):
    store.update_contest_settings(c["cid"], status=ContestStatus.RUNNING,
        start_at=now_utc() - timedelta(hours=1), freeze_at=now_utc() + timedelta(hours=1), end_at=now_utc() + timedelta(hours=2))


def test_schedule_manager_can_confirm_emergency_without_notice_permission(context):
    c = context
    body = {"end_at": (c["contest"].end_at + timedelta(minutes=30)).isoformat()}
    headers = c["headers"]["settings_manager"]
    challenge = client.patch(c["settings"], headers=headers, json=body)
    approval = approve(challenge, title="대회 종료 시각 변경")
    assert "종료 시간이" in approval["body"] and "에서" in approval["body"]
    assert store.contests[c["cid"]].end_at == c["contest"].end_at
    assert notices(c) == []
    result = client.patch(c["settings"], headers=headers, json={**body, "_change_notice": approval})
    assert result.status_code == 200, result.text
    notice = notices(c)[0]
    assert result.json()["change_notice"] == notice.model_dump(mode="json")
    assert notice.title == approval["title"] and approval["reason"] in notice.body
    assert notice.emergency and notice.pinned and notice.visibility == "participants"
    assert result.json()["data"]["emergency_notice"] == notice.body
    assert client.post(f"/api/operator/contests/{c['cid']}/notices", headers=headers,
        json={"title": "No", "body": "No"}).status_code == 403
    assert client.patch(c["settings"], headers=headers, json={**body, "_change_notice": approval}).status_code == 200
    assert len(notices(c)) == 1


@pytest.mark.parametrize("status", [ContestStatus.DRAFT, ContestStatus.SCHEDULED, ContestStatus.ENDED, ContestStatus.ARCHIVED])
def test_unannounced_contests_save_schedule_without_notice(context, status):
    c = context
    store.update_contest_settings(c["cid"], status=status)
    result = client.patch(c["settings"], headers=c["headers"]["settings_manager"],
        json={"end_at": (c["contest"].end_at + timedelta(minutes=30)).isoformat()})
    assert result.status_code == 200, result.text
    assert notices(c) == []


def test_running_schedule_even_if_edit_ends_contest_needs_notice(context):
    c = context
    live(c)
    body = {"end_at": (now_utc() - timedelta(minutes=1)).isoformat()}
    approval = approve(client.patch(c["settings"], headers=c["headers"]["settings_manager"], json=body))
    assert "종료" in approval["body"] and "프리즈" in approval["body"]
    result = client.patch(c["settings"], headers=c["headers"]["settings_manager"], json={**body, "_change_notice": approval})
    assert result.status_code == 200, result.text


def test_problem_author_can_change_all_fields_after_notice(context):
    c = context
    live(c)
    body = {"title": "수정 제목", "time_limit_ms": 2000, "statement": "수정된 문제 지문"}
    headers = c["headers"]["problem_author"]
    approval = approve(client.patch(c["problem_path"], headers=headers, json=body))
    assert approval["title"] == "A번 문제 제목, 시간 제한, 지문 수정 안내"
    assert "1000ms → 2000ms" in approval["body"]
    assert store.problems[c["problem"].problem_id].title == "원래 제목"
    result = client.patch(c["problem_path"], headers=headers, json={**body, "_change_notice": approval})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["time_limit_ms"] == 2000
    assert len(notices(c)) == 1
    assert approval["reason"] in store.contests[c["cid"]].emergency_notice


def test_noop_language_settings_and_private_editorial_copy(context):
    c = context
    live(c)
    headers = c["headers"]["problem_author"]
    result = client.patch(c["problem_path"], headers=headers, json={"title": "원래 제목"})
    assert result.status_code == 200 and notices(c) == []
    approval = approve(client.patch(c["problem_path"], headers=headers,
        json={"editorial": "SECRET ANSWER", "allowed_languages": ["cpp17"]}))
    assert "SECRET" not in approval["body"]
    assert "제출 언어" in approval["title"]


def test_other_roles_cannot_edit_or_receive_change_details(context):
    c = context
    live(c)
    for role in ("notices_manager", "problems_viewer"):
        result = client.patch(c["problem_path"], headers=c["headers"][role], json={"title": "No"})
        assert result.status_code == 403
    result = client.patch(c["settings"], headers=c["headers"]["problem_author"],
        json={"end_at": (now_utc() + timedelta(hours=4)).isoformat()})
    assert result.status_code == 403
    assert notices(c) == []


def test_reason_is_required_and_snapshot_cannot_be_reused(context):
    c = context
    live(c)
    headers = c["headers"]["problem_author"]
    body = {"time_limit_ms": 2000}
    approval = approve(client.patch(c["problem_path"], headers=headers, json=body))
    rejected = client.patch(c["problem_path"], headers=headers,
        json={**body, "_change_notice": {**approval, "reason": " "}})
    assert rejected.status_code == 422 and notices(c) == []
    store.update_problem(c["cid"], c["problem"].problem_id, time_limit_ms=1500)
    refreshed = client.patch(c["problem_path"], headers=headers, json={**body, "_change_notice": approval})
    assert "1500ms → 2000ms" in approve(refreshed)["body"]
    assert store.problems[c["problem"].problem_id].time_limit_ms == 1500


def test_notice_failure_rolls_back_problem_and_schedule(context, monkeypatch):
    c = context
    live(c)
    def fail(*args, **kwargs):
        raise AppError(503, "notice_write_failed", "Try again")
    for path, body, role in ((c["problem_path"], {"title": "새 제목"}, "problem_author"),
        (c["settings"], {"end_at": (now_utc() + timedelta(hours=5)).isoformat()}, "settings_manager")):
        headers = c["headers"][role]
        approval = approve(client.patch(path, headers=headers, json=body))
        before = store.contests[c["cid"]].end_at
        with monkeypatch.context() as patch:
            patch.setattr(store, "create_contest_notice", fail)
            result = client.patch(path, headers=headers, json={**body, "_change_notice": approval})
        assert result.status_code == 503, result.text
        assert store.problems[c["problem"].problem_id].title == "원래 제목"
        assert store.contests[c["cid"]].end_at == before and notices(c) == []


def test_testcase_changes_also_require_notice(context, monkeypatch):
    c = context
    live(c)
    monkeypatch.setattr(store, "enqueue_bundle_warm", lambda *args: None)
    path = c["problem_path"] + "/testcase-sets"
    headers = c["headers"]["problem_author"]
    body = {"is_active": True}
    approval = approve(client.post(path, headers=headers, json=body))
    assert "테스트케이스" in approval["title"]
    assert store.testcase_sets_for_problem(c["cid"], c["problem"].problem_id) == []
    result = client.post(path, headers=headers, json={**body, "_change_notice": approval})
    assert result.status_code == 200, result.text
    assert len(notices(c)) == 1


def test_asset_delete_failure_preserves_file_and_metadata(context, monkeypatch):
    from app.services.storage import object_storage
    c = context
    live(c)
    key = f"contests/{c['cid']}/problems/{c['problem'].problem_id}/assets/image.png"
    object_storage.write_bytes(key, b"original file", "image/png")
    asset = store.create_problem_asset(c["cid"], c["problem"].problem_id, "image.png", key, "image/png", 13, "a" * 64)
    monkeypatch.setattr(store, "enqueue_bundle_warm", lambda *args: None)
    path = c["problem_path"] + "/assets/" + asset.asset_id
    headers = c["headers"]["problem_author"]
    approval = approve(client.delete(path, headers=headers))
    assert "image.png" in approval["body"]
    def fail(*args, **kwargs):
        raise AppError(503, "notice_write_failed", "Try again")
    with monkeypatch.context() as patch:
        patch.setattr(store, "create_contest_notice", fail)
        assert client.request("DELETE", path, headers=headers, json={"_change_notice": approval}).status_code == 503
    assert object_storage.read_bytes(key) == b"original file"
    assert store.get_problem_asset(c["cid"], c["problem"].problem_id, asset.asset_id)
    assert notices(c) == []
    result = client.request("DELETE", path, headers=headers, json={"_change_notice": approval})
    assert result.status_code == 200, result.text
    assert store.get_problem_asset(c["cid"], c["problem"].problem_id, asset.asset_id) is None
    assert len(notices(c)) == 1


def test_zip_upload_survives_confirmation_and_is_not_processed_before_it(context, monkeypatch):
    from io import BytesIO
    from zipfile import ZipFile
    from app.routers import operator
    c = context
    live(c)
    calls = []
    def build(cid, pid, cases):
        calls.append(cases)
        return {"case_count": len(cases)}
    monkeypatch.setattr(operator, "build_verified_testcase_set", build)
    monkeypatch.setattr(store, "enqueue_bundle_warm", lambda *args: None)
    archive = BytesIO()
    with ZipFile(archive, "w") as zipfile:
        zipfile.writestr("01.in", "1 2\n")
        zipfile.writestr("01.out", "3\n")
    content = archive.getvalue()
    path = c["problem_path"] + "/verified-testcase-sets:zip"
    headers = c["headers"]["problem_author"]
    approval = approve(client.post(path, headers=headers, files={"file": ("tests.zip", content, "application/zip")}))
    assert calls == []
    import json
    result = client.post(path, headers=headers, files={"file": ("tests.zip", content, "application/zip")},
        data={"_change_notice": json.dumps(approval)})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["imported_archive"]["case_count"] == 1
    assert len(calls) == 1 and len(notices(c)) == 1
