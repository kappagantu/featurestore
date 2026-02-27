"""create or upgrade feature_groups

Revision ID: 0001_create_feature_groups
Revises:
Create Date: 2026-02-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0001_create_feature_groups"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _index_exists(bind, table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(bind)
    indexes = inspector.get_indexes(table_name)
    return any(index["name"] == index_name for index in indexes)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("feature_groups"):
        op.create_table(
            "feature_groups",
            sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("entity", sa.String(length=128), nullable=False),
            sa.Column("owner", sa.String(length=128), nullable=False),
            sa.Column("description", sa.String(length=512), nullable=True),
            sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("schema", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.PrimaryKeyConstraint("name"),
        )
    else:
        columns = {col["name"] for col in inspector.get_columns("feature_groups")}
        if "tenant_id" not in columns:
            op.add_column(
                "feature_groups",
                sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
            )

    inspector = sa.inspect(bind)
    pk = inspector.get_pk_constraint("feature_groups")
    pk_name = pk.get("name")
    pk_columns = pk.get("constrained_columns", [])
    if pk_columns != ["tenant_id", "name"]:
        if pk_name:
            op.drop_constraint(pk_name, "feature_groups", type_="primary")
        op.create_primary_key("feature_groups_pkey", "feature_groups", ["tenant_id", "name"])

    if not _index_exists(bind, "feature_groups", "ix_feature_groups_tenant_owner"):
        op.create_index(
            "ix_feature_groups_tenant_owner", "feature_groups", ["tenant_id", "owner"], unique=False
        )
    if not _index_exists(bind, "feature_groups", "ix_feature_groups_tenant_entity"):
        op.create_index(
            "ix_feature_groups_tenant_entity", "feature_groups", ["tenant_id", "entity"], unique=False
        )


def downgrade() -> None:
    op.drop_index("ix_feature_groups_tenant_entity", table_name="feature_groups")
    op.drop_index("ix_feature_groups_tenant_owner", table_name="feature_groups")
    op.drop_table("feature_groups")
