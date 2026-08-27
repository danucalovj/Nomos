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
    agents_can_set_working_dir: bool = True

    @property
    def db_path(self) -> Path:
        # Renamed from agentcomms.db (issue #22). Adopt a pre-rename database
        # in place the first time we see one. The sidecars (-wal, -shm) move
        # FIRST and the main .db moves LAST, so the main file's existence is a
        # correct completion flag: a concurrent thread either sees the legacy
        # db (and waits on the lock) or sees a fully-adopted set — never a
        # renamed db whose committed pages still live in an un-renamed WAL
        # (issue #28 H3). The whole check runs under the lock for the same
        # reason, and any OSError (not just FileNotFoundError) is tolerated:
        # the loser of a cross-process race must not crash a property getter.
        new = self.data_dir / "nomos.db"
        legacy = self.data_dir / "agentcomms.db"
        with _adopt_lock:
            if legacy.exists() and not new.exists():
                for suffix in ("-wal", "-shm", ""):
                    src = Path(str(legacy) + suffix)
                    try:
                        if src.exists():
                            src.rename(str(new) + suffix)
                    except FileNotFoundError:
                        pass  # lost a cross-process race: the winner moved it
                    except OSError:
                        # A REAL sidecar failure must abort before the main db
                        # is renamed, or committed pages could be left behind
                        # under the legacy name. Keep using the legacy file;
                        # adoption retries on the next call.
                        return legacy
        return new if new.exists() or not legacy.exists() else legacy

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
