from typing import Dict, Any, List

import gspread
from google.oauth2.service_account import Credentials

from app.config import GOOGLE_SHEET_ID, get_service_account_file

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# Matches your current Google Sheet style, but the writer is header-based,
# so it will also work if you add/remove/reorder columns later.
DEFAULT_HEADERS = [
    'id', 'created_time', 'ad_id', 'ad_name', 'adset_id', 'adset_name',
    'campaign_id', 'campaign_name', 'form_id', 'form_name', 'is_organic',
    'platform', 'full_name', 'phone_number', 'email', 'city', 'course',
    'received_at', 'page_id', 'status', 'error', 'raw_data',
]

_worksheet_cache = None


def _norm(value: str) -> str:
    return str(value or '').strip().lower().replace(' ', '_').replace('-', '_')


def get_worksheet():
    global _worksheet_cache
    if _worksheet_cache is not None:
        return _worksheet_cache

    if not GOOGLE_SHEET_ID:
        raise RuntimeError('GOOGLE_SHEET_ID is missing')

    service_file = get_service_account_file()
    creds = Credentials.from_service_account_file(service_file, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)

    try:
        worksheet = spreadsheet.worksheet('Leads')
    except Exception:
        worksheet = spreadsheet.add_worksheet(title='Leads', rows=1000, cols=30)
        worksheet.append_row(DEFAULT_HEADERS)

    existing_headers: List[str] = worksheet.row_values(1)
    if not existing_headers:
        worksheet.append_row(DEFAULT_HEADERS)

    _worksheet_cache = worksheet
    return worksheet


def append_lead_to_sheet(row: Dict[str, Any]):
    """Append by matching current Sheet headers.

    This avoids silent column mismatch. If your sheet already has headers like
    id, created_time, ad_id, form_id, etc., values will go under those columns.
    """
    worksheet = get_worksheet()
    headers = worksheet.row_values(1)
    if not headers:
        headers = DEFAULT_HEADERS
        worksheet.append_row(headers)

    normalized_row = {_norm(k): v for k, v in row.items()}
    values = []
    for header in headers:
        key = _norm(header)
        value = normalized_row.get(key, '')
        if isinstance(value, (dict, list)):
            value = str(value)
        values.append(value)

    print('Appending row to Google Sheet:', values, flush=True)
    worksheet.append_row(
        values,
        value_input_option='USER_ENTERED',
        insert_data_option='INSERT_ROWS',
    )
