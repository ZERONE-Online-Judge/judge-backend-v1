"""Keep display-only browser metadata with the session that created it."""
from alembic import op
import sqlalchemy as sa

revision = "0062_session_client_environment"
down_revision = "0061_resolver_release_default"
branch_labels = depends_on = None


def upgrade():
    for table in ("general_sessions", "staff_sessions", "team_sessions"):
        if "client_environment" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(table, sa.Column("client_environment", sa.Text(), nullable=True))


def downgrade():
    for table in ("general_sessions", "staff_sessions", "team_sessions"):
        op.drop_column(table, "client_environment")
