"""Эндпойнты проверки обновлений — константа кода (updates.ENDPOINTS), не настройка:
ключ endpoints убирается из сохранённых настроек."""
import json

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"


def upgrade():
    db = op.get_bind()
    for row_id, value in db.execute(sa.text("SELECT id, value FROM settings")).fetchall():
        settings = json.loads(value)
        if settings.pop("endpoints", None) is not None:
            db.execute(sa.text("UPDATE settings SET value=:v WHERE id=:i"),
                       {"v": json.dumps(settings, ensure_ascii=False), "i": row_id})


def downgrade():
    pass
