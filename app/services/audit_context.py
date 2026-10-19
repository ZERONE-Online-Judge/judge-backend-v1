"""Small, contest-scoped audit targets; never copy verification reports or source."""
import json
import re

from sqlalchemy import func, select
from app.orm_models import ProblemAssetRow, ProblemRow, StaffAccountRow, VerificationRunRow

OPERATOR_PATH = re.compile(r"^/api/(?:operator|admin)/contests/([^/]+)/operators(?:/([^/]+)(?:/account:replace)?)?$")
VERIFICATION_PATH = re.compile(r"^/api/operator/contests/([^/]+)/problems/([^/]+)/(?:verification-tasks(?:/[^/]+(?:/stop)?)?|verification-runs/[^/]+/analysis)$")


def operator_snapshot(db, path, body=None, *, after=False):
    match = OPERATOR_PATH.fullmatch(path)
    if not match:
        return None
    cid, path_email = match.groups()
    body = body if isinstance(body, dict) else {}
    email = body.get('email') if after or not path_email else None
    email = email or path_email
    if not isinstance(email, str):
        return None
    # Request.url.path is already URL decoded; do not unquote it a second time.
    email = email.strip().lower()
    row = db.scalar(select(StaffAccountRow).where(func.lower(StaffAccountRow.email) == email))
    if row is None or not json.loads(row.contest_scopes or '{}').get(cid):
        return None
    return {'email': row.email, 'display_name': row.display_name,
            'roles': json.loads(row.contest_roles or '{}').get(cid, [])}


def verification_snapshot(db, path, result):
    match = VERIFICATION_PATH.fullmatch(path)
    if not match:
        return {}
    cid, pid = match.groups()
    problem = db.get(ProblemRow, pid)
    if problem is None or problem.contest_id != cid:
        return {}
    target = {'problem_title': problem.title, 'problem_code': problem.problem_code}
    result = result if isinstance(result, dict) else {}
    asset_id = result.get('source_asset_id')
    run_match = re.search(r'/verification-runs/([^/]+)/analysis$', path)
    if run_match:
        run = db.get(VerificationRunRow, run_match.group(1))
        if run and run.contest_id == cid and run.problem_id == pid:
            asset_id = run.asset_id
    if isinstance(asset_id, str):
        asset = db.get(ProblemAssetRow, asset_id)
        if asset and asset.contest_id == cid and asset.problem_id == pid:
            target['original_filename'] = asset.original_filename
    analysis = {key: result[key] for key in ('analysis_id', 'status', 'task_id', 'cancel_requested')
                if key in result and isinstance(result[key], (str, bool))}
    return {'target': target, 'analysis': analysis}


def enrich_current_problem_labels(db, logs):
    """One bounded lookup per page, without rewriting historical audit facts."""
    pending = []
    for log in logs:
        match = VERIFICATION_PATH.fullmatch(log.path)
        target = (log.details or {}).get('target')
        target = target if isinstance(target, dict) else {}
        if (match and log.status_code < 400 and log.contest_id == match.group(1)
                and not target.get('problem_title')):
            pending.append((log, match.group(2)))
    if not pending:
        return
    problems = {row.problem_id: row for row in db.execute(
        select(ProblemRow.problem_id, ProblemRow.contest_id, ProblemRow.problem_code, ProblemRow.title)
        .where(ProblemRow.problem_id.in_({pid for _, pid in pending}))
    )}
    for log, pid in pending:
        problem = problems.get(pid)
        if problem and problem.contest_id == log.contest_id:
            log.details = {**(log.details or {}), 'related_target': {
                'problem_title': problem.title, 'problem_code': problem.problem_code,
                'label_source': 'current',
            }}
