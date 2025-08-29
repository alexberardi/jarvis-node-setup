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

class JarvisSecret(IJarvisSecret):
    def __init__(self, key: str, description: str, scope: str, value_type: str):
        self._key = key
        self._description = description
        if scope != "integration" and scope != "node":
            raise ValueError(f"Scope must be integration or node for {key}")
        self._scope = scope

        if value_type != "int" and value_type != "string" and value_type != "bool":
            raise ValueError(f"Value Type must be int, string or bool for {key}")
        self._value_type = value_type

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
