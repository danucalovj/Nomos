"""Application configuration loaded from environment variables / .env."""
from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_adopt_lock = threading.Lock()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NOMOS_", env_file=".env", extra="ignore"
    )

    host: str = "127.0.0.1"
    port: int = 8484
    data_dir: Path = Path("./data")
    max_upload_mb: int = 25
    base_url: str = "http://127.0.0.1:8484"
    agents_can_create_projects: bool = True

    @property
    def db_path(self) -> Path:
        # Renamed from agentcomms.db (issue #22). Adopt a pre-rename database
        # in place the first time we see one; -wal/-shm ride along so an
        # uncheckpointed WAL is never orphaned. Called from every thread-local
        # connect, so the adoption is locked and re-checked; a rename lost to
        # a concurrent process is tolerated (the winner already moved it).
        new = self.data_dir / "nomos.db"
        legacy = self.data_dir / "agentcomms.db"
        if legacy.exists() and not new.exists():
            with _adopt_lock:
                if legacy.exists() and not new.exists():
                    for suffix in ("", "-wal", "-shm"):
                        src = Path(str(legacy) + suffix)
                        try:
                            if src.exists():
                                src.rename(str(new) + suffix)
                        except FileNotFoundError:
                            pass
        return new

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
