"""Plaud personal-data API client.

Plaud publishes no API key for personal accounts. The sanctioned path is the official
`@plaud-ai/cli`, which performs a browser OAuth2+PKCE login and stores the token set at
`~/.plaud/tokens.json`. This module reuses that token and talks to the same REST API the
CLI does, so we get JSON instead of parsing terminal output.

Refresh strategy, in order:
  1. If the token is still valid, use it.
  2. Shell out to `plaud me`, which makes the CLI refresh and rewrite its own token file.
  3. Fall back to calling the refresh endpoint directly and writing the file ourselves.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx

from .config import Config

# Refresh this many milliseconds before the token actually expires.
EXPIRY_MARGIN_MS = 60_000


class PlaudAuthError(RuntimeError):
    """Raised when there is no usable token and the user must log in again."""


class PlaudApiError(RuntimeError):
    pass


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: int | None = None  # epoch milliseconds, as written by the CLI

    @property
    def stale(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() * 1000 > self.expires_at - EXPIRY_MARGIN_MS

    @classmethod
    def read(cls, path: Path) -> "TokenSet | None":
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not data.get("access_token"):
            return None
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=data.get("expires_at"),
        )

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.chmod(0o600)
        tmp.replace(path)


LOGIN_HINT = (
    "Not signed in to Plaud. On this host run:\n"
    "  plaud login\n"
    "and, if it is headless, first open an SSH tunnel from your desktop:\n"
    "  ssh -L 8199:localhost:8199 <user>@<this-host>\n"
    "then paste the printed URL into your local browser."
)


class PlaudClient:
    def __init__(self, cfg: Config, *, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.token_path = Path(os.path.expanduser(cfg.plaud.token_file))
        self._client = client or httpx.Client(timeout=60.0)
        self._tokens: TokenSet | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PlaudClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- authentication -------------------------------------------------

    def _refresh_via_cli(self) -> bool:
        """`plaud me` triggers the CLI's own refresh and rewrites the token file."""
        try:
            proc = subprocess.run(
                [self.cfg.plaud.cli, "me"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def _refresh_directly(self, refresh_token: str) -> TokenSet:
        response = self._client.post(
            self.cfg.plaud.refresh_url,
            data={"refresh_token": refresh_token},
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise PlaudAuthError(
                f"Token refresh failed ({response.status_code}). {LOGIN_HINT}"
            )
        data = response.json()
        expires_in = data.get("expires_in")
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or refresh_token,
            token_type=data.get("token_type", "Bearer"),
            expires_at=int(time.time() * 1000 + expires_in * 1000) if expires_in else None,
        )

    def access_token(self) -> str:
        tokens = self._tokens or TokenSet.read(self.token_path)
        if tokens is None:
            raise PlaudAuthError(LOGIN_HINT)

        if tokens.stale:
            if self._refresh_via_cli():
                refreshed = TokenSet.read(self.token_path)
                if refreshed and not refreshed.stale:
                    tokens = refreshed
            if tokens.stale:
                if not tokens.refresh_token:
                    raise PlaudAuthError(LOGIN_HINT)
                tokens = self._refresh_directly(tokens.refresh_token)
                tokens.write(self.token_path)

        self._tokens = tokens
        return tokens.access_token

    # --- REST ------------------------------------------------------------

    def _force_refresh(self) -> bool:
        """Refresh even though the stored token claims to still be valid."""
        tokens = self._tokens or TokenSet.read(self.token_path)
        if tokens is None:
            return False
        if self._refresh_via_cli():
            refreshed = TokenSet.read(self.token_path)
            if refreshed and refreshed.access_token != tokens.access_token:
                self._tokens = refreshed
                return True
        if not tokens.refresh_token:
            return False
        try:
            refreshed = self._refresh_directly(tokens.refresh_token)
        except PlaudAuthError:
            return False
        refreshed.write(self.token_path)
        self._tokens = refreshed
        return True

    def _get(self, path: str, *, allow_refresh: bool = True) -> dict[str, Any]:
        response = self._client.get(
            f"{self.cfg.plaud.api_base}{path}",
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Accept": "application/json",
            },
        )
        if response.status_code in (401, 403):
            # The token may have been rotated or revoked since we last read it.
            if allow_refresh and self._force_refresh():
                return self._get(path, allow_refresh=False)
            self._tokens = None
            raise PlaudAuthError(f"Plaud rejected the token ({response.status_code}). {LOGIN_HINT}")
        if response.status_code >= 400:
            raise PlaudApiError(f"GET {path} failed: {response.status_code} {response.text[:300]}")
        return response.json()

    def current_user(self) -> dict[str, Any]:
        return self._get("/open/third-party/users/current")

    def list_files(self, page: int = 1, page_size: int | None = None) -> list[dict[str, Any]]:
        size = page_size or self.cfg.plaud.page_size
        payload = self._get(f"/open/third-party/files/?page={page}&page_size={size}")
        return _extract_items(payload)

    def get_file(self, file_id: str) -> dict[str, Any]:
        payload = self._get(f"/open/third-party/files/{file_id}")
        # Some deployments wrap the object in {"data": {...}}.
        if isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload

    def iter_files(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
        max_pages: int = 50,
    ) -> Iterator[dict[str, Any]]:
        """Walk pages newest-first, stopping at `since` or after `limit` items."""
        seen = 0
        for page in range(1, max_pages + 1):
            items = self.list_files(page=page)
            if not items:
                return
            for item in items:
                created = parse_timestamp(item.get("start_at") or item.get("created_at"))
                if since and created and created < since:
                    return
                yield item
                seen += 1
                if limit and seen >= limit:
                    return
            if len(items) < self.cfg.plaud.page_size:
                return


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """Tolerate the handful of envelope shapes this endpoint has been seen to use."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "files", "list", "results", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _extract_items(value)
            if nested:
                return nested
    return []


def parse_timestamp(value: Any) -> datetime | None:
    """Plaud mixes ISO-8601 strings and epoch numbers (seconds or milliseconds)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_timestamp(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def duration_seconds(item: dict[str, Any]) -> float:
    """Plaud reports `duration` in milliseconds."""
    raw = item.get("duration") or 0
    try:
        return float(raw) / 1000
    except (TypeError, ValueError):
        return 0.0
