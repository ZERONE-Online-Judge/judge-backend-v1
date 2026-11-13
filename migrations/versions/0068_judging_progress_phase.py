"""Expose bounded retry progress without changing verdicts or test counts."""
from alembic import op
import sqlalchemy as sa

revision = "0068_judging_progress_phase"
down_revision = "0067_freeze_count_default"
branch_labels = depends_on = None


def upgrade():
    for table in ("submissions", "editor_judge_runs"):
        if "progress_phase" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(table, sa.Column("progress_phase", sa.String(32), nullable=True))


def downgrade():
    for table in ("editor_judge_runs", "submissions"):
        if "progress_phase" in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}:
            op.drop_column(table, "progress_phase")
