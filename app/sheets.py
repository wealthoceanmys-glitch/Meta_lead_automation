from typing import Dict, Any, List

import gspread
from google.oauth2.service_account import Credentials

from app.config import GOOGLE_SHEET_ID, get_service_account_file

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

HEADERS = [
    'Received At', 'Lead ID', 'Created Time', 'Name', 'Phone', 'Email',
    'City', 'Course', 'Form ID', 'Page ID', 'Ad ID', 'Campaign ID', 'Raw Data',
]

_worksheet_cache = None


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
        worksheet = spreadsheet.add_worksheet(title='Leads', rows=1000, cols=20)
        worksheet.append_row(HEADERS)
    existing_headers: List[str] = worksheet.row_values(1)
    if not existing_headers:
        worksheet.append_row(HEADERS)
    _worksheet_cache = worksheet
    return worksheet


def append_lead_to_sheet(row: Dict[str, Any]):
    worksheet = get_worksheet()
    worksheet.append_row(
        [
            row.get('received_at', ''), row.get('lead_id', ''), row.get('created_time', ''),
            row.get('name', ''), row.get('phone', ''), row.get('email', ''),
            row.get('city', ''), row.get('course', ''), row.get('form_id', ''),
            row.get('page_id', ''), row.get('ad_id', ''), row.get('campaign_id', ''),
            row.get('raw_data', ''),
        ],
        value_input_option='USER_ENTERED',
        insert_data_option='INSERT_ROWS',
    )
