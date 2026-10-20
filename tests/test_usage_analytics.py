import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.main import app
from app.database import SessionLocal
from app.orm_models import GeneralSessionRow, UsageEventRow
from app.services import usage_ingest, usage_reports
from app.services.store import store
from app.settings import settings

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_events():
    with SessionLocal() as db:
        db.execute(delete(UsageEventRow))
        db.commit()
    usage_ingest._limits.clear()


def payload(**changes):
    return {"event_id": str(uuid4()), "visitor_id": str(uuid4()), "visit_id": str(uuid4()), "path": "/about", **changes}


def login(email):
    result = TestClient(app).post('/api/auth/general/otp/verify', json={"email": email, "otp_code": "", "force_new_session": True})
    assert result.status_code == 200, result.text
    return result.json()["data"]


def auth(token):
    return {"Authorization": "Bearer " + token}


def events():
    with SessionLocal() as db:
        return db.scalars(select(UsageEventRow)).all()


def test_ingest_deduplicates_and_never_retains_sensitive_url_content():
    event = payload(path='/login?email=secret@example.com#access_token=secret', referrer='https://www.google.com/search?q=secret', active_seconds=86400)
    first = client.post('/api/public/usage', json=event)
    second = client.post('/api/public/usage', json=event)
    assert first.status_code == second.status_code == 202
    row, = events()
    assert row.page_key == 'public.login'
    assert row.referrer_host == 'google.com'
    assert row.active_seconds <= 2
    values = vars(row)
    assert not any(key in values for key in ['email', 'client_ip', 'path', 'user_agent'])
    assert 'secret' not in str(values)
    assert event['visitor_id'] not in str(values)
    assert row.account_key is None


def test_visible_time_is_monotonic_and_clamped_to_server_elapsed_time():
    event = payload()
    client.post('/api/public/usage', json=event)
    with SessionLocal() as db:
        row = db.scalar(select(UsageEventRow))
        row.created_at = usage_ingest.now_utc() - timedelta(seconds=100)
        db.commit()
    client.post('/api/public/usage', json={**event, 'active_seconds': 25})
    client.post('/api/public/usage', json={**event, 'active_seconds': 5})
    assert events()[0].active_seconds == 25
    client.post('/api/public/usage', json={**event, 'active_seconds': 86400})
    assert 100 <= events()[0].active_seconds <= 105
    assert client.post('/api/public/usage', json={**event, 'path': '/notices'}).json()['data']['accepted'] is False
    assert len(events()) == 1


@pytest.mark.parametrize('headers', [{'dnt':'1'}, {'sec-gpc':'1'}, {'origin':'https://unrelated.example'}, {'user-agent':'Googlebot'}, {'user-agent':'HeadlessChrome'}])
def test_opt_out_bots_and_foreign_origins_are_not_collected(headers):
    result = client.post('/api/public/usage', json=payload(), headers=headers)
    assert result.status_code == 202
    assert result.json()['data']['accepted'] is False
    assert events() == []


def test_unknown_paths_contests_and_forged_metadata_are_rejected():
    for path in ['/private/secret@example.com', '/admin/analytics', '/operator', f'/contests/{uuid4()}', 'https://evil.test/about']:
        assert client.post('/api/public/usage', json=payload(path=path)).json()['data']['accepted'] is False
    for extra in [{'audience':'service_master'}, {'active_seconds':-1}, {'active_seconds':86401}, {'path':'x'*257}, {'visitor_id':'bad-id'}]:
        assert client.post('/api/public/usage', json=payload(**extra)).status_code == 422
    assert events() == []


def test_disabled_collection_and_bounded_per_browser_and_network_limits(monkeypatch):
    monkeypatch.setattr(settings, 'feature_usage_analytics', False)
    assert client.post('/api/public/usage', json=payload()).json()['data']['accepted'] is False
    assert events() == []
    monkeypatch.setattr(settings, 'feature_usage_analytics', True)
    visitor = str(uuid4())
    assert all(usage_ingest.allow_event(visitor) for _ in range(60))
    assert not usage_ingest.allow_event(visitor)
    usage_ingest._limits.clear()
    assert all(usage_ingest.allow_event(str(uuid4()), 'network') for _ in range(3000))
    assert not usage_ingest.allow_event(str(uuid4()), 'network')


def test_authenticated_audience_and_reports_require_real_service_master():
    master = login('test3@zoj.com')
    operator = login('test4@zoj.com')
    event = payload(path='/admin/analytics')
    result = client.post('/api/public/usage', json=event, headers=auth(master['access_token']))
    assert result.json()['data']['accepted'] is True
    row, = events()
    assert row.audience == 'service_master'
    assert row.account_key == usage_ingest.private_key('account', 'test3@zoj.com')
    assert client.get('/api/admin/analytics').status_code == 401
    assert client.get('/api/admin/analytics', headers=auth(operator['access_token'])).status_code == 403
    result = client.get('/api/admin/analytics', headers=auth(master['operator_session']['access_token']))
    assert result.status_code == 200, result.text
    assert result.headers['cache-control'] == 'private, no-store'
    assert result.json()['data']['summary']['signed_in_users'] == 1
    before = None
    with SessionLocal() as db:
        session = db.scalar(select(GeneralSessionRow).where(GeneralSessionRow.email == 'test3@zoj.com', GeneralSessionRow.revoked_at.is_(None)))
        before = session.last_seen_at
    client.post('/api/public/usage', json=payload(), headers=auth(master['access_token']))
    with SessionLocal() as db:
        assert db.get(GeneralSessionRow, session.general_session_id).last_seen_at == before
        active = db.get(GeneralSessionRow, session.general_session_id)
        active.revoked_at = usage_ingest.now_utc()
        db.commit()
    client.post('/api/public/usage', json=payload(), headers=auth(master['access_token']))
    assert any(row.audience == 'anonymous' for row in events())


def test_participant_and_operator_with_preview_permission_are_distinct():
    contest = next(iter(store.contests.values()))
    participant = client.post(f'/api/contests/{contest.contest_id}/participant-login/otp/verify', json={'email':'test1@zoj.com','otp_code':'','force_new_session':True}).json()['data']
    client.post('/api/public/usage', json=payload(path=f'/contests/{contest.contest_id}'), headers=auth(participant['access_token']))
    assert events()[0].audience == 'participant'
    account = store.upsert_contest_operator(contest.contest_id, f'preview-{uuid4().hex}@zoj.com', 'Preview', ['participant_preview'])
    preview = login(str(account.email))
    client.post('/api/public/usage', json=payload(path=f'/contests/{contest.contest_id}/problems'), headers=auth(preview['access_token']))
    assert any(row.audience == 'operator' for row in events())


def add_event(when, visitor, visit, *, cid=None, service='public', audience='anonymous', page='public.home', seconds=0):
    with SessionLocal() as db:
        db.add(UsageEventRow(event_id=str(uuid4()), visitor_key=visitor, visit_key=visit, account_key='signed-in' if audience!='anonymous' else None,
            contest_id=cid,service=service,page_key=page,audience=audience,device='desktop',browser='Chrome',referrer_host='direct',active_seconds=seconds,created_at=when,last_seen_at=when))
        db.commit()


def test_reports_use_kst_boundaries_distinct_visitors_zero_bins_and_independent_filters(monkeypatch):
    now=datetime(2026,9,23,3,tzinfo=timezone.utc)
    monkeypatch.setattr(usage_reports,'now_utc',lambda:now)
    boundary=datetime(2026,9,21,15,tzinfo=timezone.utc)  # Sept 22 at midnight KST.
    cid=store.create_contest('Analytics contest','Test','Test').contest_id
    add_event(boundary-timedelta(seconds=1),'returning','old')
    add_event(boundary,'returning','visit',cid=cid,service='contest',page='contest.overview',seconds=30)
    add_event(boundary+timedelta(hours=1),'returning','visit',cid=cid,service='contest',page='contest.problems',seconds=60)
    add_event(boundary+timedelta(hours=1),'new','another',cid=cid,service='contest',page='contest.problems')
    add_event(boundary+timedelta(hours=2),'operator','staff',audience='operator',service='operator',page='operator.home')
    add_event(boundary+timedelta(days=1),'future','next')
    report=usage_reports.usage_report(datetime(2026,9,22).date(),datetime(2026,9,22).date(),cid,None,'all')
    assert report['summary']['views']==3
    assert report['summary']['visitors']==report['summary']['visits']==2
    assert report['summary']['returning_visitors']==report['summary']['new_visitors']==1
    assert report['summary']['average_active_seconds']==30
    assert len(report['timeline'])==24
    assert report['timeline'][0]['views']==1
    assert report['timeline'][1]['views']==2
    assert report['timeline'][2]['views']==0
    assert report['hours'][1]['views']==2
    assert next(row for row in report['heatmap'] if row['weekday']==1 and row['hour']==0)['views']==1
    assert report['contests'][0]['title']=='Analytics contest'
    assert {row['key']:row['views'] for row in report['pages']}=={'contest.overview':1,'contest.problems':2}
    public=usage_reports.usage_report(datetime(2026,9,22).date(),datetime(2026,9,22).date(),None,None,'visitors')
    assert public['summary']['views']==3
    staff=usage_reports.usage_report(datetime(2026,9,22).date(),datetime(2026,9,22).date(),None,'operator','staff')
    assert staff['summary']['views']==staff['summary']['signed_in_users']==1
    empty=usage_reports.usage_report(datetime(2026,9,22).date(),datetime(2026,9,22).date(),None,'admin','all')
    assert empty['summary']['views']==0
    assert all(row['views']==0 for row in empty['timeline'])
    assert not report['comparison_available']
    assert len(report['heatmap'])==168


def test_range_validation_and_retention(monkeypatch):
    today=usage_reports.now_utc().astimezone(usage_reports.KST).date()
    admin=auth(login('test3@zoj.com')['access_token'])
    for params in [ {'start':today.isoformat(),'end':(today-timedelta(days=1)).isoformat()}, {'start':(today-timedelta(days=366)).isoformat()}, {'end':(today+timedelta(days=1)).isoformat()}, {'start':(today-timedelta(days=396)).isoformat()}, {'service':'made-up'}, {'audience':'unknown'} ]:
        assert client.get('/api/admin/analytics',params=params,headers=admin).status_code==422
    assert client.get('/api/admin/analytics',params={'contest_id':str(uuid4())},headers=admin).status_code==404
    now=usage_ingest.now_utc()
    add_event(now-timedelta(days=396),'expired','expired')
    add_event(now-timedelta(days=394),'retained','retained')
    assert usage_ingest.purge_usage_events()==1
    assert events()[0].visitor_key=='retained'


@pytest.mark.parametrize('url,expected',[('https://user:pass@example.com/path','direct'),('https://foo.private.example/path?token=secret','external'),('https://m.search.naver.com?q=secret','naver.com'),('https://zoj.kr/contests/secret','internal'),('data:text/plain,secret','direct')])
def test_referrers_are_coarse_categories(url,expected):
    assert usage_ingest.referrer_host(url)==expected


def test_operational_metrics_scope_verdicts_and_judge_delay():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.database import Base
    from app.orm_models import AccessLogRow, ContestQuestionRow, OperationalAuditLogRow, SubmissionRow

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    since = datetime(2026, 9, 22, tzinfo=timezone.utc)
    until = since + timedelta(days=1)
    with Session(engine) as db:
        for cid, verdict, kind, delay, when in [
            ('chosen', 'accepted', 'participant', 4, since),
            ('chosen', 'wrong_answer', 'operator_test', 8, since),
            ('chosen', 'waiting', 'participant_preview', 0, since),
            ('other', 'accepted', 'participant', 100, since),
            ('chosen', 'accepted', 'participant', 100, until),
        ]:
            db.add(SubmissionRow(contest_id=cid, division_id='division', problem_id='problem', language='python313', source_code='not returned', status=verdict, submission_kind=kind, submitted_at=when, status_updated_at=when+timedelta(seconds=delay)))
        for cid, kind in [('chosen','participant_login'), ('chosen','login_failed'), ('chosen','session_conflict'), ('chosen','participant_refresh'), (None,'general_login')]:
            db.add(AccessLogRow(contest_id=cid,event_type=kind,account_scope='participant',created_at=since))
        for status in [200,403,500]:
            db.add(OperationalAuditLogRow(scope='operator',action='update',method='POST',path='/operator',status_code=status,contest_id='chosen',created_at=since))
        db.add(ContestQuestionRow(contest_id='chosen',participant_team_id='team',team_member_id='member',title='not returned',body='not returned',created_at=since))
        db.commit()
        selected = usage_reports.operational_metrics(db, since, until, 'chosen')
        assert selected['submissions'] == 3
        assert selected['questions'] == 1
        assert selected['login_successes'] == selected['login_failures'] == selected['session_conflicts'] == 1
        assert selected['operation_failures'] == 2
        assert selected['average_judge_seconds'] == pytest.approx(6, abs=.01)
        assert {row['key'] for row in selected['submission_kinds']} == {'participant','operator_test','participant_preview'}
        assert selected['languages'] == [{'key':'python313','count':3}]
        all_contests = usage_reports.operational_metrics(db, since, until, None)
        assert all_contests['submissions'] == 4
        assert all_contests['login_successes'] == 2
        assert 'not returned' not in json.dumps(selected)
    engine.dispose()
