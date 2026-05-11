from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import META_VERIFY_TOKEN, get_env
from app.meta_leads import process_leadgen_payload, normalize_phone, subscribe_page_to_app
from app.sheets import append_lead_to_sheet

META_PAGE_ID = get_env('META_PAGE_ID')  # Add this to Render env vars


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Subscribe page to app on startup so webhooks are routed correctly
    if META_PAGE_ID:
        print(f'Subscribing page {META_PAGE_ID} to leadgen webhooks...')
        ok = subscribe_page_to_app(META_PAGE_ID)
        print('Page subscription result:', ok)
    else:
        print('WARNING: META_PAGE_ID not set — skipping page subscription')
    yield


app = FastAPI(title='Meta Leads to Google Sheets', version='1.1.0', lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.get('/')
def health():
    return {
        'status': 'ok',
        'service': 'meta-leads-to-google-sheets',
    }


@app.get('/webhook/meta-leads')
async def verify_meta_webhook(request: Request):
    params = dict(request.query_params)
    mode = params.get('hub.mode')
    token = params.get('hub.verify_token')
    challenge = params.get('hub.challenge')

    if mode == 'subscribe' and token == META_VERIFY_TOKEN and challenge:
        try:
            return int(challenge)
        except ValueError:
            return challenge

    raise HTTPException(status_code=403, detail='Webhook verification failed')


@app.post('/webhook/meta-leads')
async def receive_meta_lead(request: Request):
    payload: Dict[str, Any] = await request.json()
    print('Meta webhook payload:', payload)

    try:
        processed = process_leadgen_payload(payload)
        return {
            'success': True,
            'processed': processed,
        }
    except Exception as exc:
        print('Lead webhook error:', str(exc))
        return {
            'success': False,
            'error': str(exc),
        }


@app.post('/test/sheet')
async def test_sheet(payload: Dict[str, Any]):
    row = {
        'received_at': datetime.now(timezone.utc).isoformat(),
        'lead_id': 'manual-test',
        'created_time': datetime.now(timezone.utc).isoformat(),
        'name': payload.get('name', 'Test Customer'),
        'phone': normalize_phone(payload.get('phone', '')),
        'email': payload.get('email', ''),
        'city': payload.get('city', ''),
        'course': payload.get('course', ''),
        'form_id': payload.get('form_id', 'manual'),
        'page_id': payload.get('page_id', 'manual'),
        'ad_id': payload.get('ad_id', 'manual'),
        'campaign_id': payload.get('campaign_id', 'manual'),
        'raw_data': str(payload),
    }
    append_lead_to_sheet(row)
    return {
        'success': True,
        'message': 'Test row added to Google Sheet',
        'row': row,
    }
