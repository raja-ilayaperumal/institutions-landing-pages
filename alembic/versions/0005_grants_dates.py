"""Add posted_at / inactive_at / response_deadline_at to landing.grants so we
can filter out stale and expired federal opportunities.

Background: the SAM.gov connector previously stored only title/URL/agency and
left award_date/end_date NULL. Every grant therefore looked equally fresh in
the export, including opportunities whose response date had passed by 18+
months (see MIT NotID 80GSFC24R0055 — inactive since 2024-12-05).

Three new date columns:
  - posted_at:            "Original Published Date" on the SAM.gov page
  - inactive_at:          "Original Inactive Date" — the hard expiry
  - response_deadline_at: "Original Response Date" — when proposals are due

Index on (unitid, inactive_at) so the export's "recent + active" filter
can scan cheaply.

Revision ID: 0005_grants_dates
Revises: 0004_ir_pages_contact
Create Date: 2026-05-26
"""
from alembic import op

revision = "0005_grants_dates"
down_revision = "0004_ir_pages_contact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE landing.grants ADD COLUMN posted_at            DATE")
    op.execute("ALTER TABLE landing.grants ADD COLUMN inactive_at          DATE")
    op.execute("ALTER TABLE landing.grants ADD COLUMN response_deadline_at DATE")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_grants_unitid_inactive_at "
        "ON landing.grants (unitid, inactive_at DESC NULLS LAST)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS landing.ix_grants_unitid_inactive_at")
    for col in ("response_deadline_at", "inactive_at", "posted_at"):
        op.execute(f"ALTER TABLE landing.grants DROP COLUMN IF EXISTS {col}")
