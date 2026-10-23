from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.database import Base
from app.settings import settings


def test_sqlite_inbox_upgrade_and_downgrade(tmp_path, monkeypatch):
    url = f'sqlite:///{tmp_path / "migration.db"}'
    engine = create_engine(url)
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
        if table.name not in {'rejudgings', 'rejudging_items', 'notifications',
            'editor_bridge_nonces', 'editor_problem_imports', 'auth_rate_limits',
            'identity_accounts', 'account_passwords', 'password_challenges',
            'account_passkeys', 'security_challenges', 'session_verifications',
            'trusted_login_environments'}])
    with engine.begin() as db:
        db.execute(text('DROP INDEX IF EXISTS ix_team_members_email_lower'))
    monkeypatch.setattr(settings, 'database_url', url)
    cfg = Config('alembic.ini')
    command.stamp(cfg, '0039_restore_verification_runs')
    command.upgrade(cfg, 'head')
    inspector = inspect(engine)
    assert 'notifications' in inspector.get_table_names() and 'rejudging_notifications' not in inspector.get_table_names()
    assert {'read_at', 'dismissed_at', 'deleted_at', 'notification_type'} <= {column['name'] for column in inspector.get_columns('notifications')}
    assert 'ix_notification_contest' in {index['name'] for index in inspector.get_indexes('notifications')}
    command.downgrade(cfg, '0041_rejudging_notifications')
    assert 'rejudging_notifications' in inspect(engine).get_table_names()
    command.upgrade(cfg, 'head')
    command.downgrade(cfg, '0039_restore_verification_runs')
    assert 'notifications' not in inspect(engine).get_table_names()
    engine.dispose()
