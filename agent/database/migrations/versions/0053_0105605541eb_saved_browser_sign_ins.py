"""Saved browser sign-ins

One encrypted session (cookies and localStorage) per person and exact origin,
restored into a thread's browser by its owner. ``expired_at`` is set when a
restore finds the session no longer signs in.
"""

from alembic import op

revision = "0105605541eb"
down_revision = "c5d3394c5073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE browser_saved_sign_in (
            sign_in_id text PRIMARY KEY,
            owner_login text NOT NULL,
            origin text NOT NULL,
            encrypted_state text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            expires_at timestamptz NOT NULL,
            last_used_at timestamptz,
            expired_at timestamptz,
            UNIQUE (owner_login, origin)
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError
