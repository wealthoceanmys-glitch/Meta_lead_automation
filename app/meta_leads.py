import json
from datetime import datetime, timezone
from typing import Dict, Any, List

import requests

from app.config import META_GRAPH_VERSION, META_PAGE_ACCESS_TOKEN
from app.sheets import append_lead_to_sheet


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_field(field_data: List[Dict[str, Any]], possible_names: List[str]) -> str:
    normalized = {x.lower().strip() for x in possible_names}
    for item in field_data or []:
        name = str(item.get('name', '')).lower().strip()
        values = item.get('values') or []
        if name in normalized and values:
            return str(values[0])
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

    # Rich field list first. Some accounts/tokens may reject a field. If so,
    # fallback to the minimum field list instead of losing the whole lead.
    rich_fields = (
        'id,created_time,field_data,ad_id,ad_name,adset_id,adset_name,'
        'campaign_id,campaign_name,form_id,form_name,page_id,is_organic,platform'
    )
    basic_fields = 'id,created_time,field_data,ad_id,form_id,campaign_id,page_id'

    for fields in (rich_fields, basic_fields):
        params = {'access_token': META_PAGE_ACCESS_TOKEN, 'fields': fields}
        response = requests.get(url, params=params, timeout=25)
        print('Meta fetch lead response:', response.status_code, response.text, flush=True)
        if response.ok:
            return response.json()

    # Last response failed. Raise with useful Meta text.
    response.raise_for_status()
    return response.json()


def map_lead_to_row(lead: Dict[str, Any], webhook_value: Dict[str, Any] | None = None) -> Dict[str, Any]:
    webhook_value = webhook_value or {}
    field_data = lead.get('field_data', [])

    full_name = get_field(field_data, ['full_name', 'name', 'first_name', 'customer_name', 'your_name'])
    phone = get_field(field_data, ['phone_number', 'phone', 'mobile_number', 'mobile', 'whatsapp_number'])
    email = get_field(field_data, ['email', 'email_address'])
    city = get_field(field_data, ['city', 'location', 'place'])
    course = get_field(field_data, ['course', 'interested_course', 'program', 'service', 'interested_in'])

    return {
        'id': lead.get('id') or webhook_value.get('leadgen_id', ''),
        'lead_id': lead.get('id') or webhook_value.get('leadgen_id', ''),
        'created_time': lead.get('created_time') or webhook_value.get('created_time', ''),
        'ad_id': lead.get('ad_id') or webhook_value.get('ad_id', ''),
        'ad_name': lead.get('ad_name', ''),
        'adset_id': lead.get('adset_id') or webhook_value.get('adset_id', ''),
        'adset_name': lead.get('adset_name', ''),
        'campaign_id': lead.get('campaign_id') or webhook_value.get('campaign_id', ''),
        'campaign_name': lead.get('campaign_name', ''),
        'form_id': lead.get('form_id') or webhook_value.get('form_id', ''),
        'form_name': lead.get('form_name', ''),
        'is_organic': lead.get('is_organic', ''),
        'platform': lead.get('platform', ''),
        'full_name': full_name,
        'name': full_name,
        'phone_number': normalize_phone(phone),
        'phone': normalize_phone(phone),
        'email': email,
        'city': city,
        'course': course,
        'page_id': lead.get('page_id') or webhook_value.get('page_id', ''),
        'received_at': now_iso(),
        'status': 'processed',
        'error': '',
        'raw_data': json.dumps(lead, ensure_ascii=False),
    }


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
