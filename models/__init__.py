from sqlalchemy.orm import declarative_base

Base = declarative_base()

from .command_data import CommandData
from .secret import Secret