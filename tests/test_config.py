"""
SagaMind — Configuration Tests
==============================

Validates fail-closed production behaviour, workspace-root validation, and the derived
auth/CORS helpers.
"""

import pytest

from src.config import Settings


class TestProductionFailClosed:
    def test_production_with_default_secrets_raises(self):
        with pytest.raises(RuntimeError):
            Settings(env="production")

    def test_production_without_api_keys_raises(self):
        with pytest.raises(RuntimeError):
            Settings(env="production", db_pass="strong-db", neo4j_pass="strong-neo")

    def test_production_with_full_secrets_ok(self):
        s = Settings(
            env="production",
            db_pass="strong-db",
            neo4j_pass="strong-neo",
            api_keys="key-a,key-b",
        )
        assert s.is_production is True
        assert s.auth_enabled is True
        assert s.api_key_set == {"key-a", "key-b"}


class TestWorkspaceValidation:
    def test_relative_workspace_root_raises(self):
        with pytest.raises(ValueError):
            Settings(allowed_workspace_root="relative/path")

    def test_absolute_workspace_root_ok(self, tmp_path):
        s = Settings(allowed_workspace_root=str(tmp_path))
        assert s.allowed_workspace_root == str(tmp_path)


class TestDerivedHelpers:
    def test_dev_auth_disabled_by_default(self):
        s = Settings(env="development")
        assert s.auth_enabled is False

    def test_cors_origin_parsing(self):
        s = Settings(cors_origins="https://a.com, https://b.com")
        assert s.cors_origin_list == ["https://a.com", "https://b.com"]

    def test_database_url(self):
        s = Settings(db_user="u", db_pass="p", db_host="h", db_port=1234, db_name="d")
        assert s.database_url == "postgresql://u:p@h:1234/d"
