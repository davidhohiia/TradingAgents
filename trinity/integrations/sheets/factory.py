"""Builds the Google Sheets syncer from settings (returns None when disabled / not configured)."""

from __future__ import annotations

from typing import Any

from trinity.config.settings import Settings
from trinity.core.log import get_logger
from trinity.integrations.sheets.client import SheetsClient, SheetsError, google_token_provider
from trinity.integrations.sheets.sync import SheetsSyncer

log = get_logger(__name__)


def build_sheets_syncer(settings: Settings, pm: Any, *, client: SheetsClient | None = None) -> SheetsSyncer | None:
    if not settings.google_sheets_enabled:
        return None
    if client is None:
        sa, tok = settings.google_service_account_file, settings.google_oauth_token_file
        if sa is not None and not sa.exists():
            log.error("GOOGLE_SERVICE_ACCOUNT_FILE %s not found: Google Sheets journal disabled", sa)
            return None
        if sa is None and (tok is None or not tok.exists()):
            log.error("Google Sheets enabled but no credentials (GOOGLE_SERVICE_ACCOUNT_FILE or "
                      "GOOGLE_OAUTH_TOKEN_FILE): journal stays local until configured")
            return None
        try:
            client = SheetsClient(google_token_provider(sa, tok if sa is None else None))
        except (SheetsError, ValueError, OSError) as exc:
            log.error("Google credentials could not be loaded: %s", type(exc).__name__)
            return None
    return SheetsSyncer(pm.repo, client, pm.mode, spreadsheet_id=settings.google_sheets_spreadsheet_id,
                        portfolio_fn=pm.journal_portfolio_data, allocations_fn=pm.growth.allocations,
                        tz=settings.google_sheets_timezone, clock=pm.clock)
