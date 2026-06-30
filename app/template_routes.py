"""
WhatsApp Template Management Routes
------------------------------------
POST   /templates/upload-media          Upload header media → get handle
POST   /templates                       Submit template to Meta for approval
GET    /templates                       List all templates (with cached status)
GET    /templates/{id}/status           Refresh status from Meta
DELETE /templates/{id}                  Delete template from Meta + DB
POST   /templates/{id}/send-bulk        Send template to contacts from CSV body
"""

import csv
import io
import json
import logging
import re
import requests
from typing import Any, Optional, List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import require_user
from .config import settings
from .db import get_db
from .models import WhatsAppTemplate, WhatsAppMessage
from .utils import now_iso, clean_phone

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/templates", tags=["templates"])

GRAPH = "https://graph.facebook.com"
GV = "v25.0"


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _wa_headers():
    return {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }


# Cache the WABA ID after first successful lookup
_WABA_ID_CACHE: str = ""


def _waba_id() -> str:
    """
    Return the WhatsApp Business Account ID.

    Resolution order:
    1. WHATSAPP_BUSINESS_ACCOUNT_ID env var (fastest)
    2. Derived from WHATSAPP_PHONE_NUMBER_ID via Graph API (auto)
    3. Derived from META_PAGE_ACCESS_TOKEN phone number list

    The result is cached in memory for the lifetime of the process.
    """
    global _WABA_ID_CACHE

    if _WABA_ID_CACHE:
        return _WABA_ID_CACHE

    # 1. Explicit env var
    waba = (getattr(settings, "whatsapp_business_account_id", None) or "").strip()
    if waba:
        _WABA_ID_CACHE = waba
        return waba

    token = (settings.whatsapp_access_token or "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="WHATSAPP_ACCESS_TOKEN is not set.")

    phone_id = (settings.whatsapp_phone_number_id or "").strip()

    # 2. Look up via phone number ID → whatsapp_business_account
    if phone_id:
        r = requests.get(
            f"{GRAPH}/{GV}/{phone_id}",
            headers=_wa_headers(),
            params={"fields": "whatsapp_business_account"},
            timeout=15,
        )
        if r.status_code == 200:
            waba = r.json().get("whatsapp_business_account", {}).get("id", "")
            if waba:
                _WABA_ID_CACHE = waba
                logger.info("Auto-resolved WABA ID from phone number: %s", waba)
                return waba

    # 3. Look up via token's own business account list
    r2 = requests.get(
        f"{GRAPH}/{GV}/me/businesses",
        headers=_wa_headers(),
        timeout=15,
    )
    if r2.status_code == 200:
        biz_list = r2.json().get("data", [])
        if biz_list:
            # Pick first; each business may have multiple WABAs
            biz_id = biz_list[0].get("id", "")
            if biz_id:
                r3 = requests.get(
                    f"{GRAPH}/{GV}/{biz_id}/owned_whatsapp_business_accounts",
                    headers=_wa_headers(),
                    timeout=15,
                )
                if r3.status_code == 200:
                    waba_list = r3.json().get("data", [])
                    if waba_list:
                        waba = waba_list[0].get("id", "")
                        if waba:
                            _WABA_ID_CACHE = waba
                            logger.info("Auto-resolved WABA ID from business: %s", waba)
                            return waba

    raise HTTPException(
        status_code=503,
        detail=(
            "Cannot determine WhatsApp Business Account ID. "
            "Add WHATSAPP_BUSINESS_ACCOUNT_ID to your Render environment variables. "
            "Find it in Meta Business Manager → Business Settings → WhatsApp Accounts."
        ),
    )


def _build_meta_payload(t: "TemplateIn") -> dict:
    """Convert our TemplateIn schema into the Meta Graph API payload."""
    components = []

    # HEADER
    if t.header_type and t.header_type != "NONE":
        if t.header_type == "TEXT" and t.header_text:
            comp: dict = {
                "type": "HEADER",
                "format": "TEXT",
                "text": t.header_text,
            }
            # Header variables
            if t.header_variables:
                comp["example"] = {"header_text": t.header_variables}
            components.append(comp)
        elif t.header_type in ("IMAGE", "VIDEO", "DOCUMENT") and t.header_media_handle:
            comp = {
                "type": "HEADER",
                "format": t.header_type,
                "example": {"header_handle": [t.header_media_handle]},
            }
            components.append(comp)
        elif t.header_type == "LOCATION":
            components.append({"type": "HEADER", "format": "LOCATION"})

    # BODY
    body_comp: dict = {"type": "BODY", "text": t.body_text}
    if t.body_variables:
        body_comp["example"] = {"body_text": [t.body_variables]}
    components.append(body_comp)

    # FOOTER
    if t.footer_text:
        components.append({"type": "FOOTER", "text": t.footer_text})

    # BUTTONS
    if t.buttons:
        btn_list = []
        for b in t.buttons:
            if b.get("type") == "QUICK_REPLY":
                btn_list.append({"type": "QUICK_REPLY", "text": b["text"]})
            elif b.get("type") == "URL":
                btn_list.append({
                    "type": "URL",
                    "text": b["text"],
                    "url": b.get("url", ""),
                    **({"example": [b["url_example"]]} if b.get("url_example") else {}),
                })
            elif b.get("type") == "PHONE_NUMBER":
                btn_list.append({
                    "type": "PHONE_NUMBER",
                    "text": b["text"],
                    "phone_number": b.get("phone_number", ""),
                })
        if btn_list:
            components.append({"type": "BUTTONS", "buttons": btn_list})

    return {
        "name": t.name,
        "category": t.category,
        "language": t.language,
        "components": components,
    }


# ─────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────

class ButtonIn(BaseModel):
    type: str                          # QUICK_REPLY | URL | PHONE_NUMBER
    text: str
    url: Optional[str] = None          # for URL buttons
    url_example: Optional[str] = None  # filled URL example for approval
    phone_number: Optional[str] = None


class TemplateIn(BaseModel):
    name: str
    category: str = "MARKETING"        # MARKETING | UTILITY | AUTHENTICATION
    language: str = "en"

    header_type: Optional[str] = None  # TEXT | IMAGE | VIDEO | DOCUMENT | LOCATION | NONE
    header_text: Optional[str] = None
    header_variables: Optional[List[str]] = None
    header_media_handle: Optional[str] = None

    body_text: str
    body_variables: Optional[List[str]] = None  # example values for {{1}} {{2}} …

    footer_text: Optional[str] = None
    buttons: Optional[List[ButtonIn]] = None


class BulkContactIn(BaseModel):
    phone: str
    name: Optional[str] = None
    # extra variable values keyed by {{1}}, {{2}} or just list position
    variables: Optional[List[str]] = None


class BulkSendIn(BaseModel):
    contacts: List[BulkContactIn]
    variable_map: Optional[List[str]] = None   # column names from CSV mapped to {{1}}, {{2}} …


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

@router.post("/upload-media")
async def upload_header_media(
    file: UploadFile = File(...),
    user: str = Depends(require_user),
):
    """
    Upload image/video/document to Meta Resumable Upload API.
    Returns the media handle to use in header_media_handle.
    """
    content = await file.read()
    file_size = len(content)
    mime = file.content_type or "application/octet-stream"

    # Step 1: Create upload session
    session_url = f"{GRAPH}/{GV}/{_waba_id()}/uploads"
    r = requests.post(
        session_url,
        headers={
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        },
        json={
            "file_length": file_size,
            "file_type": mime,
            "file_name": file.filename,
        },
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=r.json())

    upload_session_id = r.json().get("id")
    if not upload_session_id:
        raise HTTPException(status_code=502, detail="No upload session id from Meta")

    # Step 2: Upload binary
    upload_url = f"{GRAPH}/{GV}/{upload_session_id}"
    r2 = requests.post(
        upload_url,
        headers={
            "Authorization": f"OAuth {settings.whatsapp_access_token}",
            "file_offset": "0",
            "Content-Type": mime,
        },
        data=content,
        timeout=60,
    )
    if r2.status_code not in (200, 201):
        raise HTTPException(status_code=r2.status_code, detail=r2.json())

    handle = r2.json().get("h")
    if not handle:
        raise HTTPException(status_code=502, detail="No handle returned from Meta upload")

    return {"handle": handle, "filename": file.filename, "size": file_size, "mime": mime}


@router.post("")
def create_template(
    data: TemplateIn,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """Submit a new WhatsApp message template to Meta for approval."""
    waba = _waba_id()
    payload = _build_meta_payload(data)

    r = requests.post(
        f"{GRAPH}/{GV}/{waba}/message_templates",
        headers=_wa_headers(),
        json=payload,
        timeout=30,
    )
    meta_data = r.json()
    logger.info("Meta template submit: %s %s", r.status_code, meta_data)

    meta_id = meta_data.get("id")
    status = meta_data.get("status", "PENDING")

    if r.status_code not in (200, 201) and not meta_id:
        raise HTTPException(
            status_code=r.status_code,
            detail=meta_data.get("error", {}).get("message", str(meta_data)),
        )

    tmpl = WhatsAppTemplate(
        meta_template_id=meta_id,
        name=data.name,
        category=data.category,
        language=data.language,
        status=status,
        header_type=data.header_type,
        header_text=data.header_text,
        header_media_handle=data.header_media_handle,
        body_text=data.body_text,
        body_variables=data.body_variables,
        footer_text=data.footer_text,
        buttons=[b.dict() for b in data.buttons] if data.buttons else None,
        meta_raw=meta_data,
    )
    db.add(tmpl)
    db.commit()
    db.refresh(tmpl)
    return _tmpl_out(tmpl)


@router.get("")
def list_templates(
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """List all templates stored locally."""
    templates = db.query(WhatsAppTemplate).order_by(WhatsAppTemplate.id.desc()).all()
    return [_tmpl_out(t) for t in templates]


@router.get("/{template_id}/status")
def refresh_template_status(
    template_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """Pull latest approval status from Meta and update the DB record."""
    tmpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if not tmpl.meta_template_id:
        raise HTTPException(status_code=400, detail="No Meta template ID stored")

    r = requests.get(
        f"{GRAPH}/{GV}/{tmpl.meta_template_id}",
        headers=_wa_headers(),
        params={"fields": "id,name,status,category,language,quality_score,rejected_reason,components"},
        timeout=20,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.json())

    meta_data = r.json()
    tmpl.status = meta_data.get("status", tmpl.status)
    tmpl.rejection_reason = meta_data.get("rejected_reason")
    tmpl.meta_raw = meta_data
    db.commit()
    db.refresh(tmpl)
    return _tmpl_out(tmpl)


@router.delete("/{template_id}")
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """Delete template from Meta and from local DB."""
    tmpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if tmpl.meta_template_id:
        waba = _waba_id()
        r = requests.delete(
            f"{GRAPH}/{GV}/{waba}/message_templates",
            headers=_wa_headers(),
            params={"hsm_id": tmpl.meta_template_id, "name": tmpl.name},
            timeout=20,
        )
        logger.info("Meta template delete: %s %s", r.status_code, r.text)

    db.delete(tmpl)
    db.commit()
    return {"ok": True, "deleted_id": template_id}


@router.post("/{template_id}/send-bulk")
def send_bulk(
    template_id: int,
    data: BulkSendIn,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Send an approved template to a list of contacts.
    Each contact can carry its own variable values.
    """
    tmpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if tmpl.status not in ("APPROVED", "approved"):
        raise HTTPException(status_code=400, detail=f"Template status is '{tmpl.status}', not APPROVED")

    url = f"{GRAPH}/{GV}/{settings.whatsapp_phone_number_id}/messages"

    # Log what we received to diagnose variable issues
    logger.info("SEND-BULK template=%s contacts=%d first_vars=%s body_text_preview=%s",
        tmpl.name,
        len(data.contacts),
        data.contacts[0].variables if data.contacts else "NO_CONTACTS",
        (tmpl.body_text or "")[:60]
    )

    results = []
    for contact in data.contacts:
        phone = clean_phone(contact.phone)
        if not phone:
            results.append({"phone": contact.phone, "ok": False, "error": "invalid phone"})
            continue

        components = _build_send_components(tmpl, contact)

        # Empty components list means a required variable had no value
        if components == [] and tmpl.body_text and re.search(r"\{\{[a-zA-Z_]", tmpl.body_text or ""):
            results.append({
                "phone": contact.phone,
                "name": contact.name,
                "ok": False,
                "wamid": None,
                "error": "One or more template variables are empty — check CSV column mapping",
            })
            continue

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": tmpl.name,
                "language": {"code": tmpl.language},
                "components": components,
            },
        }

        logger.info("Sending template payload: %s", payload)
        r = requests.post(url, json=payload, headers=_wa_headers(), timeout=20)
        try:
            resp = r.json()
        except Exception:
            resp = {"raw": r.text}

        ok = r.status_code in (200, 201)
        wamid = ((resp.get("messages") or [{}])[0]).get("id") if ok else None

        if ok and wamid:
            db.add(WhatsAppMessage(
                wa_message_id=wamid,
                phone=phone,
                contact_name=contact.name,
                direction="outgoing",
                message_type="template",
                body=f"[Template: {tmpl.name}]",
                status="accepted",
                raw=resp,
                timestamp=now_iso(),
            ))

        error_detail = None
        if not ok:
            err = resp.get("error", {})
            code = err.get("code", "")
            msg = err.get("message", "")
            error_sub = err.get("error_data", {}).get("details", "") if isinstance(err.get("error_data"), dict) else ""
            error_detail = f"(#{code}) {msg}" + (f" — {error_sub}" if error_sub else "")
            logger.error("Send failed phone=%s code=%s msg=%s payload=%s", phone, code, msg, payload)

        results.append({
            "phone": contact.phone,
            "name": contact.name,
            "ok": ok,
            "wamid": wamid,
            "error": error_detail,
        })

    db.commit()

    sent = sum(1 for r in results if r["ok"])
    return {"total": len(results), "sent": sent, "failed": len(results) - sent, "results": results}


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _build_send_components(tmpl: WhatsAppTemplate, contact: BulkContactIn) -> list:
    """Build the template components array for a single send call."""
    components = []
    variables = contact.variables or []

    # Header param
    # TEXT headers with variables: send the variable value
    # IMAGE/VIDEO/DOCUMENT headers: DO NOT send header component when sending messages
    # Meta uses the approved sample media automatically — sending CDN links causes (#100)
    if tmpl.header_type == "TEXT" and tmpl.header_text and "{{" in (tmpl.header_text or ""):
        param_val = variables[0] if variables else (contact.name or "")
        if param_val:
            components.append({
                "type": "header",
                "parameters": [{"type": "text", "text": str(param_val)}],
            })
    # For IMAGE/VIDEO/DOCUMENT: skip header component entirely
    # Meta automatically uses the approved sample image when no header component is sent

    # Body params
    # Meta stores body text as {{1}}, {{2}} positional even when named vars were used
    # body_variables may store the var names (e.g. ["event_time", "venue_name"])
    # variables list from CSV is already in correct positional order
    body_params = []
    numeric_placeholders = re.findall(r"\{\{(\d+)\}\}", tmpl.body_text or "")
    named_placeholders = re.findall(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}", tmpl.body_text or "")

    # Determine how many params are needed
    if numeric_placeholders:
        param_count = len(numeric_placeholders)
    elif named_placeholders:
        seen: set = set()
        param_count = len([n for n in named_placeholders if not (n in seen or seen.add(n))])
    else:
        param_count = 0

    for i in range(param_count):
        if i < len(variables) and variables[i]:
            val = str(variables[i])
        elif named_placeholders and i < len(named_placeholders):
            # fallback: use name for name-type variables
            vname = named_placeholders[i]
            if vname in ("name", "customer_name", "client_name", "client"):
                val = contact.name or ""
            else:
                val = ""
        else:
            val = contact.name if i == 0 else ""
        body_params.append({"type": "text", "text": str(val) if val else ""})

    if body_params:
        empty_params = [i+1 for i, p in enumerate(body_params) if not p["text"].strip()]
        if empty_params:
            logger.error("Empty params at positions %s for template %s", empty_params, tmpl.name)
            return []
        components.append({"type": "body", "parameters": body_params})

    return components


def _extract_header_media_url(meta_raw: dict) -> Optional[str]:
    """Pull the image/video/document URL from Meta's raw template response if present."""
    if not meta_raw:
        return None
    components = meta_raw.get("components", [])
    for comp in components:
        if comp.get("type", "").upper() == "HEADER":
            fmt = comp.get("format", "").upper()
            if fmt in ("IMAGE", "VIDEO", "DOCUMENT"):
                # Meta returns example.header_handle or example.header_url
                ex = comp.get("example", {})
                urls = ex.get("header_url", []) or ex.get("header_handle", [])
                if urls:
                    return urls[0]
    return None


def _tmpl_out(t: WhatsAppTemplate) -> dict:
    header_media_url = _extract_header_media_url(t.meta_raw or {})
    return {
        "id": t.id,
        "meta_template_id": t.meta_template_id,
        "name": t.name,
        "category": t.category,
        "language": t.language,
        "status": t.status,
        "header_type": t.header_type,
        "header_text": t.header_text,
        "header_media_handle": t.header_media_handle,
        "header_media_url": header_media_url,   # ← direct URL for preview
        "body_text": t.body_text,
        "body_variables": t.body_variables,
        "footer_text": t.footer_text,
        "buttons": t.buttons,
        "rejection_reason": t.rejection_reason,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }



@router.get("/debug")
def debug_template_config(user: str = Depends(require_user)):
    """
    Diagnoses token permissions and WABA access.
    Tests both WHATSAPP_ACCESS_TOKEN and META_PAGE_ACCESS_TOKEN.
    """
    wa_token  = (settings.whatsapp_access_token or "").strip()
    page_token = (settings.meta_page_access_token or "").strip()
    phone_id  = (settings.whatsapp_phone_number_id or "").strip()
    explicit_waba = (getattr(settings, "whatsapp_business_account_id", None) or "").strip()

    result: dict = {
        "graph_version": GV,
        "phone_number_id": phone_id or "NOT SET",
        "explicit_waba_id": explicit_waba or "NOT SET",
        "tokens_tested": [],
        "recommendation": None,
        "steps": [],
    }

    def test_token(token: str, label: str) -> dict:
        """Run a full permission check on a token."""
        info: dict = {
            "label": label,
            "preview": token[:14] + "..." if token else "EMPTY",
            "identity": None,
            "token_type": None,
            "can_read_waba": False,
            "template_list_status": None,
            "error": None,
        }
        if not token:
            info["error"] = "Token is empty"
            return info

        hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # /me — don't request "type" field, it's not available in newer Graph versions
        r = requests.get(f"{GRAPH}/{GV}/me", headers=hdrs,
                         params={"fields": "id,name"}, timeout=10)
        if r.status_code == 200:
            me = r.json()
            info["identity"] = f"{me.get('name','?')} (id:{me.get('id','?')})"
            info["token_type"] = "ok"
        else:
            # /me failing is not fatal — token may still work for WABA
            info["identity"] = f"(could not resolve /me: {r.status_code})"
            info["token_type"] = "unknown"

        # Try WABA templates directly
        if explicit_waba:
            r2 = requests.get(
                f"{GRAPH}/{GV}/{explicit_waba}/message_templates",
                headers=hdrs,
                params={"limit": 2, "fields": "id,name,status"},
                timeout=15,
            )
            info["template_list_status"] = r2.status_code
            if r2.status_code == 200:
                info["can_read_waba"] = True
                info["template_sample"] = [
                    {"name": t.get("name"), "status": t.get("status")}
                    for t in r2.json().get("data", [])[:2]
                ]
            else:
                info["template_list_error"] = r2.json().get("error", {}).get("message", r2.text[:150])
        return info

    # Test both tokens
    wa_result   = test_token(wa_token,   "WHATSAPP_ACCESS_TOKEN")
    page_result = test_token(page_token, "META_PAGE_ACCESS_TOKEN")
    result["tokens_tested"] = [wa_result, page_result]

    # Determine recommendation
    if wa_result["can_read_waba"]:
        result["recommendation"] = "WHATSAPP_ACCESS_TOKEN works. Click Pull from Meta."
        result["working_token"] = "WHATSAPP_ACCESS_TOKEN"
    elif page_result["can_read_waba"]:
        result["recommendation"] = (
            "META_PAGE_ACCESS_TOKEN can read templates. "
            "Set WHATSAPP_ACCESS_TOKEN = same value as META_PAGE_ACCESS_TOKEN in Render, then redeploy."
        )
        result["working_token"] = "META_PAGE_ACCESS_TOKEN"
    else:
        result["recommendation"] = (
            "Neither token has whatsapp_business_management permission. "
            "You need a PERMANENT System User token. Steps: "
            "1) business.facebook.com → Settings → Users → System Users → "
            "2) Create/select a system user → Generate New Token → "
            "3) Select your app → tick 'whatsapp_business_management' + 'whatsapp_business_messaging' → "
            "4) Copy the token → set as WHATSAPP_ACCESS_TOKEN in Render."
        )
        result["working_token"] = None

    # Quick WABA resolve check
    result["waba_id_to_use"] = explicit_waba or "NOT SET — add WHATSAPP_BUSINESS_ACCOUNT_ID to Render"

    return result



@router.get("/test-token")
def test_token_raw(user: str = Depends(require_user)):
    """
    Calls the WABA message_templates endpoint with the stored token.
    Returns the raw response so you can see exactly what Meta says.
    No processing — pure passthrough.
    """
    token = (settings.whatsapp_access_token or "").strip()
    explicit_waba = (getattr(settings, "whatsapp_business_account_id", None) or "").strip()

    result = {
        "token_length": len(token),
        "token_first_20": token[:20] + "..." if len(token) > 20 else token,
        "token_last_10": "..." + token[-10:] if len(token) > 10 else token,
        "waba_id": explicit_waba,
        "calls": [],
    }

    headers = {"Authorization": f"Bearer {token}"}

    # Call 1: /me
    r1 = requests.get(f"{GRAPH}/{GV}/me", headers=headers,
                      params={"fields": "id,name"}, timeout=10)
    result["calls"].append({
        "url": f"{GRAPH}/{GV}/me",
        "status": r1.status_code,
        "response": r1.json() if r1.headers.get("content-type","").startswith("application/json") else r1.text[:200],
    })

    # Call 2: message_templates
    if explicit_waba:
        r2 = requests.get(
            f"{GRAPH}/{GV}/{explicit_waba}/message_templates",
            headers=headers,
            params={"fields": "id,name,status", "limit": 3},
            timeout=15,
        )
        result["calls"].append({
            "url": f"{GRAPH}/{GV}/{explicit_waba}/message_templates",
            "status": r2.status_code,
            "response": r2.json() if r2.headers.get("content-type","").startswith("application/json") else r2.text[:300],
        })

    # Call 3: debug_token (self-introspect)
    r3 = requests.get(
        f"{GRAPH}/{GV}/debug_token",
        headers=headers,
        params={"input_token": token},
        timeout=10,
    )
    if r3.status_code == 200:
        data = r3.json().get("data", {})
        result["token_debug"] = {
            "is_valid": data.get("is_valid"),
            "type": data.get("type"),
            "app_id": data.get("app_id"),
            "expires_at": "never" if data.get("expires_at") == 0 else data.get("expires_at"),
            "scopes": data.get("scopes", []),
            "error": data.get("error"),
        }
    else:
        result["token_debug"] = {"status": r3.status_code, "response": r3.text[:200]}

    return result



@router.get("/inspect-all")
def inspect_all_templates(db: Session = Depends(get_db)):
    """Public debug — list all template IDs and names."""
    templates = db.query(WhatsAppTemplate).all()
    return [{"id": t.id, "name": t.name, "body_text": (t.body_text or "")[:80], "body_variables": t.body_variables} for t in templates]


@router.get("/{template_id}/inspect")
def inspect_template(
    template_id: int,
    db: Session = Depends(get_db),
):
    # No auth — debug only, remove after fixing
    """Show exactly what is stored in DB + what payload will be sent."""
    import re as _re
    tmpl = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == template_id).first()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Not found")

    body_text = tmpl.body_text or ""
    named = _re.findall(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}", body_text)
    numeric = _re.findall(r"\{\{(\d+)\}\}", body_text)

    # Simulate what would be sent with dummy variables
    dummy_vars = [f"VAR_{i+1}" for i in range(max(len(named), len(numeric), len(tmpl.body_variables or [])))]

    contact_sim = BulkContactIn(phone="919741308822", name="Test", variables=dummy_vars)
    try:
        components = _build_send_components(tmpl, contact_sim)
    except Exception as e:
        components = f"ERROR: {e}"

    return {
        "id": tmpl.id,
        "name": tmpl.name,
        "body_text": body_text,
        "body_text_named_placeholders": named,
        "body_text_numeric_placeholders": numeric,
        "stored_body_variables": tmpl.body_variables,
        "header_type": tmpl.header_type,
        "meta_raw_components": (tmpl.meta_raw or {}).get("components", []),
        "simulated_send_payload_components": components,
    }


@router.post("/sync-from-meta")
def sync_templates_from_meta(
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Pull all approved templates from Meta WABA and upsert into local DB.
    This lets you import templates that were created directly in WhatsApp Manager.
    """
    waba = _waba_id()
    r = requests.get(
        f"{GRAPH}/{GV}/{waba}/message_templates",
        headers=_wa_headers(),
        params={
            "fields": "id,name,status,category,language,components,quality_score,rejected_reason",
            "limit": 100,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.json())

    meta_templates = r.json().get("data", [])
    synced = []
    skipped = []

    for mt in meta_templates:
        meta_id = mt.get("id")
        name = mt.get("name")
        existing = (
            db.query(WhatsAppTemplate)
            .filter(WhatsAppTemplate.meta_template_id == meta_id)
            .first()
        )

        # Parse components back into our fields
        components = mt.get("components", [])
        header_type = None
        header_text = None
        body_text = ""
        footer_text = None
        buttons = []
        body_variables = []

        for comp in components:
            ctype = comp.get("type", "").upper()
            if ctype == "HEADER":
                fmt = comp.get("format", "TEXT").upper()
                header_type = fmt
                if fmt == "TEXT":
                    header_text = comp.get("text", "")
            elif ctype == "BODY":
                body_text = comp.get("text", "")
                # Extract example variable values if present
                ex = comp.get("example", {})
                if ex.get("body_text"):
                    body_variables = ex["body_text"][0] if ex["body_text"] else []
                # Also check for named variable examples (newer Meta API format)
                if ex.get("body_text_named_params"):
                    # Store as ordered list of var names for CSV column mapping
                    named = ex["body_text_named_params"]
                    body_variables = [p.get("param_name", f"var{i+1}") for i, p in enumerate(named)]
            elif ctype == "FOOTER":
                footer_text = comp.get("text", "")
            elif ctype == "BUTTONS":
                for b in comp.get("buttons", []):
                    bt = b.get("type", "").upper()
                    btn = {"type": bt, "text": b.get("text", "")}
                    if bt == "URL":
                        btn["url"] = b.get("url", "")
                    elif bt == "PHONE_NUMBER":
                        btn["phone_number"] = b.get("phone_number", "")
                    buttons.append(btn)

        if existing:
            existing.status = mt.get("status")
            existing.rejection_reason = mt.get("rejected_reason")
            existing.meta_raw = mt
            existing.body_text = body_text or existing.body_text
            existing.footer_text = footer_text
            existing.buttons = buttons or existing.buttons
            db.commit()
            skipped.append(name)
        else:
            tmpl = WhatsAppTemplate(
                meta_template_id=meta_id,
                name=name,
                category=mt.get("category", "MARKETING"),
                language=mt.get("language", "en"),
                status=mt.get("status"),
                header_type=header_type,
                header_text=header_text,
                body_text=body_text,
                body_variables=body_variables if body_variables else None,
                footer_text=footer_text,
                buttons=buttons if buttons else None,
                rejection_reason=mt.get("rejected_reason"),
                meta_raw=mt,
            )
            db.add(tmpl)
            synced.append(name)

    db.commit()
    return {
        "ok": True,
        "total_from_meta": len(meta_templates),
        "newly_imported": len(synced),
        "updated_existing": len(skipped),
        "imported": synced,
        "updated": skipped,
    }
