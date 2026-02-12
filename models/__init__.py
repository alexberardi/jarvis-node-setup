from sqlalchemy.orm import declarative_base

Base = declarative_base()

from .command_data import CommandData  # noqa: E402
from .secret import Secret  # noqa: E402

__all__ = ["Base", "CommandData", "Secret"]
