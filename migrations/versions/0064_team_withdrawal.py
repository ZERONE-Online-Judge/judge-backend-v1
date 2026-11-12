"""Add reversible contest-only team withdrawal without deleting results."""
from alembic import op
import sqlalchemy as sa

revision = "0064_team_withdrawal"
down_revision = "0063_participant_access"
branch_labels = depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("participant_teams")}
    for column in (
        sa.Column("withdrawn", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("withdrawal_reason", sa.Text(), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_by", sa.String(255), nullable=True),
    ):
        if column.name not in columns:
            op.add_column("participant_teams", column)


def downgrade():
    for name in ("withdrawn_by", "withdrawn_at", "withdrawal_reason", "withdrawn"):
        op.drop_column("participant_teams", name)
