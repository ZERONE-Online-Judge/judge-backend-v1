import importlib.util
from pathlib import Path
import sqlalchemy as sa


def test_runtime_migration_preserves_history_and_manual_limits():
    spec = importlib.util.spec_from_file_location("runtime_migration", Path(__file__).parents[1]/"migrations/versions/0053_zoj_runtime_policy.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    problems = sa.Table("problems", metadata, sa.Column("problem_id", sa.String, primary_key=True),
        sa.Column("allowed_languages", sa.JSON), sa.Column("language_resource_limits", sa.JSON))
    submissions = sa.Table("submissions", metadata, sa.Column("language", sa.String))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(problems.insert(), [{"problem_id":"old", "allowed_languages":["cpp17","python313"],
            "language_resource_limits":{"python313":{"time_limit_ms":700,"disable_auto_adjustment":True}}},
            {"problem_id":"cpp", "allowed_languages":["cpp17"], "language_resource_limits":{}}])
        connection.execute(submissions.insert().values(language="python313"))
        migration.migrate_problems(connection,"python313","python311")
        migration.migrate_problems(connection,"python313","python311")
        row=connection.execute(sa.select(problems).where(problems.c.problem_id=="old")).mappings().one()
        assert row["allowed_languages"]==["cpp17","python311"]
        assert row["language_resource_limits"]["python311"]==row["language_resource_limits"]["python313"]
        assert connection.scalar(sa.select(submissions.c.language))=="python313"
        assert connection.scalar(sa.select(problems.c.allowed_languages).where(problems.c.problem_id=="cpp"))==["cpp17"]
