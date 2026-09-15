"""Google Drive upload.

Scope is `drive.file` only — the app can see and touch nothing but the files it created
itself. That also keeps the OAuth consent screen non-sensitive, so it can be published to
production without Google review, which matters because an unpublished ("Testing") client
has its refresh tokens revoked after seven days and would break the unattended timer.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from .config import (
    DATA_DIR,
    GOOGLE_CLIENT_FILE,
    GOOGLE_TOKEN_FILE,
    Config,
    ConfigError,
)

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"
FOLDER_CACHE = DATA_DIR / "drive_folders.json"

MIME_TYPES = {
    "md": "text/markdown",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "json": "application/json",
    "txt": "text/plain",
    "summary": "text/markdown",
}

SETUP_HINT = f"""No Google OAuth client found at {GOOGLE_CLIENT_FILE}.

  1. In Google Cloud Console create (or pick) a project and enable the Drive API.
  2. Configure the OAuth consent screen, add the scope
     https://www.googleapis.com/auth/drive.file, and PUBLISH the app
     ("In production"). Left in "Testing", Google expires the refresh token after
     7 days and unattended syncs stop working.
  3. Create an OAuth client ID of type "Desktop app", download the JSON, and save it
     as {GOOGLE_CLIENT_FILE} (chmod 600).
  4. Run: plaud-scribe auth google
"""


class DriveError(RuntimeError):
    pass


def load_credentials() -> Credentials | None:
    if not GOOGLE_TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_credentials(creds)
    return creds


def _save_credentials(creds: Credentials) -> None:
    GOOGLE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOOGLE_TOKEN_FILE.write_text(creds.to_json())
    GOOGLE_TOKEN_FILE.chmod(0o600)


def authorize(cfg: Config) -> Credentials:
    """Run the loopback OAuth flow. On a headless host, tunnel the port first."""
    if not GOOGLE_CLIENT_FILE.exists():
        raise ConfigError(SETUP_HINT)
    port = cfg.drive.oauth_port
    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CLIENT_FILE), SCOPES)
    print(
        f"If this host has no browser, open an SSH tunnel from your desktop first:\n"
        f"  ssh -L {port}:localhost:{port} <user>@<this-host>\n"
        f"then open the URL below in your local browser.\n"
    )
    creds = flow.run_local_server(port=port, open_browser=False, prompt="consent")
    _save_credentials(creds)
    return creds


class DriveUploader:
    def __init__(self, cfg: Config) -> None:
        creds = load_credentials()
        if creds is None or not creds.valid:
            raise DriveError("Google Drive is not authorised. Run: plaud-scribe auth google")
        self.cfg = cfg
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._folders: dict[str, str] = _read_folder_cache()

    # --- folders ---------------------------------------------------------

    def _find_folder(self, name: str, parent_id: str | None) -> str | None:
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = [
            f"name = '{escaped}'",
            f"mimeType = '{FOLDER_MIME}'",
            "trashed = false",
        ]
        query.append(f"'{parent_id}' in parents" if parent_id else "'root' in parents")
        result = (
            self.service.files()
            .list(q=" and ".join(query), fields="files(id, name)", pageSize=10)
            .execute()
        )
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
        cache_key = f"{parent_id or 'root'}/{name}"
        cached = self._folders.get(cache_key)
        if cached and self._still_exists(cached):
            return cached

        found = self._find_folder(name, parent_id)
        if not found:
            body = {"name": name, "mimeType": FOLDER_MIME}
            if parent_id:
                body["parents"] = [parent_id]
            found = self.service.files().create(body=body, fields="id").execute()["id"]
            log.info("Created Drive folder %s", cache_key)

        self._folders[cache_key] = found
        _write_folder_cache(self._folders)
        return found

    def _still_exists(self, file_id: str) -> bool:
        """False only when Drive says the file is gone.

        Any other failure is re-raised: swallowing it would make the caller create a
        second copy of a file that is actually still there.
        """
        try:
            meta = self.service.files().get(fileId=file_id, fields="id, trashed").execute()
        except HttpError as err:
            if err.resp.status in (404, 403):
                return False
            raise
        return not meta.get("trashed", False)

    def folder_for(self, subfolder: str) -> str:
        root = self.ensure_folder(self.cfg.drive.root_folder)
        return self.ensure_folder(subfolder, parent_id=root)

    # --- files -----------------------------------------------------------

    def upload_text(
        self,
        *,
        name: str,
        content: str,
        folder_id: str,
        extension: str,
        existing_id: str | None = None,
    ) -> str:
        media = MediaInMemoryUpload(
            content.encode("utf-8"),
            mimetype=MIME_TYPES.get(extension, "text/plain"),
            resumable=False,
        )
        if existing_id and self._still_exists(existing_id):
            updated = (
                self.service.files()
                .update(fileId=existing_id, body={"name": name}, media_body=media, fields="id")
                .execute()
            )
            return updated["id"]
        created = (
            self.service.files()
            .create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
                fields="id",
            )
            .execute()
        )
        return created["id"]


def _read_folder_cache() -> dict[str, str]:
    try:
        return json.loads(FOLDER_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_folder_cache(data: dict[str, str]) -> None:
    FOLDER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    Path(FOLDER_CACHE).write_text(json.dumps(data, indent=2))
