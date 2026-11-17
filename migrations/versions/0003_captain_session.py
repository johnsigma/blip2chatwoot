"""Remember whether a conversation is in a Captain session started by a BLiP icebreaker.

Revision ID: 0003_captain_session
Revises: 0002_conversation_agent
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_captain_session"
down_revision = "0002_conversation_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_mappings") as batch:
        batch.add_column(
            sa.Column(
                "captain_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("conversation_mappings") as batch:
        batch.drop_column("captain_active")
