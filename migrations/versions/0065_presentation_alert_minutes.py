"""Configure presentation freeze and end reminder minutes per contest."""
from alembic import op
import sqlalchemy as sa

revision = "0065_presentation_alert_minutes"
down_revision = "0064_team_withdrawal"
branch_labels = depends_on = None

FIELDS = (
    "presentation_freeze_alert_minutes",
    "presentation_end_alert_minutes",
)


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("contests")}
    for field in FIELDS:
        if field not in columns:
            op.add_column("contests", sa.Column(
                field, sa.JSON(), nullable=False,
                server_default=sa.text("'[30,20,10,5]'"),
            ))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("contests")}
    for field in reversed(FIELDS):
        if field in columns:
            op.drop_column("contests", field)
