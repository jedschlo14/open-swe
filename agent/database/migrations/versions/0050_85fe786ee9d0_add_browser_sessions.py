"""Add browser sessions

One row per browser session a thread has run. The partial unique index is what
makes a thread's starts idempotent across replicas: at most one session per
thread is starting, ready, or stopping.
"""

from alembic import op

revision = "85fe786ee9d0"
down_revision = ["1a27b64154a3", "243390dbd39e", "f7b59c5091a8", "52fab62a7608"]
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE browser_session (
            session_id text PRIMARY KEY,
            thread_id text NOT NULL,
            sandbox_id text,
            state text NOT NULL
                CHECK (state IN ('starting', 'ready', 'stopping', 'stopped', 'failed')),
            failure_reason text CHECK (
                failure_reason IN (
                    'sandbox_lost',
                    'sandbox_unsupported',
                    'engine_missing',
                    'launch_failed'
                )
            ),
            stop_reason text CHECK (
                stop_reason IN ('requested', 'thread_closed', 'idle_timeout', 'sandbox_recreated')
            ),
            stream_port integer,
            idle_timeout_seconds integer NOT NULL CHECK (idle_timeout_seconds > 0),
            started_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            last_activity_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            warned_at timestamptz,
            ended_at timestamptz,
            CHECK ((state = 'failed') = (failure_reason IS NOT NULL))
        )
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX browser_session_active_thread_idx
        ON browser_session (thread_id)
        WHERE state IN ('starting', 'ready', 'stopping')
        """
    )

    # Status reads show a thread's most recent session, active or not.
    op.execute(
        "CREATE INDEX browser_session_thread_created_idx ON browser_session (thread_id, created_at)"
    )


def downgrade() -> None:
    raise NotImplementedError
