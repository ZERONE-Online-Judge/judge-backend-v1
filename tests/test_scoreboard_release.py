import os
from datetime import timedelta
from uuid import uuid4

os.environ.setdefault('ENABLE_DEMO_SEED', 'true')
os.environ.setdefault('ALLOW_EMPTY_OTP', 'true')

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models import ContestStatus, ContestResourceAccess, now_utc
from app.orm_models import ParticipantTeamRow, ProblemRow, SubmissionRow
from app.services.store import store

client = TestClient(app)


@pytest.fixture
def ranking():
    now = now_utc()
    contest = store.create_contest('Release test', 'Test', '', start_at=now-timedelta(hours=3),
        freeze_at=now-timedelta(hours=2), end_at=now-timedelta(hours=1), status=ContestStatus.ENDED)
    store.update_contest_settings(contest.contest_id, scoreboard_access_after_end=ContestResourceAccess.PUBLIC)
    divisions = [store.create_contest_division(contest.contest_id, code, code) for code in ('A', 'B')]
    submission_ids = []
    for division in divisions:
        with store._session() as db:
            problem = ProblemRow(contest_id=contest.contest_id, division_id=division.division_id,
                problem_code='A', title='Sum', statement='', time_limit_ms=1000, memory_limit_mb=256, display_order=1)
            db.add(problem)
            db.flush()
            for index in range(3):
                team = ParticipantTeamRow(contest_id=contest.contest_id, division_id=division.division_id,
                    team_name=f'{division.code}-secret-team-{index}')
                db.add(team)
                db.flush()
                submission = SubmissionRow(contest_id=contest.contest_id, division_id=division.division_id,
                    problem_id=problem.problem_id, participant_team_id=team.participant_team_id,
                    language='cpp17', source_code='secret source', status='accepted',
                    submitted_at=now-timedelta(minutes=90 if index == 0 else 80))
                db.add(submission)
                db.flush()
                submission_ids.append(submission.submission_id)
            db.commit()
    response = client.post('/api/auth/general/otp/verify', json={
        'email':'test3@zoj.com', 'otp_code':'', 'force_new_session':True})
    assert response.status_code == 200
    token = response.json()['data']['operator_session']['access_token']
    return contest.contest_id, [d.division_id for d in divisions], submission_ids, {'Authorization':f'Bearer {token}'}


def url(contest, division):
    return f'/api/operator/contests/{contest}/divisions/{division}/scoreboard/release'


def public(contest, division):
    response = client.get(f'/api/contests/{contest}/divisions/{division}/scoreboard')
    assert response.status_code == 200, response.text
    return response.json()['data']


def test_partial_release_preserves_freeze_and_matches_presentation(ranking):
    contest, (division, other), _, headers = ranking
    before_other = public(contest, other)
    endpoint = url(contest, division)
    started = client.post(endpoint, headers=headers, json={'action':'start'})
    assert started.status_code == 200, started.text
    assert [item['rank'] for item in started.json()['data']['ranks']] == [1, 2]
    hidden = public(contest, division)
    assert all(row['solved'] == 0 and row['submission_count'] == 0 for row in hidden['rows'])
    assert hidden['release']['ranks'] == []
    assert public(contest, other) == before_other
    # Rank 2 is shared by two teams; reveal them together and retain hidden rank 1.
    revealed = client.post(endpoint, headers=headers, json={'action':'rank','rank':2})
    assert revealed.status_code == 200
    assert revealed.json()['data']['revealed_count'] == 2
    board = public(contest, division)
    assert [row['solved'] for row in board['rows']] == [1, 1, 0]
    assert board['rows'][-1]['team_name'] == 'A-secret-team-0'
    assert board['rows'][-1]['submission_count'] == 0
    assert board['release']['ranks'] == []
    assert all(score['best_submission_id'] is None for row in board['rows'][1:] for score in row['problem_scores'])
    presentation = client.get(f'/api/operator/contests/{contest}/scoreboard/presentation', headers=headers).json()['data']
    section = next(s for s in presentation['sections'] if s['division']['division_id'] == division)
    assert section['rows'] == board['rows']
    assert section['release'] == board['release']
    assert public(contest, other) == before_other


def test_release_persists_idempotently_and_uses_fixed_snapshot(ranking):
    contest, (division, other), submissions, headers = ranking
    endpoint = url(contest, division)
    client.post(endpoint, headers=headers, json={'action':'start'})
    client.post(endpoint, headers=headers, json={'action':'rank','rank':2})
    before = public(contest, division)
    with store._session() as db:
        db.get(SubmissionRow, submissions[0]).status = 'wrong_answer'
        db.commit()
    assert public(contest, division) == before
    assert client.post(endpoint, headers=headers, json={'action':'start'}).json()['data']['revealed_count'] == 2
    assert client.post(endpoint, headers=headers, json={'action':'rank','rank':2}).json()['data']['revealed_count'] == 2
    assert client.get(endpoint, headers=headers).json()['data']['revealed_count'] == 2
    response = client.post(endpoint, headers=headers, json={'action':'all'})
    assert response.status_code == 200
    assert response.json()['data']['mode'] == 'all'
    board = public(contest, division)
    assert not board['frozen']
    assert board['rows'][0]['team_name'] == 'A-secret-team-0'
    assert board['rows'][0]['solved'] == 1
    assert public(contest, other)['frozen']
    assert store.scoreboard_release(contest, other)['mode'] == 'not_started'


def test_direct_all_release_is_independent(ranking):
    contest, (division, other), _, headers = ranking
    response = client.post(url(contest, division), headers=headers, json={'action':'all'})
    assert response.status_code == 200
    assert response.json()['data']['revealed_count'] == 3
    assert public(contest, division)['frozen'] is False
    assert public(contest, other)['frozen'] is True


def test_release_validates_auth_scope_rank_and_start(ranking):
    contest, (division, _), _, headers = ranking
    endpoint = url(contest, division)
    assert client.get(endpoint).status_code == 401
    assert client.post(endpoint, json={'action':'all'}).status_code == 401
    assert client.post(url(str(uuid4()), division), headers=headers, json={'action':'all'}).status_code == 404
    assert client.post(endpoint, headers=headers, json={'action':'rank','rank':1}).status_code == 409
    client.post(endpoint, headers=headers, json={'action':'start'})
    assert client.post(endpoint, headers=headers, json={'action':'rank','rank':3}).status_code == 409
    assert client.post(endpoint, headers=headers, json={'action':'rank'}).status_code == 409
    assert client.post(endpoint, headers=headers, json={'action':'rank','rank':0}).status_code == 422
    assert store.scoreboard_release(contest, division)['revealed_count'] == 0


def test_release_waits_for_end_and_pending_judging(ranking):
    contest, (division, _), submissions, headers = ranking
    endpoint = url(contest, division)
    store.update_contest_settings(contest, status=ContestStatus.RUNNING, end_at=now_utc()+timedelta(hours=1))
    assert client.post(endpoint, headers=headers, json={'action':'start'}).status_code == 409
    store.update_contest_settings(contest, status=ContestStatus.ENDED, end_at=now_utc()-timedelta(hours=1))
    with store._session() as db:
        db.get(SubmissionRow, submissions[0]).status = 'judging'
        db.commit()
    assert client.post(endpoint, headers=headers, json={'action':'all'}).status_code == 409
    assert store.scoreboard_release(contest, division)['mode'] == 'not_started'


def test_migration_creates_release_table_and_preserves_existing_state():
    import importlib.util
    from pathlib import Path
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[1] / 'migrations/versions/0023_scoreboard_releases.py'
    spec = importlib.util.spec_from_file_location('release_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert 'scoreboard_releases' in sa.inspect(connection).get_table_names()
        connection.execute(sa.text("INSERT INTO scoreboard_releases (division_id, contest_id, mode, snapshot_rows, revealed_ranks, created_at) VALUES ('d', 'c', 'partial', '[]', '[2]', CURRENT_TIMESTAMP)"))
        migration.upgrade()
        assert connection.scalar(sa.text('SELECT revealed_ranks FROM scoreboard_releases')) == '[2]'
        migration.downgrade()
        assert 'scoreboard_releases' not in sa.inspect(connection).get_table_names()
