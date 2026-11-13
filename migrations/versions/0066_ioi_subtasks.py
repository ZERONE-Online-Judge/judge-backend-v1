"""Add IOI subtask configurations and immutable score evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0066_ioi_subtasks"
down_revision = "0065_presentation_alert_minutes"
branch_labels = depends_on = None

COLUMNS = {
    "contests": [sa.Column("scoring_mode", sa.String(16), nullable=False, server_default="icpc")],
    "testcase_sets": [sa.Column("subtasks", sa.JSON(), nullable=False, server_default="[]")],
    "submissions": [sa.Column("scoring_mode", sa.String(16), nullable=False, server_default="icpc"),
        sa.Column("score", sa.Integer(), nullable=True), sa.Column("max_score", sa.Integer(), nullable=True),
        sa.Column("subtask_results", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("scoring_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("scoring_evidence", sa.JSON(), nullable=False, server_default="[]")],
}


def upgrade():
    for table, columns in COLUMNS.items():
        known = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
        for column in columns:
            if column.name not in known:
                op.add_column(table, sa.Column(column.name, column.type, nullable=column.nullable, server_default=column.server_default.arg if column.server_default else None))


def downgrade():
    for table, columns in reversed(list(COLUMNS.items())):
        known = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
        for column in reversed(columns):
            if column.name in known:
                op.drop_column(table, column.name)
