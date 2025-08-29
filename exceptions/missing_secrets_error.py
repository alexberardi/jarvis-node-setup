from typing import List

class MissingSecretsError(Exception):
    def __init__(self, missing_secrets: List[str]):
        self.missing_secrets = missing_secrets
        message = f"Missing required secrets: {', '.join(missing_secrets)}"
        super().__init__(message)