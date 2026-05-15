import base64
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
TMP_DIR = Path('/tmp')


def get_env(name: str, default: str = '') -> str:
    return os.getenv(name, default).strip()


META_VERIFY_TOKEN = get_env('META_VERIFY_TOKEN', 'woi_leads_verify_123')
META_GRAPH_VERSION = get_env('META_GRAPH_VERSION', 'v22.0')
META_PAGE_ACCESS_TOKEN = get_env('META_PAGE_ACCESS_TOKEN')
GOOGLE_SHEET_ID = get_env('GOOGLE_SHEET_ID')
GOOGLE_SERVICE_ACCOUNT_JSON = get_env('GOOGLE_SERVICE_ACCOUNT_JSON', str(BASE_DIR / 'service_account.json'))
GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 = get_env('GOOGLE_SERVICE_ACCOUNT_JSON_BASE64')


def get_service_account_file() -> str:
    """
    Local: use service_account.json file.
    Render: use GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 env var and write it to /tmp.
    """
    if GOOGLE_SERVICE_ACCOUNT_JSON_BASE64:
        output_path = TMP_DIR / 'service_account.json'
        try:
            decoded = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_BASE64)
            output_path.write_bytes(decoded)
            return str(output_path)
        except Exception as exc:
            raise RuntimeError(f'Invalid GOOGLE_SERVICE_ACCOUNT_JSON_BASE64: {exc}') from exc

    return GOOGLE_SERVICE_ACCOUNT_JSON

# WhatsApp Cloud API settings. Keep WHATSAPP_ENABLED=false until your real number,
# permanent token, and approved template are ready.
WHATSAPP_ENABLED = get_env('WHATSAPP_ENABLED', 'false').lower() in ('1', 'true', 'yes', 'on')
WHATSAPP_PHONE_NUMBER_ID = get_env('WHATSAPP_PHONE_NUMBER_ID')
WHATSAPP_ACCESS_TOKEN = get_env('WHATSAPP_ACCESS_TOKEN') or META_PAGE_ACCESS_TOKEN
WHATSAPP_TEMPLATE_NAME = get_env('WHATSAPP_TEMPLATE_NAME', 'hello_world')
WHATSAPP_LANGUAGE_CODE = get_env('WHATSAPP_LANGUAGE_CODE', 'en_US')
# Comma-separated template variable fields. Example: name or name,course
# Leave blank for templates with no body variables.
WHATSAPP_TEMPLATE_BODY_FIELDS = get_env('WHATSAPP_TEMPLATE_BODY_FIELDS', '')
