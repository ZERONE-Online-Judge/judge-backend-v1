import importlib.util
from pathlib import Path
import sqlalchemy as sa
from app.judge_languages import ProgrammingLanguages
from app.services.problem_archive import ArchiveProblem


def test_legacy_archive_admission_upgrades_without_losing_allowances():
    assert ProgrammingLanguages(allowed_languages=["cpp17", "python313"]).allowed_languages == ["cpp17", "python311"]
    limits = {lang: {"time_limit_ms": 700} for lang in ["cpp17", "python313", "java8", "c99", "pypy3", "kotlin"]}
    problem = ArchiveProblem(problem_code="A", title="Old archive", statement="sum", time_limit_ms=1000,
        memory_limit_mb=256, display_order=1, allowed_languages=["python313"], language_resource_limits=limits)
    assert problem.allowed_languages == ["python311"]
    assert len(problem.language_resource_limits) == 7
    assert problem.language_resource_limits["python311"] == problem.language_resource_limits["python313"]


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
