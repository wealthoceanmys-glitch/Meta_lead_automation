import json
from datetime import datetime, timezone
from typing import Dict, Any, List

import requests

from app.config import META_GRAPH_VERSION, META_PAGE_ACCESS_TOKEN
from app.sheets import append_lead_to_sheet


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_key(value: str) -> str:
    """Normalize Meta field names and Google Sheet headers the same way.

    Example:
    'What is your current experience level?' -> 'what_is_your_current_experience_level'
    'phone-number' -> 'phone_number'
    """
    text = str(value or '').strip().lower()
    for ch in [' ', '-', '.', '/', '\n', '\t']:
        text = text.replace(ch, '_')
    # Remove common punctuation that appears in question-style field names.
    for ch in ['?', ':', ';', ',', '(', ')', '[', ']', '{', '}', '!', "'", '"']:
        text = text.replace(ch, '')
    while '__' in text:
        text = text.replace('__', '_')
    return text.strip('_')


def field_value(item: Dict[str, Any]) -> str:
    values = item.get('values') or []
    if isinstance(values, list):
        return ', '.join(str(v) for v in values if v is not None)
    return str(values or '')


def get_field(field_data: List[Dict[str, Any]], possible_names: List[str]) -> str:
    normalized = {normalize_key(x) for x in possible_names}
    for item in field_data or []:
        name = normalize_key(item.get('name', ''))
        value = field_value(item)
        if name in normalized and value:
            return value
    return ''


def normalize_phone(phone: str) -> str:
    if not phone:
        return ''
    digits = ''.join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) == 10:
        return '91' + digits
    if digits.startswith('91') and len(digits) == 12:
        return digits
    return digits


def fetch_lead_details(leadgen_id: str) -> Dict[str, Any]:
    if not META_PAGE_ACCESS_TOKEN:
        raise RuntimeError('META_PAGE_ACCESS_TOKEN is missing')

    url = f'https://graph.facebook.com/{META_GRAPH_VERSION}/{leadgen_id}'

    # Keep this list conservative. Meta Lead objects do NOT support fields like
    # form_name or page_id directly, and requesting unsupported fields makes the
    # entire Graph API call fail with (#100). Page/form IDs are already present
    # in the webhook payload, so we merge those in map_lead_to_row().
    safe_fields = 'id,created_time,field_data,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,form_id,is_organic,platform'
    minimum_fields = 'id,created_time,field_data,ad_id,form_id,campaign_id'

    last_response = None
    for fields in (safe_fields, minimum_fields):
        params = {'access_token': META_PAGE_ACCESS_TOKEN, 'fields': fields}
        response = requests.get(url, params=params, timeout=25)
        last_response = response
        print('Meta fetch lead response:', response.status_code, response.text, flush=True)
        if response.ok:
            return response.json()

    # Last response failed. Raise with useful Meta text.
    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError('Meta lead fetch failed before response was created')


def map_lead_to_row(lead: Dict[str, Any], webhook_value: Dict[str, Any] | None = None) -> Dict[str, Any]:
    webhook_value = webhook_value or {}
    field_data = lead.get('field_data', []) or []

    # These common fields are used across different Meta lead form templates.
    full_name = get_field(field_data, [
        'full_name', 'name', 'first_name', 'customer_name', 'your_name',
        'what is your full name?', 'full name'
    ])
    phone = get_field(field_data, [
        'phone_number', 'phone', 'mobile_number', 'mobile', 'whatsapp_number',
        'phone number', 'mobile number', 'whatsapp number'
    ])
    email = get_field(field_data, ['email', 'email_address', 'email address'])

    row: Dict[str, Any] = {
        'id': lead.get('id') or webhook_value.get('leadgen_id', ''),
        'lead_id': lead.get('id') or webhook_value.get('leadgen_id', ''),
        'created_time': lead.get('created_time') or webhook_value.get('created_time', ''),
        'ad_id': lead.get('ad_id') or webhook_value.get('ad_id', ''),
        'ad_name': lead.get('ad_name', ''),
        'adset_id': lead.get('adset_id') or webhook_value.get('adset_id', '') or webhook_value.get('adgroup_id', ''),
        'adset_name': lead.get('adset_name', ''),
        'campaign_id': lead.get('campaign_id') or webhook_value.get('campaign_id', ''),
        'campaign_name': lead.get('campaign_name', ''),
        'form_id': lead.get('form_id') or webhook_value.get('form_id', ''),
        'form_name': lead.get('form_name', ''),
        'is_organic': lead.get('is_organic', ''),
        'platform': lead.get('platform', ''),
        'full_name': full_name,
        'name': full_name,
        'phone': normalize_phone(phone),
        'phone_number': normalize_phone(phone),
        'email': email,
        'page_id': lead.get('page_id') or webhook_value.get('page_id', ''),
        'received_at': now_iso(),
        'lead_status': 'new',
        'status': 'processed',
        'error': '',
        'raw_data': json.dumps(lead, ensure_ascii=False),
    }

    # Add EVERY Meta form question into the row dictionary.
    # Because sheets.py writes by matching headers, custom Sheet columns like:
    #   what_is_your_current_experience_level?
    #   please_choose_a_day_for_the_free_seminar
    # will fill automatically if Meta sends fields with the same/similar names.
    for item in field_data:
        original_name = str(item.get('name', '')).strip()
        if not original_name:
            continue
        value = field_value(item)
        row[original_name] = value
        row[normalize_key(original_name)] = value

    # Explicit aliases for your current Google Sheet columns.
    row['what_is_your_current_experience_level?'] = get_field(field_data, [
        'what_is_your_current_experience_level?',
        'what_is_your_current_experience_level',
        'what is your current experience level?',
        'current experience level',
        'experience level',
    ])
    row['what_is_your_current_experience_level'] = row['what_is_your_current_experience_level?']

    row['please_choose_a_day_for_the_free_seminar'] = get_field(field_data, [
        'please_choose_a_day_for_the_free_seminar',
        'please choose a day for the free seminar',
        'please choose a day for the free seminar?',
        'seminar day',
        'free seminar day',
    ])

    return row


def extract_leadgen_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return webhook value objects containing leadgen_id.

    Supports:
    1. Real Meta lead webhook: entry[] -> changes[] -> value.leadgen_id
    2. Meta dashboard webhook test: sample.value.leadgen_id
    """
    items: List[Dict[str, Any]] = []

    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value', {}) or {}
            if value.get('leadgen_id'):
                items.append(value)

    sample = payload.get('sample', {}) or {}
    sample_value = sample.get('value', {}) or {}
    if sample_value.get('leadgen_id'):
        print('Detected Meta dashboard test sample payload:', sample_value, flush=True)
        items.append(sample_value)

    return items


def subscribe_page_to_app(page_id: str) -> bool:
    if not META_PAGE_ACCESS_TOKEN:
        print('WARNING: META_PAGE_ACCESS_TOKEN missing — cannot subscribe page', flush=True)
        return False
    url = f'https://graph.facebook.com/{META_GRAPH_VERSION}/{page_id}/subscribed_apps'
    params = {'access_token': META_PAGE_ACCESS_TOKEN, 'subscribed_fields': 'leadgen'}
    try:
        response = requests.post(url, params=params, timeout=15)
        print(f'Page subscription [{page_id}]:', response.status_code, response.text, flush=True)
        return response.ok
    except Exception as exc:
        print('Page subscription error:', exc, flush=True)
        return False


def process_leadgen_payload(payload: Dict[str, Any]) -> List[str]:
    processed: List[str] = []
    items = extract_leadgen_items(payload)
    print('Incoming leadgen items:', items, flush=True)

    if not items:
        print('WARNING: No leadgen_id found in payload:', json.dumps(payload), flush=True)
        append_lead_to_sheet({
            'id': '',
            'received_at': now_iso(),
            'status': 'no_leadgen_id',
            'error': 'No leadgen_id found in webhook payload',
            'raw_data': json.dumps(payload, ensure_ascii=False),
        })
        return processed

    for item in items:
        leadgen_id = str(item.get('leadgen_id', '')).strip()
        try:
            # Meta dashboard webhook test uses all-4 dummy IDs. Write a debug row
            # instead of silently skipping, so you can confirm Sheet connectivity.
            if leadgen_id and set(leadgen_id) == {'4'}:
                row = map_lead_to_row({'id': leadgen_id, 'field_data': []}, item)
                row['status'] = 'dashboard_test_dummy'
                row['error'] = 'Dummy leadgen_id from Meta Webhooks dashboard test. Real lead fetch skipped.'
                row['raw_data'] = json.dumps(item, ensure_ascii=False)
                append_lead_to_sheet(row)
                processed.append(f'dashboard-test:{leadgen_id}')
                continue

            lead = fetch_lead_details(leadgen_id)
            row = map_lead_to_row(lead, item)
            append_lead_to_sheet(row)
            processed.append(row.get('id', leadgen_id))
        except Exception as exc:
            error_text = str(exc)
            print(f'ERROR processing leadgen_id {leadgen_id}: {error_text}', flush=True)
            # Very important: write the error to the sheet so failures are visible.
            append_lead_to_sheet({
                'id': leadgen_id,
                'lead_id': leadgen_id,
                'created_time': item.get('created_time', ''),
                'ad_id': item.get('ad_id', ''),
                'form_id': item.get('form_id', ''),
                'page_id': item.get('page_id', ''),
                'received_at': now_iso(),
                'status': 'error',
                'error': error_text,
                'raw_data': json.dumps({'webhook_value': item}, ensure_ascii=False),
            })
            processed.append(f'error:{leadgen_id}')

    return processed
