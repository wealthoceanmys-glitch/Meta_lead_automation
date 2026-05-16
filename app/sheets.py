from typing import Dict, Any, List

import gspread
from google.oauth2.service_account import Credentials

from app.config import GOOGLE_SHEET_ID, get_service_account_file

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

DEFAULT_HEADERS = [
    'id', 'created_time', 'ad_id', 'ad_name', 'adset_id', 'adset_name',
    'campaign_id', 'campaign_name', 'form_id', 'form_name', 'is_organic',
    'platform', 'what_is_your_current_experience_level?',
    'please_choose_a_day_for_the_free_seminar', 'full_name', 'phone',
    'lead_status', 'email', 'page_id', 'received_at', 'status', 'error', 'raw_data',
]

_worksheet_cache = None


def _norm(value: str) -> str:
    text = str(value or '').strip().lower()
    for ch in [' ', '-', '.', '/', '\n', '\t']:
        text = text.replace(ch, '_')
    for ch in ['?', ':', ';', ',', '(', ')', '[', ']', '{', '}', '!', "'", '"']:
        text = text.replace(ch, '')
    while '__' in text:
        text = text.replace('__', '_')
    return text.strip('_')


def _as_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (dict, list)):
        return str(value)
    return str(value)


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
        worksheet.append_row(DEFAULT_HEADERS, value_input_option='RAW')

    existing_headers: List[str] = worksheet.row_values(1)
    if not existing_headers:
        worksheet.append_row(DEFAULT_HEADERS, value_input_option='RAW')

    _worksheet_cache = worksheet
    return worksheet


def lead_already_exists(worksheet, lead_id: str) -> bool:
    """Prevent duplicate rows when Meta retries a webhook or you press test repeatedly."""
    if not lead_id:
        return False
    try:
        existing_ids = worksheet.col_values(1)
        return str(lead_id) in {str(x).strip() for x in existing_ids[1:]}
    except Exception as exc:
        print('Duplicate check failed, will append anyway:', exc, flush=True)
        return False


def append_lead_to_sheet(row: Dict[str, Any]) -> bool:
    worksheet = get_worksheet()
    headers = worksheet.row_values(1)
    if not headers:
        headers = DEFAULT_HEADERS
        worksheet.append_row(headers, value_input_option='RAW')

    lead_id = _as_text(row.get('id') or row.get('lead_id') or '')
    if lead_id and lead_already_exists(worksheet, lead_id):
        print(f'Skipping duplicate lead_id already in sheet: {lead_id}', flush=True)
        return False

    # Build lookup with normalized keys so headers with/without ? still match.
    normalized_row = {}
    for k, v in row.items():
        normalized_row[_norm(k)] = v
        normalized_row[str(k).strip().lower()] = v

    values = []
    for header in headers:
        direct_key = str(header).strip().lower()
        norm_key = _norm(header)
        value = normalized_row.get(direct_key, normalized_row.get(norm_key, ''))
        values.append(_as_text(value))

    print('Appending row to Google Sheet:', values, flush=True)
    worksheet.append_row(
        values,
        value_input_option='RAW',  # prevents long IDs/phone numbers becoming 1.36E+15
        insert_data_option='INSERT_ROWS',
    )
    return True
