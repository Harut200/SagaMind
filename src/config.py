import os
from typing import Optional


class Settings:
    """
    Central settings loader. Validates environment configurations.

    All configuration values are loaded from environment variables with
    sensible development defaults. In production, values MUST be provided
    via environment variables or a .env file.
    """

    def __init__(self):
        # Load .env file if present (development convenience)
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        # ── Server ──────────────────────────────────────────────────
        self.env: str = os.getenv("ENV", "development")
        self.host: str = os.getenv("HOST", "0.0.0.0")
        self.port: int = int(os.getenv("PORT", "8000"))
        self.grpc_port: int = int(os.getenv("GRPC_PORT", "50051"))

        # ── Security ────────────────────────────────────────────────
        self.allowed_workspace_root: str = os.getenv(
            "ALLOWED_WORKSPACE_ROOT",
            "/Users/Harutyun/Desktop/Portfolio1"
        )

        # ── TimescaleDB ─────────────────────────────────────────────
        self.db_host: str = os.getenv("DB_HOST", "127.0.0.1")
        self.db_port: int = int(os.getenv("DB_PORT", "5432"))
        self.db_name: str = os.getenv("DB_NAME", "sagamind")
        self.db_user: str = os.getenv("DB_USER", "sagamind_user")
        self.db_pass: str = os.getenv("DB_PASS", "sagamind_secure_pass_2026")

        # ── Neo4j ───────────────────────────────────────────────────
        self.neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
        self.neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_pass: str = os.getenv("NEO4J_PASS", "sagamind_secure_neo_2026")

        # ── Redis ───────────────────────────────────────────────────
        self.redis_host: str = os.getenv("REDIS_HOST", "127.0.0.1")
        self.redis_port: int = int(os.getenv("REDIS_PORT", "6379"))

        # ── Engine ──────────────────────────────────────────────────
        self.z3_path: str = os.getenv("Z3_PATH", "z3")
        self.openai_api_key: Optional[str] = os.getenv("OPENAI_API_KEY", None)
        self.embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        self.consolidation_model: str = os.getenv("CONSOLIDATION_MODEL", "gpt-4")

        # ── Validation ──────────────────────────────────────────────
        self._validate()

    def _validate(self):
        """Validate critical settings at startup."""
        if self.env == "production":
            if self.db_pass == "sagamind_secure_pass_2026":
                import warnings
                warnings.warn(
                    "DB_PASS is using the default development password in production!",
                    stacklevel=2
                )
            if self.neo4j_pass == "sagamind_secure_neo_2026":
                import warnings
                warnings.warn(
                    "NEO4J_PASS is using the default development password in production!",
                    stacklevel=2
                )

    @property
    def database_url(self) -> str:
        """Construct PostgreSQL connection URL."""
        return f"postgresql://{self.db_user}:{self.db_pass}@{self.db_host}:{self.db_port}/{self.db_name}"


settings = Settings()
