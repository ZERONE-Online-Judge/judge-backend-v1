"""Use Python 3.11 for new submissions, retaining historical runtime IDs."""
from alembic import op
import sqlalchemy as sa

revision = "0053_zoj_runtime_policy"
down_revision = "0052_verification_executions"
branch_labels = depends_on = None


def migrate_problems(connection, old, new):
    problems = sa.table("problems", sa.column("problem_id", sa.String),
                        sa.column("allowed_languages", sa.JSON),
                        sa.column("language_resource_limits", sa.JSON))
    for row in connection.execute(sa.select(problems)).mappings():
        languages = row["allowed_languages"] or []
        limits = dict(row["language_resource_limits"] or {})
        if old not in languages:
            continue
        languages = list(dict.fromkeys(new if lang == old else lang for lang in languages))
        if old in limits and new not in limits:
            limits[new] = dict(limits[old])
        connection.execute(problems.update().where(problems.c.problem_id == row["problem_id"])
                           .values(allowed_languages=languages, language_resource_limits=limits))


def upgrade():
    migrate_problems(op.get_bind(), "python313", "python311")
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("problems", "allowed_languages", server_default='["cpp17","python311","java8","c99","pypy3","kotlin"]')


def downgrade():
    migrate_problems(op.get_bind(), "python311", "python313")
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("problems", "allowed_languages", server_default='["cpp17","python313","java8","c99","pypy3","kotlin"]')
