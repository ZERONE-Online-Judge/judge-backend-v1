from datetime import timedelta

import pytest

from app.models import now_utc
from app.orm_models import ParticipantTeamRow, SubmissionRow
from app.routers import operator
from app.services.errors import permission_denied
from tests.test_judge_security import client, database


@pytest.fixture
def submissions(database, monkeypatch):
    permissions = []
    monkeypatch.setattr(operator, 'require_contest_staff', lambda request, contest_id, permission: permissions.append((contest_id, permission)))
    with database() as db:
        db.add(ParticipantTeamRow(participant_team_id='legacy-team', contest_id='contest', division_id='division', team_name='__operator_test__:old'))
        for index, (sid, kind, team, contest, problem) in enumerate([
            ('real-1', 'participant', 'real-team', 'contest', 'problem'),
            ('review-1', 'operator_test', None, 'contest', 'problem'),
            ('preview-1', 'participant_preview', None, 'contest', 'problem'),
            ('real-2', 'participant', 'real-team', 'contest', 'other-problem'),
            ('mock-1', 'mock_judging', 'real-team', 'contest', 'problem'),
            ('review-2', 'operator_test', None, 'contest', 'problem'),
            ('legacy-review', 'participant', 'legacy-team', 'contest', 'problem'),
            ('private-trial', 'verification_trial', None, 'contest', 'problem'),
            ('other-contest', 'participant', 'real-team', 'other', 'problem'),
        ]):
            db.add(SubmissionRow(submission_id=sid, contest_id=contest, division_id='division', problem_id=problem,
                participant_team_id=team, submission_kind=kind, language='cpp17', source_code='private source',
                status='accepted', submitted_at=now_utc() + timedelta(seconds=index)))
        db.commit()
    return permissions


@pytest.mark.parametrize(('kind', 'expected'), [
    (None, ['legacy-review', 'review-2', 'mock-1', 'real-2', 'preview-1', 'review-1', 'real-1']),
    ('participant', ['real-2', 'real-1']),
    ('operator_test', ['legacy-review', 'review-2', 'review-1']),
    ('participant_preview', ['preview-1']),
    ('mock_judging', ['mock-1']),
])
def test_filter_applies_before_count_and_pagination(submissions, kind, expected):
    params = {'limit': 1}
    if kind:
        params['submission_kind'] = kind
    seen = []
    while True:
        response = client.get('/api/operator/contests/contest/submissions', params=params)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['page']['total_count'] == len(expected)
        assert all(row['source_code'] is None for row in payload['data'])
        seen += [row['submission_id'] for row in payload['data']]
        if payload['page']['next_cursor'] is None:
            break
        params['cursor'] = payload['page']['next_cursor']
    assert seen == expected
    assert all(item == ('contest', 'contest.submission.view') for item in submissions)


def test_kind_combines_with_problem_team_and_division_filters(submissions):
    url = '/api/operator/contests/contest/submissions'
    params = {'submission_kind': 'participant', 'division_id': 'division', 'problem_id': 'problem', 'participant_team_id': 'real-team'}
    payload = client.get(url, params=params).json()
    assert [row['submission_id'] for row in payload['data']] == ['real-1']
    assert payload['page']['total_count'] == 1
    params['submission_kind'] = 'operator_test'
    payload = client.get(url, params=params).json()
    assert payload['data'] == [] and payload['page']['total_count'] == 0


@pytest.mark.parametrize('kind', ['all', 'verification_trial', 'unknown'])
def test_unsupported_kinds_are_rejected(submissions, kind):
    assert client.get('/api/operator/contests/contest/submissions', params={'submission_kind': kind}).status_code == 422


def test_filter_does_not_bypass_submission_view_permission(submissions, monkeypatch):
    def denied(*args):
        raise permission_denied()
    monkeypatch.setattr(operator, 'require_contest_staff', denied)
    assert client.get('/api/operator/contests/contest/submissions?submission_kind=participant').status_code == 403
