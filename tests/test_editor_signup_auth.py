from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.database import SessionLocal
from app.orm_models import IdentityAccountRow
from app.settings import settings


def test_editor_onboarding_verifies_email_before_creating_identity_without_roles(monkeypatch):
    monkeypatch.setattr(settings, 'allow_empty_otp', True)
    email = 'editor-new-'+uuid4().hex+'@example.com'
    client = TestClient(app)
    assert client.post('/api/auth/general/otp/request', json={'email':email}).status_code == 404
    result = client.post('/api/auth/general/editor/otp/request', json={'email':email})
    assert result.status_code == 200, result.text
    code = result.json()['data']['demo_otp']
    with SessionLocal() as db:
        assert db.scalar(select(IdentityAccountRow).where(IdentityAccountRow.email==email)) is None
    result = client.post('/api/auth/general/otp/verify', json={'email':email,'otp_code':code})
    assert result.status_code == 200, result.text
    data = result.json()['data']
    assert data['account']['email'] == email
    assert data['operator_session'] is None and data['operator_contests']==[] and data['participant_contests']==[]
    assert 'zoj_sso=' in result.headers['set-cookie']
    assert client.post('/api/auth/general/otp/verify', json={'email':email,'otp_code':code}).status_code == 401


def test_editor_signup_has_the_same_rate_limits_and_cooldown(monkeypatch):
    called=[]
    monkeypatch.setattr(settings,'allow_empty_otp',False)
    from app.services import auth_rate_limit
    monkeypatch.setattr(auth_rate_limit,'consume',lambda *args:called.append(args))
    email='editor-rate-'+uuid4().hex+'@example.com'
    result=TestClient(app).post('/api/auth/general/editor/otp/request',json={'email':email})
    assert result.status_code == 200,result.text
    assert any(args[0]=='otp-mail-ip' for args in called)
    assert any(args[0]=='otp-mail-email' and args[1]==email for args in called)
    assert 'demo_otp' not in result.text
    assert TestClient(app).post('/api/auth/general/editor/otp/request',json={'email':email}).status_code==429
