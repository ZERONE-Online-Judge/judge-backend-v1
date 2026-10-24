"""Hide participant judging progress by default without changing saved policy."""
from alembic import op
import sqlalchemy as sa

revision = "0058_hidden_progress_default"
down_revision = "0057_freeze_submission_counts"
branch_labels = depends_on = None


def upgrade():
    # SQLite uses ORM insertion defaults. Do not rebuild its referenced contest
    # table just to change a default, or touch any existing contest settings.
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("contests", "participant_progress_visible",
                        existing_type=sa.Boolean(), server_default=sa.false())


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("contests", "participant_progress_visible",
                        existing_type=sa.Boolean(), server_default=sa.true())
