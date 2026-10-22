import pytest
from pydantic import ValidationError
from app.judge_languages import DEFAULT_LANGUAGES, LANGUAGES, ProgrammingLanguages, require_allowed
from app.routers.operator import ProblemUpdateRequest
from app.services.errors import AppError
from types import SimpleNamespace

@pytest.mark.parametrize('value', [[], ['ruby'], ['kotlin','kotlin'], None])
def test_reject_invalid_language_selection(value):
    with pytest.raises(ValidationError): ProgrammingLanguages(allowed_languages=value)
    with pytest.raises(ValidationError): ProblemUpdateRequest(allowed_languages=value)

def test_legacy_default_and_explicit_subset():
    assert set(ProgrammingLanguages().allowed_languages)==set(DEFAULT_LANGUAGES)
    problem=SimpleNamespace(allowed_languages=['kotlin','pypy3'])
    for language in problem.allowed_languages: require_allowed(problem,language)
    with pytest.raises(AppError) as error:require_allowed(problem,'cpp17')
    assert error.value.code=='language_not_allowed'
    assert ProblemUpdateRequest(allowed_languages=['kotlin']).allowed_languages==['kotlin']


from test_problem_archives import context, client, export, upload
from app.services.store import store
from app.database import SessionLocal
from app.orm_models import ProblemAssetRow
from sqlalchemy import select


def test_problem_language_settings_survive_copy_and_archive(context):
    c=context
    response=client.patch(c['prefix']+'/problems/'+c['pid'],headers=c['owner'],json={'allowed_languages':['pypy3','kotlin']})
    assert response.status_code==200,response.text
    assert response.json()['data']['allowed_languages']==['pypy3','kotlin']
    for method,args in [(store.create_submission,(c['cid'],c['pid'],'missing@example.com','cpp17','code')),
                        (store.create_mock_submission,(c['cid'],c['pid'],'python313','code'))]:
        with pytest.raises(AppError) as error:method(*args)
        assert error.value.code=='language_not_allowed'
    with SessionLocal() as db:
        asset=db.scalar(select(ProblemAssetRow).where(ProblemAssetRow.problem_id==c['pid'],ProblemAssetRow.original_filename=='wrong.py'))
        asset.source_language='pypy3';db.commit()
    copied=client.post(c['prefix']+'/problems:copy',headers=c['owner'],json={'source_problem_id':c['pid'],'target_division_id':c['division'],'problem_code':'C'})
    assert copied.status_code==200,copied.text
    assert copied.json()['data']['allowed_languages']==['pypy3','kotlin']
    imported=upload(c,export(c),'import')
    assert imported.status_code==200,imported.text
    pid=imported.json()['data']['problem_id']
    assert store.get_problem(pid).allowed_languages==['pypy3','kotlin']
    assert next(asset for asset in store.problem_assets_for_problem(c['cid'],pid) if asset.original_filename=='wrong.py').source_language=='pypy3'


def test_language_policy_migration_preserves_existing_rows(tmp_path):
    import importlib.util
    import json
    from pathlib import Path
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec=importlib.util.spec_from_file_location('language_migration',Path(__file__).parents[1]/'migrations/versions/0051_allowed_languages.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    engine=sa.create_engine('sqlite:///'+str(tmp_path/'migration.db'))
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE problems (problem_id TEXT PRIMARY KEY)'))
        connection.execute(sa.text('CREATE TABLE problem_assets (asset_id TEXT PRIMARY KEY)'))
        connection.execute(sa.text("INSERT INTO problems VALUES ('original')"))
        migration.op=Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert json.loads(connection.scalar(sa.text('SELECT allowed_languages FROM problems')))==["cpp17", "python313", "java8", "c99", "pypy3", "kotlin"]
        # Schema created from current metadata is safe to upgrade as well.
        migration.upgrade()
        migration.downgrade()
        assert connection.scalar(sa.text('SELECT problem_id FROM problems'))=='original'
    engine.dispose()
