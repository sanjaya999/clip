import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from .models import AIProvider, ProviderSettings, UploadPrivacy


class ProviderSettingsStore:
    """Per-provider API key, model and base URL that the user chose to remember, kept in SQLite."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_settings ("
                "provider TEXT PRIMARY KEY, api_key TEXT, model TEXT, base_url TEXT)"
            )
        # Keys are stored in plain text, so only the owner may read the file.
        path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def all(self) -> dict[AIProvider, ProviderSettings]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT provider, api_key, model, base_url FROM provider_settings").fetchall()
        return {
            AIProvider(provider): ProviderSettings(api_key=api_key, model=model, base_url=base_url)
            for provider, api_key, model, base_url in rows
        }

    def save(self, provider: AIProvider, settings: ProviderSettings) -> None:
        """Set the given fields and keep the ones left as None."""
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO provider_settings (provider, api_key, model, base_url) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET "
                "api_key = COALESCE(excluded.api_key, api_key), "
                "model = COALESCE(excluded.model, model), "
                "base_url = COALESCE(excluded.base_url, base_url)",
                (provider.value, settings.api_key, settings.model, settings.base_url),
            )

    def forget(self, provider: AIProvider) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM provider_settings WHERE provider = ?", (provider.value,))


@dataclass
class SavedAccount:
    """A platform account the user connected: its credentials and tokens, and how uploads to it behave."""
    credentials: dict[str, str] = field(default_factory=dict)
    auto_upload: bool = False
    default_privacy: UploadPrivacy = UploadPrivacy.PRIVATE
    default_description: str = ""


class AccountStore:
    """Upload credentials and tokens per platform, kept in the same owner-only SQLite file as the AI keys."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS upload_accounts ("
                "platform TEXT PRIMARY KEY, credentials TEXT NOT NULL, settings TEXT NOT NULL)"
            )
        path.chmod(0o600)

    def get(self, platform: str) -> SavedAccount:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT credentials, settings FROM upload_accounts WHERE platform = ?", (platform,)
            ).fetchone()
        if row is None:
            return SavedAccount()
        settings = json.loads(row[1])
        return SavedAccount(
            credentials=json.loads(row[0]),
            auto_upload=bool(settings.get("auto_upload")),
            default_privacy=UploadPrivacy(settings.get("default_privacy") or UploadPrivacy.PRIVATE),
            default_description=settings.get("default_description") or "",
        )

    def save(self, platform: str, account: SavedAccount) -> None:
        settings = {
            "auto_upload": account.auto_upload,
            "default_privacy": account.default_privacy.value,
            "default_description": account.default_description,
        }
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO upload_accounts (platform, credentials, settings) VALUES (?, ?, ?) "
                "ON CONFLICT(platform) DO UPDATE SET credentials = excluded.credentials, settings = excluded.settings",
                (platform, json.dumps(account.credentials), json.dumps(settings)),
            )

    def update_credentials(self, platform: str, changes: dict[str, str]) -> None:
        """Merge new credential values in, as when a platform hands back a rotated token."""
        account = self.get(platform)
        account.credentials = {**account.credentials, **changes}
        self.save(platform, account)

    def forget(self, platform: str) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM upload_accounts WHERE platform = ?", (platform,))
