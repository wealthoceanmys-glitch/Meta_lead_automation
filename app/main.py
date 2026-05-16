import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, Any

import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware

from app.config import META_VERIFY_TOKEN, META_GRAPH_VERSION, META_PAGE_ACCESS_TOKEN, GOOGLE_SHEET_ID, get_env
from app.meta_leads import process_leadgen_payload, normalize_phone, subscribe_page_to_app
from app.sheets import append_lead_to_sheet
from app.whatsapp import whatsapp_config_status, send_whatsapp_template

META_PAGE_ID = get_env('META_PAGE_ID')


async def keep_alive():
    await asyncio.sleep(60)
    url = get_env('RENDER_EXTERNAL_URL', '')
    if not url:
        print('RENDER_EXTERNAL_URL not set — keep-alive disabled')
        return
    while True:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f'{url}/', timeout=10)
                print(f'Keep-alive ping: {r.status_code}')
        except Exception as e:
            print(f'Keep-alive ping failed: {e}')
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        if META_PAGE_ID:
            print(f'Subscribing page {META_PAGE_ID} to leadgen webhooks...')
            ok = subscribe_page_to_app(META_PAGE_ID)
            print('Page subscription result:', ok)
        else:
            print('WARNING: META_PAGE_ID not set — skipping page subscription')
    except Exception as exc:
        print('WARNING: Page subscription failed on startup (non-fatal):', exc)
    asyncio.create_task(keep_alive())
    yield


app = FastAPI(title='Meta Leads to Google Sheets', version='1.4.0', lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.get('/')
def health():
    return {'status': 'ok', 'service': 'meta-leads-to-google-sheets'}


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


def _process_in_background(payload: Dict[str, Any]):
    try:
        processed = process_leadgen_payload(payload)
        print('Background processing complete. Lead IDs:', processed, flush=True)
    except Exception as exc:
        print('Background lead processing error:', str(exc), flush=True)


@app.post('/webhook/meta-leads')
async def receive_meta_lead(request: Request, background_tasks: BackgroundTasks):
    payload: Dict[str, Any] = await request.json()
    print('========== META LEAD WEBHOOK HIT ========== ', flush=True)
    print(payload, flush=True)
    print('==========================================', flush=True)
    background_tasks.add_task(_process_in_background, payload)
    return {'success': True, 'message': 'Received'}




@app.get('/debug/config')
def debug_config():
    return {
        'meta_graph_version': META_GRAPH_VERSION,
        'meta_page_token_present': bool(META_PAGE_ACCESS_TOKEN),
        'meta_page_token_prefix': (META_PAGE_ACCESS_TOKEN[:12] + '...') if META_PAGE_ACCESS_TOKEN else '',
        'meta_page_id': META_PAGE_ID,
        'google_sheet_id_present': bool(GOOGLE_SHEET_ID),
        'google_sheet_id_prefix': (GOOGLE_SHEET_ID[:8] + '...') if GOOGLE_SHEET_ID else '',
        **whatsapp_config_status(),
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
    return {'success': True, 'message': 'Test row added to Google Sheet', 'row': row}


@app.post('/test/whatsapp')
async def test_whatsapp(payload: Dict[str, Any]):
    result = send_whatsapp_template(
        phone=payload.get('phone', ''),
        name=payload.get('name', ''),
        lead=payload,
    )
    return {'success': True, 'message': 'WhatsApp template send attempted', 'result': result}
