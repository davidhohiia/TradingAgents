"""Minimal async client for the official Google Sheets API v4 (REST).

Endpoints (https://developers.google.com/sheets/api/reference/rest):
    POST   /v4/spreadsheets                                   spreadsheets.create
    GET    /v4/spreadsheets/{id}?fields=...                   spreadsheets.get
    POST   /v4/spreadsheets/{id}:batchUpdate                  addSheet, updateSheetProperties
    GET    /v4/spreadsheets/{id}/values/{range}               values.get
    POST   /v4/spreadsheets/{id}/values/{range}:append        values.append
    PUT    /v4/spreadsheets/{id}/values/{range}               values.update
    POST   /v4/spreadsheets/{id}/values/{range}:clear         values.clear
Auth: OAuth 2.0 access token with scope https://www.googleapis.com/auth/spreadsheets, obtained with
the official google-auth library from a Service Account key file or an OAuth 2.0 user token file.
Secrets are never logged (the token only travels in the Authorization header).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from trinity.core.log import get_logger

log = get_logger(__name__)

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

TokenProvider = Callable[[bool], Awaitable[str]]  # arg: force_refresh


class SheetsError(Exception):
    def __init__(self, message: str, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def google_token_provider(service_account_file: Path | None = None,
                          oauth_token_file: Path | None = None) -> TokenProvider:
    """Access tokens via the official google-auth library (refresh runs in a worker thread)."""
    import google.auth.transport.requests

    if service_account_file is not None:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(str(service_account_file), scopes=SCOPES)
    elif oauth_token_file is not None:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(oauth_token_file), SCOPES)
    else:
        raise SheetsError("no Google credentials configured", retryable=False)

    async def token(force: bool = False) -> str:
        if force or not creds.valid:
            try:
                await asyncio.to_thread(creds.refresh, google.auth.transport.requests.Request())
            except Exception as exc:  # noqa: BLE001 - network / revoked key
                raise SheetsError(f"Google auth failed: {type(exc).__name__}", retryable=True) from None
        return str(creds.token)

    return token


def a1(sheet: str, cells: str = "") -> str:
    """'Daily Statistics'!A1:G -> URL-safe path segment."""
    rng = f"'{sheet}'" + (f"!{cells}" if cells else "")
    return quote(rng, safe="")


class SheetsClient:
    def __init__(self, token_provider: TokenProvider, *, transport: Any = None, max_attempts: int = 4,
                 base_delay: float = 1.0, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 timeout: float = 30.0) -> None:
        self._token = token_provider
        self.http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleep = sleep

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        last: SheetsError | None = None
        refreshed = False
        for attempt in range(self.max_attempts):
            try:
                token = await self._token(False)
                r = await self.http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last = SheetsError(f"network error: {type(exc).__name__}", retryable=True)
            except SheetsError as exc:
                last = exc
                if not exc.retryable:
                    raise
            else:
                if r.status_code < 300:
                    return r.json() if r.content else {}
                msg = _error_message(r)
                if r.status_code == 401 and not refreshed:
                    refreshed = True
                    await self._token(True)
                    continue
                if r.status_code not in RETRYABLE_STATUS:
                    raise SheetsError(f"HTTP {r.status_code}: {msg}", retryable=False, status=r.status_code)
                last = SheetsError(f"HTTP {r.status_code}: {msg}", retryable=True, status=r.status_code)
            if attempt < self.max_attempts - 1:
                delay = self.base_delay * (2 ** attempt) * (0.5 + random.random() / 2)
                log.warning("Google Sheets request failed (%s), retry %d/%d in %.1fs", last, attempt + 1,
                            self.max_attempts - 1, delay)
                await self.sleep(delay)
        assert last is not None
        raise last

    # ------------------------------------------------------------------ spreadsheet
    async def create_spreadsheet(self, title: str, sheet_titles: list[str], time_zone: str) -> dict[str, Any]:
        body = {"properties": {"title": title, "timeZone": time_zone},
                "sheets": [{"properties": {"title": t}} for t in sheet_titles]}
        return await self._request("POST", API, json=body)

    async def get_metadata(self, spreadsheet_id: str) -> dict[str, Any]:
        return await self._request("GET", f"{API}/{spreadsheet_id}",
                                   params={"fields": "spreadsheetId,properties.title,sheets.properties"})

    async def batch_update(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._request("POST", f"{API}/{spreadsheet_id}:batchUpdate", json={"requests": requests})

    # ------------------------------------------------------------------ values
    async def get_values(self, spreadsheet_id: str, sheet: str, cells: str) -> list[list[Any]]:
        data = await self._request("GET", f"{API}/{spreadsheet_id}/values/{a1(sheet, cells)}")
        return data.get("values", [])

    async def append_rows(self, spreadsheet_id: str, sheet: str, rows: list[list[Any]]) -> dict[str, Any]:
        return await self._request(
            "POST", f"{API}/{spreadsheet_id}/values/{a1(sheet, 'A1')}:append",
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"majorDimension": "ROWS", "values": rows})

    async def update_values(self, spreadsheet_id: str, sheet: str, cells: str, rows: list[list[Any]]) -> dict[str, Any]:
        return await self._request(
            "PUT", f"{API}/{spreadsheet_id}/values/{a1(sheet, cells)}", params={"valueInputOption": "RAW"},
            json={"majorDimension": "ROWS", "values": rows})

    async def clear(self, spreadsheet_id: str, sheet: str, cells: str = "A:Z") -> dict[str, Any]:
        return await self._request("POST", f"{API}/{spreadsheet_id}/values/{a1(sheet, cells)}:clear", json={})


def _error_message(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error", {}).get("message", ""))[:300]
    except ValueError:
        return r.text[:300]
