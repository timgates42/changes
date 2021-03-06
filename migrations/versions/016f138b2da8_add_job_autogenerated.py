"""add job autogenerated

Revision ID: 016f138b2da8
Revises: 58c2302ec362
Create Date: 2016-06-09 17:32:41.079003

"""

# revision identifiers, used by Alembic.
revision = '016f138b2da8'
down_revision = '58c2302ec362'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column('job', sa.Column('autogenerated', sa.Boolean(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column('job', 'autogenerated')
