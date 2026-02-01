"""
Unit tests for JarvisPackage dataclass.

Tests the package dependency declaration used by commands
to specify their pip package requirements.
"""

import pytest

from core.ijarvis_package import JarvisPackage


class TestJarvisPackageCreation:
    """Test JarvisPackage instantiation"""

    def test_create_with_name_only(self):
        """Package can be created with just a name"""
        pkg = JarvisPackage(name="requests")
        assert pkg.name == "requests"
        assert pkg.version is None

    def test_create_with_pinned_version(self):
        """Package can be created with an exact version"""
        pkg = JarvisPackage(name="music-assistant-client", version="1.0.0")
        assert pkg.name == "music-assistant-client"
        assert pkg.version == "1.0.0"

    def test_create_with_version_constraint(self):
        """Package can be created with version constraints"""
        pkg = JarvisPackage(name="httpx", version=">=0.25.0,<1.0")
        assert pkg.name == "httpx"
        assert pkg.version == ">=0.25.0,<1.0"


class TestToPipSpec:
    """Test conversion to pip install specification"""

    def test_name_only_returns_name(self):
        """Package without version returns just the name"""
        pkg = JarvisPackage(name="requests")
        assert pkg.to_pip_spec() == "requests"

    def test_pinned_version_adds_double_equals(self):
        """Pinned version (no operator) gets == prefix"""
        pkg = JarvisPackage(name="music-assistant-client", version="1.0.0")
        assert pkg.to_pip_spec() == "music-assistant-client==1.0.0"

    def test_semver_with_patch(self):
        """Semantic version with patch number"""
        pkg = JarvisPackage(name="httpx", version="0.25.1")
        assert pkg.to_pip_spec() == "httpx==0.25.1"

    def test_version_constraint_preserved(self):
        """Version constraints (>=, <, etc) are preserved as-is"""
        pkg = JarvisPackage(name="httpx", version=">=0.25.0")
        assert pkg.to_pip_spec() == "httpx>=0.25.0"

    def test_complex_constraint_preserved(self):
        """Complex constraints with multiple operators"""
        pkg = JarvisPackage(name="pydantic", version=">=2.0,<3.0")
        assert pkg.to_pip_spec() == "pydantic>=2.0,<3.0"

    def test_tilde_constraint_preserved(self):
        """Tilde version constraints (~=)"""
        pkg = JarvisPackage(name="numpy", version="~=1.24.0")
        assert pkg.to_pip_spec() == "numpy~=1.24.0"


class TestJarvisPackageEquality:
    """Test equality and hashing"""

    def test_same_name_and_version_are_equal(self):
        """Packages with same name and version are equal"""
        pkg1 = JarvisPackage(name="requests", version="2.31.0")
        pkg2 = JarvisPackage(name="requests", version="2.31.0")
        assert pkg1 == pkg2

    def test_different_name_not_equal(self):
        """Packages with different names are not equal"""
        pkg1 = JarvisPackage(name="requests")
        pkg2 = JarvisPackage(name="httpx")
        assert pkg1 != pkg2

    def test_different_version_not_equal(self):
        """Packages with different versions are not equal"""
        pkg1 = JarvisPackage(name="requests", version="2.31.0")
        pkg2 = JarvisPackage(name="requests", version="2.30.0")
        assert pkg1 != pkg2

    def test_hashable_for_sets(self):
        """Packages can be used in sets for deduplication"""
        pkg1 = JarvisPackage(name="requests", version="2.31.0")
        pkg2 = JarvisPackage(name="requests", version="2.31.0")
        pkg3 = JarvisPackage(name="httpx")

        package_set = {pkg1, pkg2, pkg3}
        assert len(package_set) == 2
