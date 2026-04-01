from typing import cast
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from models.secret import Secret

class SecretRepository:
    def __init__(self, db: Session):
        self.db = db

    def _filter(self, key: str, scope: str, user_id: int | None = None):
        """Build a query filtered by key, scope, and optionally user_id."""
        q = self.db.query(Secret).filter_by(scope=scope, key=key)
        if scope == "user":
            q = q.filter_by(user_id=user_id)
        return q

    def add_or_update(self, key: str, value: str, scope: str, value_type: str, user_id: int | None = None):
        now = datetime.now(timezone.utc)
        secret = self._filter(key, scope, user_id).first()
        if secret:
            secret.value = value
            secret.updated_at = now
        else:
            secret = Secret(
                scope=scope, key=key, value=value, value_type=value_type,
                user_id=user_id if scope == "user" else None,
                created_at=now, updated_at=now,
            )
            self.db.add(secret)

    def get(self, key: str, scope: str, user_id: int | None = None) -> Secret | None:
        return self._filter(key, scope, user_id).first()

    def delete(self, key: str, scope: str, user_id: int | None = None):
        self._filter(key, scope, user_id).delete()

    def get_all(self, scope: str, user_id: int | None = None) -> list[Secret]:
        q = self.db.query(Secret).filter_by(scope=scope)
        if scope == "user" and user_id is not None:
            q = q.filter_by(user_id=user_id)
        return cast(list[Secret], q.all())
