from abc import ABC, abstractmethod

class IJarvisSecret(ABC):
    @property
    @abstractmethod
    def key(self) -> str:
        pass

    @property
    @abstractmethod
    def scope(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @property
    @abstractmethod
    def value_type(self) -> str:
        pass

    @property
    @abstractmethod
    def required(self) -> bool:
        pass

    @property
    def is_sensitive(self) -> bool:
        """Whether this secret contains sensitive data (API keys, passwords, tokens).

        Defaults to True. Override to False for non-sensitive config like URLs,
        units, locations, etc. Non-sensitive values are included in settings
        snapshots so the mobile app can display them.
        """
        return True

class JarvisSecret(IJarvisSecret):
    def __init__(self, key: str, description: str, scope: str, value_type: str, required: bool = True, is_sensitive: bool = True):
        self._key = key
        self._description = description
        if scope != "integration" and scope != "node":
            raise ValueError(f"Scope must be integration or node for {key}")
        self._scope = scope

        if value_type != "int" and value_type != "string" and value_type != "bool":
            raise ValueError(f"Value Type must be int, string or bool for {key}")
        self._value_type = value_type
        self._required = required
        self._is_sensitive = is_sensitive

    @property
    def is_sensitive(self) -> bool:
        return self._is_sensitive

    @property
    def key(self) -> str:
        return self._key

    @property
    def description(self) -> str:
        return self._description

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def value_type(self) -> str:
        return self._value_type

    @property
    def required(self) -> bool:
        return self._required
