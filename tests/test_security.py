"""
SagaMind — Security Primitive Tests
===================================

Covers the real filesystem-jail containment (symlink + traversal + prefix-collision)
and the fixed-window rate limiter.
"""

import os

import pytest

from src.security import PathSecurityError, SlidingRateLimiter, contain_path


class TestContainPath:
    def test_path_inside_root_is_allowed(self, tmp_path):
        target = tmp_path / "data" / "file.txt"
        resolved = contain_path(str(target), root=str(tmp_path))
        assert resolved == os.path.realpath(str(target))

    def test_relative_path_resolved_against_root(self, tmp_path):
        resolved = contain_path("a/b.txt", root=str(tmp_path))
        assert resolved.startswith(os.path.realpath(str(tmp_path)))

    def test_absolute_outside_path_rejected(self, tmp_path):
        with pytest.raises(PathSecurityError):
            contain_path("/etc/passwd", root=str(tmp_path))

    def test_dotdot_traversal_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(PathSecurityError):
            contain_path(str(root / ".." / "escape.txt"), root=str(root))

    def test_symlink_escape_rejected(self, tmp_path):
        root = tmp_path / "jail"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "link"
        os.symlink(str(outside), str(link))
        with pytest.raises(PathSecurityError):
            contain_path(str(link / "secret.txt"), root=str(root))

    def test_prefix_collision_rejected(self, tmp_path):
        root = tmp_path / "root"
        sibling = tmp_path / "root_evil"
        root.mkdir()
        sibling.mkdir()
        with pytest.raises(PathSecurityError):
            contain_path(str(sibling / "x.txt"), root=str(root))


class TestSlidingRateLimiter:
    def test_allows_within_limit(self):
        limiter = SlidingRateLimiter(max_per_minute=2)
        assert limiter.allow("k") is True
        assert limiter.allow("k") is True

    def test_blocks_over_limit(self):
        limiter = SlidingRateLimiter(max_per_minute=2)
        limiter.allow("k")
        limiter.allow("k")
        assert limiter.allow("k") is False

    def test_zero_disables_limiting(self):
        limiter = SlidingRateLimiter(max_per_minute=0)
        assert all(limiter.allow("k") for _ in range(100))

    def test_keys_are_independent(self):
        limiter = SlidingRateLimiter(max_per_minute=1)
        assert limiter.allow("a") is True
        assert limiter.allow("b") is True
        assert limiter.allow("a") is False
