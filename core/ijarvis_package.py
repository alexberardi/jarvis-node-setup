"""
JarvisPackage dataclass for declaring pip package dependencies.

Commands use this to specify their third-party package requirements.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class JarvisPackage:
    """
    Python package dependency for a command.

    Attributes:
        name: PyPI package name (e.g., "music-assistant-client")
        version: Version spec (e.g., "1.0.0", ">=1.0,<2.0"). None for latest.

    Examples:
        JarvisPackage("requests")  # Latest version
        JarvisPackage("httpx", "0.25.1")  # Pinned version
        JarvisPackage("pydantic", ">=2.0,<3.0")  # Version constraint
    """
    name: str
    version: Optional[str] = None

    def to_pip_spec(self) -> str:
        """
        Convert to pip install specification.

        Returns:
            String suitable for pip install (e.g., "package==1.0.0")
        """
        if not self.version:
            return self.name

        # If version starts with a digit, it's a pinned version - add ==
        if self.version[0].isdigit():
            return f"{self.name}=={self.version}"

        # Otherwise it's a constraint (>=, <, ~=, etc.) - use as-is
        return f"{self.name}{self.version}"
