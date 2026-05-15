import json
from typing import Any, Dict, List, Optional

import requests

from app.config import (
    META_GRAPH_VERSION,
    WHATSAPP_ACCESS_TOKEN,
    WHATSAPP_ENABLED,
    WHATSAPP_LANGUAGE_CODE,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_TEMPLATE_NAME,
)


def whatsapp_config_status() -> Dict[str, Any]:
    return {
        'whatsapp_enabled': WHATSAPP_ENABLED,
        'phone_number_id_present': bool(WHATSAPP_PHONE_NUMBER_ID),
        'access_token_present': bool(WHATSAPP_ACCESS_TOKEN),
        'template_name': WHATSAPP_TEMPLATE_NAME,
        'language_code': WHATSAPP_LANGUAGE_CODE,
    }


def _digits_only(value: str) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


def normalize_whatsapp_to(phone: str) -> str:
    """WhatsApp Cloud API expects country code + phone number without '+'."""
    digits = _digits_only(phone)
    if not digits:
        return ''
    if len(digits) == 10:
        return '91' + digits
    if digits.startswith('0') and len(digits) == 11:
        return '91' + digits[-10:]
    return digits


def _template_components(name: str = '', lead: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Build template body parameters.

    Keep this flexible:
    - If your approved template has no {{1}}, leave WHATSAPP_TEMPLATE_BODY_FIELDS empty.
    - If your template has {{1}} for name, set WHATSAPP_TEMPLATE_BODY_FIELDS=name.
    - For multiple params, set comma list such as: name,course,city
    """
    from app.config import WHATSAPP_TEMPLATE_BODY_FIELDS

    fields = [x.strip() for x in WHATSAPP_TEMPLATE_BODY_FIELDS.split(',') if x.strip()]
    if not fields:
        return []

    lead = lead or {}
    params = []
    for field in fields:
        key = field.lower()
        if key in ('name', 'full_name', 'customer_name'):
            value = name or lead.get('name') or lead.get('full_name') or 'there'
        else:
            value = lead.get(field) or lead.get(key) or ''
        params.append({'type': 'text', 'text': str(value)})

    return [{'type': 'body', 'parameters': params}] if params else []


def send_whatsapp_template(phone: str, name: str = '', lead: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not WHATSAPP_ENABLED:
        return {'skipped': True, 'reason': 'WHATSAPP_ENABLED is false'}
    if not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError('WHATSAPP_PHONE_NUMBER_ID is missing')
    if not WHATSAPP_ACCESS_TOKEN:
        raise RuntimeError('WHATSAPP_ACCESS_TOKEN is missing')
    if not WHATSAPP_TEMPLATE_NAME:
        raise RuntimeError('WHATSAPP_TEMPLATE_NAME is missing')

    to_number = normalize_whatsapp_to(phone)
    if not to_number:
        raise RuntimeError('Lead phone number is missing, WhatsApp message not sent')

    url = f'https://graph.facebook.com/{META_GRAPH_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages'
    payload: Dict[str, Any] = {
        'messaging_product': 'whatsapp',
        'to': to_number,
        'type': 'template',
        'template': {
            'name': WHATSAPP_TEMPLATE_NAME,
            'language': {'code': WHATSAPP_LANGUAGE_CODE},
        },
    }

    components = _template_components(name=name, lead=lead)
    if components:
        payload['template']['components'] = components

    headers = {
        'Authorization': f'Bearer {WHATSAPP_ACCESS_TOKEN}',
        'Content-Type': 'application/json',
    }

    response = requests.post(url, headers=headers, json=payload, timeout=25)
    print('WhatsApp send request:', json.dumps(payload, ensure_ascii=False), flush=True)
    print('WhatsApp send response:', response.status_code, response.text, flush=True)

    try:
        data = response.json()
    except Exception:
        data = {'raw_response': response.text}

    if not response.ok:
        raise RuntimeError(f'WhatsApp API error {response.status_code}: {data}')

    return data
