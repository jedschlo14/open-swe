"""Browser session handoff

Takeover and handback move through a handoff state while in-flight agent
actions settle, counted by ``agent_inflight``. ``handback_notice`` carries the
page the person left behind to the agent's next browser action.
"""

from alembic import op

revision = "c5d3394c5073"
down_revision = "97a388bdb3ed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE browser_session
            ADD COLUMN handoff text NOT NULL DEFAULT 'none'
                CHECK (handoff IN ('none', 'takeover', 'handback')),
            ADD COLUMN agent_inflight integer NOT NULL DEFAULT 0
                CHECK (agent_inflight >= 0),
            ADD COLUMN handback_notice jsonb
        """
    )


def downgrade() -> None:
    raise NotImplementedError
