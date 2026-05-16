import json
import os
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from app.config import (
    META_GRAPH_VERSION,
    WHATSAPP_ACCESS_TOKEN,
    WHATSAPP_ENABLED,
    WHATSAPP_LANGUAGE_CODE,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_TEMPLATE_NAME,
)

# Seminar automation defaults
# Thursday evening: 6:00 PM to 8:00 PM
# Sunday morning: 10:30 AM to 12:30 PM
SEMINAR_TIMEZONE = os.getenv('SEMINAR_TIMEZONE', 'Asia/Kolkata').strip() or 'Asia/Kolkata'
SEMINAR_VENUE = os.getenv('SEMINAR_VENUE', 'Kuvempunagara, Mysuru - https://g.co/kgs/FDbcqh').strip()
SEMINAR_THURSDAY_TIME = os.getenv('SEMINAR_THURSDAY_TIME', '6:00 PM to 8:00 PM').strip()
SEMINAR_THURSDAY_ARRIVAL = os.getenv('SEMINAR_THURSDAY_ARRIVAL', '5:45 PM').strip()
SEMINAR_SUNDAY_TIME = os.getenv('SEMINAR_SUNDAY_TIME', '10:30 AM to 12:30 PM').strip()
SEMINAR_SUNDAY_ARRIVAL = os.getenv('SEMINAR_SUNDAY_ARRIVAL', '10:15 AM').strip()

IST = ZoneInfo(SEMINAR_TIMEZONE)

SEMINAR_SCHEDULES = {
    'thursday': {
        'weekday': 3,  # Monday=0, Thursday=3
        'start_time': time(18, 0),
        'display_time': SEMINAR_THURSDAY_TIME,
        'arrival_time': SEMINAR_THURSDAY_ARRIVAL,
    },
    'sunday': {
        'weekday': 6,  # Sunday=6
        'start_time': time(10, 30),
        'display_time': SEMINAR_SUNDAY_TIME,
        'arrival_time': SEMINAR_SUNDAY_ARRIVAL,
    },
}

DEFAULT_SEMINAR_FIELDS = [
    'customer_name',
    'seminar_date',
    'seminar_time',
    'arrival_time',
    'venue',
]


def whatsapp_config_status() -> Dict[str, Any]:
    return {
        'whatsapp_enabled': WHATSAPP_ENABLED,
        'phone_number_id_present': bool(WHATSAPP_PHONE_NUMBER_ID),
        'access_token_present': bool(WHATSAPP_ACCESS_TOKEN),
        'template_name': WHATSAPP_TEMPLATE_NAME,
        'language_code': WHATSAPP_LANGUAGE_CODE,
        'seminar_timezone': SEMINAR_TIMEZONE,
        'seminar_venue': SEMINAR_VENUE,
        'seminar_thursday_time': SEMINAR_THURSDAY_TIME,
        'seminar_sunday_time': SEMINAR_SUNDAY_TIME,
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


def _next_weekday_date(target_weekday: int, event_start_time: time, now: Optional[datetime] = None):
    """Return next upcoming date for a weekday in IST.

    If today is the target weekday but current time has reached/crossed the
    event start time, this returns next week's date.
    """
    now = now or datetime.now(IST)
    days_ahead = target_weekday - now.weekday()
    if days_ahead < 0:
        days_ahead += 7
    if days_ahead == 0 and now.time() >= event_start_time:
        days_ahead = 7
    return now.date() + timedelta(days=days_ahead)


def _format_indian_date(date_obj) -> str:
    # Example: Sunday, 17 May 2026
    return date_obj.strftime('%A, %d %B %Y')


def _all_lead_text(lead: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key, value in (lead or {}).items():
        if value is None:
            continue
        parts.append(str(key).lower())
        parts.append(str(value).lower())
    return ' '.join(parts)


def detect_preferred_seminar_day(lead: Optional[Dict[str, Any]]) -> Optional[str]:
    """Detect Thursday/Sunday from Meta lead answers, ad name, campaign name, etc."""
    text = _all_lead_text(lead or {})

    # Explicit lead form column used in this backend.
    selected = str((lead or {}).get('please_choose_a_day_for_the_free_seminar', '')).lower()
    if selected:
        text = selected + ' ' + text

    thursday_tokens = ['thursday', 'thu', 'thur', 'thurs', 'ಗುರುವಾರ']
    sunday_tokens = ['sunday', 'sun', 'ಭಾನುವಾರ']

    if any(token in text for token in thursday_tokens):
        return 'thursday'
    if any(token in text for token in sunday_tokens):
        return 'sunday'
    return None


def _nearest_upcoming_seminar(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(IST)
    options: List[Dict[str, Any]] = []
    for seminar_day, cfg in SEMINAR_SCHEDULES.items():
        event_date = _next_weekday_date(cfg['weekday'], cfg['start_time'], now=now)
        event_datetime = datetime.combine(event_date, cfg['start_time'], tzinfo=IST)
        options.append({
            'seminar_day': seminar_day,
            'event_date': event_date,
            'event_datetime': event_datetime,
            'seminar_time': cfg['display_time'],
            'arrival_time': cfg['arrival_time'],
        })
    return min(options, key=lambda x: x['event_datetime'])


def get_seminar_details_for_lead(lead: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Return dynamic seminar variables for WhatsApp templates.

    If lead/ad/form text mentions Thursday or Sunday, it picks that next
    upcoming day. If nothing is mentioned, it picks the nearest upcoming seminar.
    """
    now = datetime.now(IST)
    preferred = detect_preferred_seminar_day(lead)

    if preferred in SEMINAR_SCHEDULES:
        cfg = SEMINAR_SCHEDULES[preferred]
        event_date = _next_weekday_date(cfg['weekday'], cfg['start_time'], now=now)
        seminar = {
            'seminar_day': preferred,
            'event_date': event_date,
            'seminar_time': cfg['display_time'],
            'arrival_time': cfg['arrival_time'],
        }
    else:
        seminar = _nearest_upcoming_seminar(now=now)

    return {
        'seminar_day': seminar['seminar_day'],
        'seminar_date': _format_indian_date(seminar['event_date']),
        'seminar_time': seminar['seminar_time'],
        'arrival_time': seminar['arrival_time'],
        'venue': SEMINAR_VENUE,
    }


def _get_lead_value(lead: Dict[str, Any], *keys: str, default: str = '') -> str:
    for key in keys:
        value = lead.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _template_fields() -> List[str]:
    """Fields to send as WhatsApp body parameters.

    WHATSAPP_TEMPLATE_BODY_FIELDS can override this. For the approved seminar
    registration template, the defaults are named params:
    customer_name, seminar_date, seminar_time, arrival_time, venue.
    """
    from app.config import WHATSAPP_TEMPLATE_BODY_FIELDS

    fields = [x.strip() for x in WHATSAPP_TEMPLATE_BODY_FIELDS.split(',') if x.strip()]
    if fields:
        return fields

    # hello_world and templates without variables should not receive components.
    if WHATSAPP_TEMPLATE_NAME == 'hello_world':
        return []

    # Current automation template uses these 5 named variables.
    return DEFAULT_SEMINAR_FIELDS


def _value_for_template_field(field: str, name: str, lead: Dict[str, Any], seminar: Dict[str, str]) -> str:
    key = field.strip()
    key_lower = key.lower()

    if key_lower in ('name', 'full_name', 'customer_name'):
        return name or _get_lead_value(lead, 'name', 'full_name', default='Customer')
    if key_lower == 'seminar_date':
        return seminar['seminar_date']
    if key_lower == 'seminar_time':
        return seminar['seminar_time']
    if key_lower == 'arrival_time':
        return seminar['arrival_time']
    if key_lower in ('venue', 'seminar_venue'):
        return seminar['venue']
    if key_lower == 'seminar_day':
        return seminar['seminar_day'].title()

    return _get_lead_value(lead, key, key_lower, default='')


def _template_components(name: str = '', lead: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Build WhatsApp template body parameters.

    Supports Meta named variables such as {{customer_name}}. If your template
    uses numbered variables instead, Meta usually ignores parameter_name, but if
    it complains, set WHATSAPP_TEMPLATE_BODY_FIELDS to match your template order
    and remove variable names in the template itself.
    """
    fields = _template_fields()
    if not fields:
        return []

    lead = lead or {}
    seminar = get_seminar_details_for_lead(lead)
    print('Selected seminar details:', json.dumps(seminar, ensure_ascii=False), flush=True)

    params: List[Dict[str, str]] = []
    for field in fields:
        value = _value_for_template_field(field, name=name, lead=lead, seminar=seminar)
        param: Dict[str, str] = {
            'type': 'text',
            'text': str(value),
        }

        # Your WhatsApp Manager template uses named variables, so this is needed.
        # It is harmless for the approved named-variable templates.
        if field.lower() not in ('name', 'full_name'):
            param['parameter_name'] = field
        else:
            param['parameter_name'] = 'customer_name'

        params.append(param)

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
