from db import SessionLocal
from models.secret import Secret
from repositories.secrets_repository import SecretRepository

def set_secret(key: str, value: str, scope: str, value_type: str = "string"):
    # Validate scope
    allowed_scopes = {"integration", "node"}
    if scope not in allowed_scopes:
        raise ValueError(f"Invalid scope '{scope}'. Must be one of {allowed_scopes}.")

    # Validate value_type
    allowed_types = {"int", "string", "bool"}
    if value_type not in allowed_types:
        raise ValueError(f"Invalid value_type '{value_type}'. Must be one of {allowed_types}.")

    # Convert value according to type
    store_value = value
    if value_type == "int":
        try:
            store_value = str(int(value))
        except Exception:
            raise ValueError(f"Value '{value}' could not be converted to int.")
    elif value_type == "bool":
        if isinstance(value, bool):
            store_value = "true" if value else "false"
        else:
            vstr = str(value).strip().lower()
            if vstr not in ("true", "false"):
                raise ValueError(f"Boolean value must be 'true' or 'false', got '{value}'.")
            store_value = vstr
    # else string: store as is

    with SessionLocal() as session:
        repo = SecretRepository(session)
        repo.add_or_update(key, store_value, scope, value_type)
        session.commit()


def get_secret(key: str, scope: str) -> Secret:
    with SessionLocal() as session:
        repo = SecretRepository(session)
        secret = repo.get(key, scope)
        return secret

def get_secret_value(key: str, scope: str):
    secret = get_secret(key, scope)
    return secret.value if secret else None

def get_secret_value_int(key: str, scope: str):
    secret = get_secret(key, scope)
    try:
        return int(secret.value)
    except:
        raise ValueError(f"The stored {key} is not a number")

def delete_secret(key: str, scope: str):
    with SessionLocal() as session:
        repo = SecretRepository(session)
        repo.delete(key, scope)
        session.commit()

def get_all_secrets(scope: str) -> list[Secret]:
    with SessionLocal() as session:
        repo = SecretRepository(session)
        secrets = repo.get_all(scope)
        return secrets
