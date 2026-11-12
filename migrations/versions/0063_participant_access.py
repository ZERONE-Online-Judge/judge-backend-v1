"""Add independent contest-only team and member access restrictions."""
from alembic import op
import sqlalchemy as sa

revision = "0063_participant_access"
down_revision = "0062_session_client_environment"
branch_labels = depends_on = None


def upgrade():
    for table in ("participant_teams", "team_members"):
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        for column in (
            sa.Column("access_blocked", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("access_block_reason", sa.Text(), nullable=True),
            sa.Column("access_blocked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("access_blocked_by", sa.String(255), nullable=True),
        ):
            if column.name not in columns:
                op.add_column(table, column)


def downgrade():
    for table in ("participant_teams", "team_members"):
        for name in ("access_blocked_by", "access_blocked_at", "access_block_reason", "access_blocked"):
            op.drop_column(table, name)
