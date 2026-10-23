"""Add passkeys, single-use ceremonies and verified browser environments."""

from alembic import op
import sqlalchemy as sa

revision = "0054_account_passkeys"
down_revision = "0053_zoj_runtime_policy"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "account_passkeys",
        sa.Column("credential_id", sa.String(1024), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("identity_accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("transports", sa.JSON(), nullable=False),
        sa.Column("backed_up", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_account_passkeys_account_id", "account_passkeys", ["account_id"]
    )
    op.create_table(
        "security_challenges",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("challenge", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_security_challenges_expires_at", "security_challenges", ["expires_at"]
    )
    op.create_table(
        "session_verifications",
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("general_sessions.general_session_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("verified_at", sa.Integer(), nullable=False),
    )
    op.create_table(
        "trusted_login_environments",
        sa.Column("email", sa.String(254), primary_key=True),
        sa.Column("device_hash", sa.String(64), primary_key=True),
        sa.Column("ip_hash", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_trusted_login_environments_expires_at",
        "trusted_login_environments",
        ["expires_at"],
    )


def downgrade():
    for table in [
        "trusted_login_environments",
        "session_verifications",
        "security_challenges",
        "account_passkeys",
    ]:
        op.drop_table(table)
