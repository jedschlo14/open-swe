"""Browser session control and egress

Adds the lease the broker checks on every action, the egress allowlist the
sandbox proxy enforces, the element names the confirmation gate classifies
against, and the one action awaiting a person's confirmation.
"""

from alembic import op

revision = "97a388bdb3ed"
down_revision = "85fe786ee9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE browser_session
            ADD COLUMN controller text NOT NULL DEFAULT 'agent'
                CHECK (controller IN ('agent', 'user')),
            ADD COLUMN controller_login text,
            ADD COLUMN lease_epoch bigint NOT NULL DEFAULT 0,
            ADD COLUMN approved_endpoints text[] NOT NULL DEFAULT '{}',
            ADD COLUMN allowed_endpoints text[] NOT NULL DEFAULT '{}',
            ADD COLUMN page_refs jsonb,
            ADD COLUMN pending_confirmation jsonb
        """
    )
    op.execute("ALTER TABLE browser_session DROP CONSTRAINT browser_session_failure_reason_check")
    op.execute(
        """
        ALTER TABLE browser_session ADD CONSTRAINT browser_session_failure_reason_check CHECK (
            failure_reason IN (
                'sandbox_lost',
                'sandbox_unsupported',
                'engine_missing',
                'egress_unavailable',
                'launch_failed'
            )
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError
