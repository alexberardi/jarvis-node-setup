from typing import cast
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from models.secret import Secret

class SecretRepository:
    def __init__(self, db: Session):
        self.db = db

    def add_or_update(self,  key: str, value: str, scope: str, value_type: str):
        now = datetime.now(timezone.utc)
        secret = self.db.query(Secret).filter_by(scope=scope, key=key).first()
        if secret:
            secret.value = value
            secret.updated_at = now
        else:
            secret = Secret(scope=scope, key=key, value=value, value_type=value_type, created_at=now, updated_at=now)
            self.db.add(secret)

    def get(self, key: str, scope: str) -> Secret | None:
        secret = self.db.query(Secret).filter_by(key=key, scope=scope).first()
        return secret

    def delete(self, key: str, scope: str):
        self.db.query(Secret).filter_by(key=key, scope=scope).delete()

    def get_all(self, scope: str) -> list[Secret]:
        secrets = cast(list[Secret], self.db.query(Secret).filter_by(scope=scope).all())
        return secrets