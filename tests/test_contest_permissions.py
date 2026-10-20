"""HTTP-level permission matrix, ownership, assignment and storage regression tests."""
import os
import json
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ENABLE_DEMO_SEED", "true")
os.environ.setdefault("ALLOW_EMPTY_OTP", "true")

from app.main import app
from app.models import ContestStatus, now_utc
from app.orm_models import StaffAccountRow, SubmissionRow
from app.services.contest_roles import ROLE_PERMISSIONS, roles_for_scopes
from app.services.store import store
from app.services.storage import object_storage

client = TestClient(app)


def login(email):
    response = TestClient(app).post("/api/auth/general/otp/verify", json={"email": email, "otp_code": "", "force_new_session": True})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["data"]["operator_session"]["access_token"]}


@pytest.fixture(scope="module")
def context():
    contest = store.create_contest("Permission tests", "Test", "Test", now_utc() + timedelta(days=2), status=ContestStatus.DRAFT)
    cid = contest.contest_id
    division = store.create_contest_division(cid, "general", "General")
    problem = store.create_problem(cid, division.division_id, "A", "Review me", "Statement", 1000, 128, {}, 1)
    store.update_problem(cid, problem.problem_id, editorial="PRIVATE EDITORIAL")
    tokens = {}
    accounts = {}
    for role in ROLE_PERMISSIONS:
        account = store.upsert_contest_operator(cid, f"{role}-{uuid4().hex}@zoj.com", role, ["master"] if role == "owner" else [role])
        accounts[role] = account
        tokens[role] = login(str(account.email))
    return {"cid": cid, "division": division, "problem": problem, "tokens": tokens, "accounts": accounts, "prefix": f"/api/operator/contests/{cid}"}


@pytest.mark.parametrize("role", list(ROLE_PERMISSIONS))
def test_read_permission_matrix(context, role):
    c = context
    permissions = ROLE_PERMISSIONS[role]
    routes = {
        "/dashboard": None, "/divisions": None,
        "/operators": "contest.staff.manage", "/participants": "contest.participant.view",
        "/notices": "contest.notice.view", "/boards": "contest.board.question.view",
        "/submissions": "contest.submission.view", "/submission-filters": "contest.submission.view",
        "/judge-history": "contest.submission.view", "/scoreboard/internal": "contest.scoreboard.view",
        "/scoreboard/presentation": "contest.scoreboard.view", "/problems": "contest.problem.view",
        "/mail-logs": "contest.audit.view", "/audit-logs": "contest.audit.view", "/access-logs": "contest.access_log.view",
        "/access-log-stats": "contest.access_log.view",
        f"/problems/{c['problem'].problem_id}/testcase-sets": "contest.problem.manage",
        f"/problems/{c['problem'].problem_id}/package-status": "contest.problem.manage",
        f"/problems/{c['problem'].problem_id}/assets": "contest.problem.view",
        f"/divisions/{c['division'].division_id}/scoreboard/release": "contest.scoreboard.view",
    }
    for path, permission in routes.items():
        response = client.get(c["prefix"] + path, headers=c["tokens"][role])
        allowed = role in {"owner", "master"} or permission is None or permission in permissions
        assert response.status_code == (200 if allowed else 403), (role, path, response.text)
    dashboard = client.get(c["prefix"] + "/dashboard", headers=c["tokens"][role]).json()["data"]
    if role not in {"owner", "master", "staff_manager"}:
        assert dashboard["operators"] == []
    if role not in {"owner", "master", "submissions_viewer"}:
        assert dashboard["submission_count"] == dashboard["pending_jobs"] == 0


@pytest.mark.parametrize("role", [role for role in ROLE_PERMISSIONS if role not in {"owner", "master"}])
def test_denied_mutations_are_checked_server_side(context, role):
    c = context
    permissions = ROLE_PERMISSIONS[role]
    mutations = [
        ("patch", "/settings", {"title": "Unauthorized"}, "contest.settings.manage"),
        ("patch", "/settings", {"scoreboard_freeze_mode": "live"}, "contest.scoreboard.manage"),
        ("patch", "/settings", {"emergency_notice": "Unauthorized"}, "contest.notice.manage"),
        ("post", "/divisions", {"name": "Unauthorized"}, "contest.participant.manage"),
        ("post", "/operators", {"email": "forbidden@zoj.com", "display_name": "No", "roles": ["problem_reviewer"]}, "contest.staff.manage"),
        ("delete", "/operators/nobody@zoj.com", None, "contest.staff.manage"),
        ("post", "/notices", {"title": "No", "body": "No"}, "contest.notice.manage"),
        ("delete", "/notices/unknown", None, "contest.notice.manage"),
        ("delete", "/boards/unknown", None, "contest.board.question.manage"),
        ("post", "/boards/unknown/answers", {"body": "No"}, "contest.board.question.manage"),
        ("delete", "/participants/unknown", None, "contest.participant.manage"),
        ("post", "/participants:bulk-create", {"teams": []}, "contest.participant.manage"),
        ("post", "/storage/presign-upload", {"category": "testcases", "filename": "a.in"}, "contest.problem.manage"),
        ("delete", f"/problems/{c['problem'].problem_id}", None, "contest.problem.manage"),
        ("post", f"/problems/{c['problem'].problem_id}/testcase-sets", {}, "contest.problem.manage"),
        ("post", f"/problems/{c['problem'].problem_id}/test-submissions", {"language": "cpp17", "source_code": "int main(){}"}, "contest.problem.test"),
        ("post", f"/divisions/{c['division'].division_id}/scoreboard/release", {"action": "all"}, "contest.scoreboard.manage"),
    ]
    for method, path, body, permission in mutations:
        if permission in permissions:
            continue
        response = client.request(method, c["prefix"] + path, headers=c["tokens"][role], json=body)
        assert response.status_code == 403, (role, method, path, response.text)


def test_division_management_belongs_to_participant_managers(context):
    from sqlalchemy import delete
    from app.orm_models import ContestDivisionRow
    c = context
    path = c["prefix"] + "/divisions"
    settings = c["tokens"]["settings_manager"]
    participants = c["tokens"]["participants_manager"]
    assert client.post(path, headers=settings, json={"name": "Forbidden"}).status_code == 403
    created = client.post(path, headers=participants, json={"name": f"Division {uuid4().hex}"})
    assert created.status_code == 200
    division_id = created.json()["data"]["division_id"]
    try:
        assert client.patch(path + "/" + division_id, headers=settings, json={"name": "Forbidden"}).status_code == 403
        assert client.patch(path + "/" + division_id, headers=participants, json={"name": "Managed by participants"}).status_code == 200
    finally:
        with store._session() as db:
            db.execute(delete(ContestDivisionRow).where(ContestDivisionRow.division_id == division_id))
            db.commit()


def test_role_selection_validation_multiselect_and_protected_master(context):
    c = context
    headers = c["tokens"]["master"]
    email = f"multi-{uuid4().hex}@zoj.com"
    for name, roles in [("", ["problem_reviewer"]), ("  ", ["problem_reviewer"]), ("Name", []), ("Name", ["unknown"]), ("Name", ["master", "problem_author"])]:
        response = client.post(c["prefix"] + "/operators", headers=headers, json={"email": email, "display_name": name, "roles": roles})
        assert response.status_code == 422
    assert client.post(c["prefix"] + "/operators", headers=headers, json={"email": email, "display_name": "Name"}).status_code == 422
    roles = ["participants_manager", "problem_reviewer"]
    response = client.post(c["prefix"] + "/operators", headers=headers, json={"email": email, "display_name": "  Reviewer  ", "roles": roles})
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["display_name"] == "Reviewer"
    assert result["contest_roles"][c["cid"]] == roles
    multi_headers = login(email)
    assert client.get(c["prefix"] + "/participants", headers=multi_headers).status_code == 200
    assert client.get(c["prefix"] + "/problems", headers=multi_headers).status_code == 200
    assert client.get(c["prefix"] + "/submissions", headers=multi_headers).status_code == 403
    # A role change takes effect immediately for the same existing bearer token.
    update = client.patch(c["prefix"] + "/operators/" + email, headers=headers, json={"display_name": "Reviewer", "roles": ["problem_reviewer"]})
    assert update.status_code == 200
    assert client.get(c["prefix"] + "/participants", headers=multi_headers).status_code == 403
    staff_headers = c["tokens"]["staff_manager"]
    master_email = str(c["accounts"]["owner"].email)
    for target, roles in [(email, ["master"]), (master_email, ["problem_reviewer"]), (master_email, ["master"])]:
        for method, path in [("post", "/operators"), ("patch", "/operators/" + target)]:
            response = client.request(method, c["prefix"] + path, headers=staff_headers, json={"email": target, "display_name": "Escalation", "roles": roles})
            assert response.status_code == 403
    assert client.delete(c["prefix"] + "/operators/" + master_email, headers=staff_headers).status_code == 403
    assert client.patch(c["prefix"] + "/operators/" + master_email, headers=headers, json={"display_name": "Master", "roles": ["problem_reviewer"]}).status_code == 409
    assert client.delete(c["prefix"] + "/operators/" + master_email, headers=headers).status_code == 409
    assert client.patch(c["prefix"] + "/operators/" + master_email, headers=headers, json={"display_name": "Master", "roles": ["master"]}).status_code == 200


def test_admin_assignment_always_protected_master(context):
    headers = login("test3@zoj.com")
    email = f"assigned-{uuid4().hex}@zoj.com"
    response = client.post("/api/admin/contests", headers=headers, json={"organization_name": "Permissions", "operator_email": email})
    assert response.status_code == 200, response.text
    cid = response.json()["data"]["contest_id"]
    assigned = next(account for account in store.contest_operator_accounts(cid) if str(account.email) == email)
    assert assigned.contest_roles[cid] == ["owner"]
    assert assigned.contest_scopes[cid] == ["contest.*", "contest.owner"]
    assert cid in assigned.protected_master_contests
    other = f"assigned-{uuid4().hex}@zoj.com"
    response = client.post(f"/api/admin/contests/{cid}/operators", headers=headers, json={"email": other, "roles": ["problem_reviewer"]})
    assert response.status_code == 200
    assert response.json()["data"]["contest_roles"][cid] == ["master"]
    assert cid not in response.json()["data"]["protected_master_contests"]


def test_problem_viewer_reads_statements_without_private_files_or_judging(context):
    c = context
    source = store.create_problem(c["cid"], c["division"].division_id, "B", "View me", "Statement", 1000, 128, {}, 2)
    pid = source.problem_id
    store.update_problem(c["cid"], pid, editorial="PRIVATE EDITORIAL",
        statement_translations={"en": {"title": "English title", "statement": "Public English"}},
        editorial_translations={"en": "PRIVATE ENGLISH EDITORIAL"})
    headers = c["tokens"]["problems_viewer"]
    result = client.get(c["prefix"] + "/problems", headers=headers)
    assert result.status_code == 200
    problem = next(item for item in result.json()["data"] if item["problem_id"] == pid)
    assert problem["statement"] == "Statement"
    assert problem["editorial"] == ""
    assert problem["editorial_translations"] == {}
    assert problem["statement_translations"]["en"]["statement"] == "Public English"
    assert "solved_team_count" not in problem
    keys = {}
    for category in ["assets", "editorial-assets", "package-files/checker"]:
        key = f"contests/{c['cid']}/problems/{pid}/{category}/viewer-{uuid4().hex}.png"
        object_storage.write_bytes(key, b"image")
        store.create_problem_asset(c["cid"], pid, "image.png", key, "image/png", 5, "a" * 64)
        keys[category] = key
    assets = client.get(c["prefix"] + f"/problems/{pid}/assets", headers=headers)
    assert assets.status_code == 200
    listed = {item["storage_key"] for item in assets.json()["data"]}
    assert keys["assets"] in listed
    assert keys["editorial-assets"] not in listed
    assert keys["package-files/checker"] not in listed
    for category in ["editorial-assets", "package-files/checker"]:
        assert client.get("/api/storage/objects/" + keys[category], headers=headers).status_code == 403
    request = client.post(c["prefix"] + f"/problems/{pid}/test-submissions", headers=headers, json={"language": "cpp17", "source_code": "int main(){}"})
    assert request.status_code == 403
    assert client.patch(c["prefix"] + f"/problems/{pid}", headers=headers, json={"title": "Forbidden"}).status_code == 403
    assert store.problems[pid].title == "View me"
    store.delete_problem(c["cid"], pid)


def test_legacy_reviewer_scopes_still_include_problem_viewing(context):
    c = context
    account = store.upsert_contest_operator(c["cid"], f"legacy-review-{uuid4().hex}@zoj.com", "Legacy reviewer", ["problem_reviewer"])
    legacy_scopes = ["contest.view", "contest.problem.review", "contest.problem.test"]
    with store._session() as db:
        row = db.get(StaffAccountRow, account.staff_account_id)
        row.contest_scopes = json.dumps({c["cid"]: legacy_scopes})
        row.contest_roles = "{}"
        db.commit()
    assert set(roles_for_scopes(legacy_scopes)) == {"problems_viewer", "problem_reviewer"}
    headers = login(str(account.email))
    result = client.get(c["prefix"] + "/problems", headers=headers)
    assert result.status_code == 200
    assert all(item["editorial"] == "" for item in result.json()["data"])
    assert client.get(c["prefix"] + f"/problems/{c['problem'].problem_id}/assets", headers=headers).status_code == 200
    assert client.get(c["prefix"] + f"/problems/{c['problem'].problem_id}/testcase-sets", headers=headers).status_code == 403


def test_reviewer_ownership_and_private_assets(context):
    c = context
    cid, pid = c["cid"], c["problem"].problem_id
    headers = c["tokens"]["problem_reviewer"]
    prefix = c["prefix"]
    for category, mime in [("assets", "image/png"), ("problem-assets", "image/png"), ("editorial-assets", "image/png"), ("package-files/checker", "text/plain"), ("package-files/package-resource", "image/png")]:
        key = f"contests/{cid}/problems/{pid}/{category}/asset.png"
        object_storage.write_bytes(key, b"asset")
        store.create_problem_asset(cid, pid, "asset.png", key, mime, 5, "a" * 64)
    problem = client.get(prefix + "/problems", headers=headers).json()["data"][0]
    assert problem["editorial"] == ""
    assert "solved_team_count" not in problem
    assets = client.get(prefix + f"/problems/{pid}/assets", headers=headers).json()["data"]
    assert len(assets) == 2
    assert all("/assets/" in asset["storage_key"] or "/problem-assets/" in asset["storage_key"] for asset in assets)
    # A public statement image URL remains usable without authorization headers.
    assert client.get(assets[0]["download_url"]).content == b"asset"
    own = client.post(prefix + f"/problems/{pid}/test-submissions", headers=headers, json={"language": "cpp17", "source_code": "int main(){}"})
    assert own.status_code == 200
    sid = own.json()["data"]["submission_id"]
    with store._session() as db:
        row = db.get(SubmissionRow, sid)
        row.status = "wrong_answer"
        row.judge_message = "[input] SECRET CASE [expected] SECRET ANSWER"
        db.commit()
    for suffix in ["", "/status:wait?wait_seconds=0"]:
        result = client.get(prefix + "/test-submissions/" + sid + suffix, headers=headers)
        assert result.status_code == 200
        assert result.json()["data"]["judge_message"] is None
        other = client.get(prefix + "/test-submissions/" + sid + suffix, headers=c["tokens"]["problem_author"])
        assert other.status_code == 403
        assert client.get(prefix + "/test-submissions/" + sid + suffix, headers=c["tokens"]["submissions_viewer"]).status_code == 200
    assert client.get(prefix + "/submissions/" + sid, headers=headers).status_code == 403
    # General participant submissions must never be read through the test endpoint.
    with store._session() as db:
        row = db.get(SubmissionRow, sid)
        row.submission_kind = "participant"
        db.commit()
    assert client.get(prefix + "/test-submissions/" + sid, headers=headers).status_code == 404


def test_contest_scope_storage_signatures_and_namespace(context):
    c = context
    cid, pid = c["cid"], c["problem"].problem_id
    author = c["tokens"]["problem_author"]
    reviewer = c["tokens"]["problem_reviewer"]
    key = f"contests/{cid}/problems/{pid}/testcases/private.in"
    object_storage.write_bytes(key, b"private")
    assert client.get("/api/storage/objects/" + key, headers=reviewer).status_code == 403
    assert client.get("/api/storage/objects/" + key, headers=author).content == b"private"
    assert client.get("/api/storage/objects/" + key).status_code == 401
    put_url = object_storage.presigned_put_url(key)
    get_url = object_storage.presigned_get_url(key)
    assert client.put("/api/storage/objects/" + key, content=b"bad").status_code == 403
    assert client.put(get_url, content=b"bad").status_code == 403
    assert client.get(put_url).status_code == 401
    assert client.get(get_url.replace("private.in", "other.in")).status_code == 401
    expired = int(time.time()) - 10
    signature = object_storage._signature("GET", key, expired)
    assert client.get(f"/api/storage/objects/{key}?expires={expired}&signature={signature}").status_code == 401
    assert client.put(put_url, content=b"updated").status_code == 200
    assert client.get(get_url).content == b"updated"
    other_contest = store.create_contest("Other", "Other", "Other")
    foreign = f"contests/{other_contest.contest_id}/problem-assets/secret.txt"
    object_storage.write_bytes(foreign, b"foreign")
    assert client.get("/api/storage/objects/" + foreign, headers=author).status_code == 403
    for role, headers in c["tokens"].items():
        assert client.get(f"/api/operator/contests/{other_contest.contest_id}/dashboard", headers=headers).status_code == 403, role
    for category in ["../elsewhere", "/absolute", "a/../../bad"]:
        assert client.post(c["prefix"] + "/storage/presign-upload", headers=author, json={"category": category, "filename": "x"}).status_code == 422
    response = client.post(c["prefix"] + f"/problems/{pid}/assets", headers=author, json={"original_filename": "secret.txt", "storage_key": foreign, "mime_type": "text/plain", "file_size": 7, "sha256": "a" * 64})
    assert response.status_code == 403
    assert object_storage.read_bytes(foreign) == b"foreign"


def test_scoreboard_settings_field_isolation_and_minimal_submission_filters(context):
    c = context
    headers = c["tokens"]["scoreboard_manager"]
    assert client.patch(c["prefix"] + "/settings", headers=headers, json={"scoreboard_freeze_mode": "live"}).status_code == 200
    assert client.patch(c["prefix"] + "/settings", headers=headers, json={"scoreboard_freeze_mode": "frozen", "title": "Escalation"}).status_code == 403
    assert store.contests[c["cid"]].scoreboard_freeze_mode == "live"
    presentation = client.get(c["prefix"] + "/scoreboard/presentation", headers=c["tokens"]["scoreboard_viewer"]).json()["data"]
    assert "statement" not in presentation["sections"][0]["problems"][0]
    assert "editorial" not in presentation["sections"][0]["problems"][0]
    result = client.get(c["prefix"] + "/submission-filters", headers=c["tokens"]["submissions_viewer"]).json()["data"]
    assert set(result["problems"][0]) == {"problem_id", "problem_code", "title", "division_id"}
    assert all(set(team) == {"participant_team_id", "team_name", "division_id"} for team in result["teams"])


def test_legacy_wildcard_and_last_master_protection():
    contest = store.create_contest("Legacy", "Legacy", "Legacy")
    legacy = store.upsert_contest_operator(contest.contest_id, f"legacy-{uuid4().hex}@zoj.com", "Legacy")
    with store._session() as db:
        row = db.get(StaffAccountRow, legacy.staff_account_id)
        row.contest_roles = "{}"
        db.commit()
    headers = login(str(legacy.email))
    prefix = f"/api/operator/contests/{contest.contest_id}"
    assert client.get(prefix + "/operators", headers=headers).json()["data"][0]["contest_roles"][contest.contest_id] == ["owner"]
    result = client.patch(prefix + "/operators/" + str(legacy.email), headers=headers, json={"display_name": "Legacy", "roles": ["problem_reviewer"]})
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "contest_owner_immutable"


def test_historical_assignment_recovery_requires_success_and_exact_contest():
    import json
    from sqlalchemy import create_engine, text
    from app.database import backfill_assigned_contest_masters

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE staff_accounts (staff_account_id TEXT, email TEXT, contest_scopes TEXT, contest_roles TEXT, protected_master_contests TEXT)"))
        connection.execute(text("CREATE TABLE operational_audit_logs (scope TEXT, method TEXT, status_code INTEGER, path TEXT, details TEXT)"))
        connection.execute(text("INSERT INTO staff_accounts VALUES ('one', 'operator@zoj.com', :scopes, '{}', '[]')"), {"scopes": json.dumps({"assigned": ["contest.*"], "failed": ["contest.*"], "ordinary": ["contest.*"], "initial-unknown": ["contest.*"]})})
        for cid, status in [("assigned", 200), ("failed", 403), ("removed", 200)]:
            connection.execute(text("INSERT INTO operational_audit_logs VALUES ('admin', 'POST', :status, :path, :details)"), {"status": status, "path": f"/api/admin/contests/{cid}/operators", "details": json.dumps({"body": {"email": "OPERATOR@zoj.com"}})})
        connection.execute(text("INSERT INTO operational_audit_logs VALUES ('admin', 'POST', 200, '/api/admin/contests', :details)"), {"details": json.dumps({"body": {"operator_email": "operator@zoj.com"}})})
        backfill_assigned_contest_masters(connection)
        backfill_assigned_contest_masters(connection)
        roles, protected = connection.execute(text("SELECT contest_roles, protected_master_contests FROM staff_accounts")).one()
        assert json.loads(roles) == {"assigned": ["master"]}
        assert json.loads(protected) == ["assigned"]


def test_foreign_resource_ids_do_not_bypass_contest_scope(context):
    c = context
    other = store.create_contest("Private", "Private", "Private")
    division = store.create_contest_division(other.contest_id, "private", "Private")
    problem = store.create_problem(other.contest_id, division.division_id, "P", "Private", "Secret", 1000, 128, {}, 1)
    own_prefix = c["prefix"]
    author = c["tokens"]["problem_author"]
    reviewer = c["tokens"]["problem_reviewer"]
    for headers in [author, reviewer]:
        assert client.get(own_prefix + f"/problems/{problem.problem_id}/assets", headers=headers).status_code == 404
        assert client.post(own_prefix + f"/problems/{problem.problem_id}/test-submissions", headers=headers, json={"language": "cpp17", "source_code": "int main(){}"}).status_code == 404
    assert client.post(own_prefix + "/problems:copy", headers=author, json={"source_problem_id": problem.problem_id, "target_division_id": c["division"].division_id}).status_code == 404
    assert client.patch(own_prefix + f"/problems/{problem.problem_id}", headers=author, json={"title": "Stolen"}).status_code == 404
    assert client.delete(own_prefix + f"/problems/{problem.problem_id}", headers=author).status_code == 404


def test_private_question_notifications_only_reach_authorized_posts_staff(context):
    c = context
    email = f"participant-{uuid4().hex}@zoj.com"
    store.create_participant_team(c["cid"], c["division"].division_id, "Question team", "Participant", email, [])
    store.update_contest_settings(c["cid"], status=ContestStatus.RUNNING, start_at=now_utc() - timedelta(hours=1), end_at=now_utc() + timedelta(hours=2), freeze_at=now_utc() + timedelta(hours=1))
    logged_in = client.post(f"/api/contests/{c['cid']}/participant-login/otp/verify", json={"email": email, "otp_code": "", "force_new_session": True})
    assert logged_in.status_code == 200
    before = set(store.mail_queue)
    response = client.post(f"/api/contests/{c['cid']}/boards", headers={"Authorization": "Bearer " + logged_in.json()["data"]["access_token"]}, json={"title": "Private", "body": "Private participant question", "visibility": "private"})
    assert response.status_code == 200
    recipients = {str(mail.recipient_email) for key, mail in store.mail_queue.items() if key not in before and mail.mail_type == "contest_question_created"}
    assert recipients == {str(c["accounts"][role].email) for role in ("owner", "master", "posts_manager")}


def test_notice_management_is_independent_from_board_management(context):
    c = context
    headers = c["tokens"]["notices_manager"]
    board_headers = c["tokens"]["posts_manager"]
    endpoint = c["prefix"] + "/notices"
    created = client.post(endpoint, headers=headers, json={"title": "Notice", "body": "Body", "emergency": True})
    assert created.status_code == 200
    notice_id = created.json()["data"]["contest_notice_id"]
    assert client.patch(endpoint + "/" + notice_id, headers=headers, json={"title": "Updated"}).status_code == 200
    assert client.patch(c["prefix"] + "/settings", headers=headers, json={"emergency_notice": "Emergency"}).status_code == 200
    assert client.get(c["prefix"] + "/boards", headers=headers).status_code == 403
    assert client.get(endpoint, headers=board_headers).status_code == 403
    assert client.patch(endpoint + "/" + notice_id, headers=board_headers, json={"title": "Forbidden"}).status_code == 403
    assert client.delete(endpoint + "/" + notice_id, headers=headers).status_code == 200


def test_all_nonmaster_roles_can_be_selected_together(context):
    c = context
    roles = [role for role in ROLE_PERMISSIONS if role not in {"owner", "master", "participant_preview"}]
    assert len(roles) == 12
    email = f"all-roles-{uuid4().hex}@zoj.com"
    response = client.post(c["prefix"] + "/operators", headers=c["tokens"]["master"], json={"email": email, "display_name": "All selected", "roles": roles})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["contest_roles"][c["cid"]] == roles
    headers = login(email)
    for path in ["/notices", "/boards", "/audit-logs", "/access-logs"]:
        assert client.get(c["prefix"] + path, headers=headers).status_code == 200
    response = client.patch(c["prefix"] + "/operators/" + email, headers=c["tokens"]["master"], json={"display_name": "All selected", "roles": roles})
    assert response.status_code == 200
    # Removing the standalone notice role revokes its scope immediately, even
    # though the account still has board, settings and every other limited role.
    response = client.patch(c["prefix"] + "/operators/" + email, headers=c["tokens"]["master"], json={"display_name": "Without notices", "roles": [role for role in roles if role != "notices_manager"]})
    assert response.status_code == 200
    assert client.get(c["prefix"] + "/notices", headers=headers).status_code == 403
    assert client.get(c["prefix"] + "/boards", headers=headers).status_code == 200
    assert client.post(c["prefix"] + "/operators", headers=c["tokens"]["master"], json={"email": email, "display_name": "Invalid mix", "roles": ["master", *roles]}).status_code == 422


def test_legacy_combined_notice_grants_migrate_without_new_board_role_escalation():
    import json
    from sqlalchemy import create_engine, text
    from app.database import backfill_separate_notice_roles

    engine = create_engine("sqlite://")
    scopes = {
        "legacy": ["contest.view", "contest.board.question.manage", "contest.notice.manage"],
        "new-board-only": ["contest.view", "contest.board.question.manage"],
        "already-split": ["contest.view", "contest.board.question.manage", "contest.notice.manage"],
        "master": ["contest.*"],
    }
    roles = {
        "legacy": ["posts_manager", "problem_reviewer"],
        "new-board-only": ["posts_manager"],
        "already-split": ["posts_manager", "notices_manager"],
        "master": ["master"],
    }
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE staff_accounts (staff_account_id TEXT, contest_scopes TEXT, contest_roles TEXT)"))
        connection.execute(text("INSERT INTO staff_accounts VALUES ('one', :scopes, :roles)"), {"scopes": json.dumps(scopes), "roles": json.dumps(roles)})
        backfill_separate_notice_roles(connection)
        backfill_separate_notice_roles(connection)
        saved_scopes, saved_roles = connection.execute(text("SELECT contest_scopes, contest_roles FROM staff_accounts")).one()
        assert json.loads(saved_scopes) == scopes
        assert json.loads(saved_roles) == {**roles, "legacy": ["posts_manager", "problem_reviewer", "notices_manager"]}
