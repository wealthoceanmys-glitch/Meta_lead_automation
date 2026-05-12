import json
from datetime import datetime, timezone
from typing import Dict, Any, List

import requests

from app.config import META_GRAPH_VERSION, META_PAGE_ACCESS_TOKEN
from app.sheets import append_lead_to_sheet


def get_field(field_data: List[Dict[str, Any]], possible_names: List[str]) -> str:
    normalized = {x.lower().strip() for x in possible_names}
    for item in field_data:
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
    params = {
        'access_token': META_PAGE_ACCESS_TOKEN,
        'fields': 'id,created_time,field_data,ad_id,form_id,campaign_id,page_id',
    }
    response = requests.get(url, params=params, timeout=25)
    print('Meta fetch lead response:', response.status_code, response.text)
    response.raise_for_status()
    return response.json()


def map_lead_to_row(lead: Dict[str, Any]) -> Dict[str, Any]:
    field_data = lead.get('field_data', [])
    name = get_field(field_data, ['full_name', 'name', 'first_name', 'customer_name', 'your_name'])
    phone = get_field(field_data, ['phone_number', 'phone', 'mobile_number', 'mobile', 'whatsapp_number'])
    email = get_field(field_data, ['email', 'email_address'])
    city = get_field(field_data, ['city', 'location', 'place'])
    course = get_field(field_data, ['course', 'interested_course', 'program', 'service', 'interested_in'])
    return {
        'received_at': datetime.now(timezone.utc).isoformat(),
        'lead_id': lead.get('id', ''),
        'created_time': lead.get('created_time', ''),
        'name': name or 'Customer',
        'phone': normalize_phone(phone),
        'email': email,
        'city': city,
        'course': course,
        'form_id': lead.get('form_id', ''),
        'page_id': lead.get('page_id', ''),
        'ad_id': lead.get('ad_id', ''),
        'campaign_id': lead.get('campaign_id', ''),
        'raw_data': json.dumps(lead, ensure_ascii=False),
    }


def extract_leadgen_ids(payload: Dict[str, Any]) -> List[str]:
    """
    Handles both formats:
    1. Production: entry[] -> changes[] -> value.leadgen_id
    2. Meta test tool: sample.value.leadgen_id
    """
    leadgen_ids: List[str] = []

    # Format 1: Real production webhook
    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value', {})
            leadgen_id = value.get('leadgen_id')
            if leadgen_id:
                leadgen_ids.append(str(leadgen_id))

    # Format 2: Meta test tool sends a 'sample' wrapper
    sample = payload.get('sample', {})
    if sample:
        sample_value = sample.get('value', {})
        leadgen_id = sample_value.get('leadgen_id')
        if leadgen_id:
            print('Detected Meta test tool sample payload, leadgen_id:', leadgen_id)
            leadgen_ids.append(str(leadgen_id))

    return leadgen_ids


def subscribe_page_to_app(page_id: str) -> bool:
    """Subscribe the Facebook page to receive leadgen events via this app."""
    if not META_PAGE_ACCESS_TOKEN:
        print('WARNING: META_PAGE_ACCESS_TOKEN missing — cannot subscribe page')
        return False
    url = f'https://graph.facebook.com/{META_GRAPH_VERSION}/{page_id}/subscribed_apps'
    params = {
        'access_token': META_PAGE_ACCESS_TOKEN,
        'subscribed_fields': 'leadgen',
    }
    try:
        response = requests.post(url, params=params, timeout=15)
        print(f'Page subscription [{page_id}]:', response.status_code, response.text)
        return response.ok
    except Exception as exc:
        print('Page subscription error:', exc)
        return False


def process_leadgen_payload(payload: Dict[str, Any]) -> List[str]:
    processed: List[str] = []
    leadgen_ids = extract_leadgen_ids(payload)
    print('Incoming leadgen IDs:', leadgen_ids)

    if not leadgen_ids:
        print('WARNING: No leadgen_id found in payload:', json.dumps(payload))
        return processed

    for leadgen_id in leadgen_ids:
        # Skip dummy test IDs (all 4s) — Meta test tool uses fake IDs
        if set(leadgen_id) == {'4'}:
            print(f'Skipping dummy test leadgen_id: {leadgen_id}')
            processed.append(f'test-skipped:{leadgen_id}')
            continue
        lead = fetch_lead_details(leadgen_id)
        row = map_lead_to_row(lead)
        append_lead_to_sheet(row)
        processed.append(row.get('lead_id', leadgen_id))

    return processed 
