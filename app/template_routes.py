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

    results = []
    for contact in data.contacts:
        phone = clean_phone(contact.phone)
        if not phone:
            results.append({"phone": contact.phone, "ok": False, "error": "invalid phone"})
            continue

        components = _build_send_components(tmpl, contact)

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

        results.append({
            "phone": contact.phone,
            "name": contact.name,
            "ok": ok,
            "wamid": wamid,
            "error": resp.get("error", {}).get("message") if not ok else None,
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

    # Header param (if header has a variable or media)
    if tmpl.header_type == "TEXT" and tmpl.header_text and "{{" in tmpl.header_text:
        param_val = variables[0] if variables else (contact.name or "")
        components.append({
            "type": "header",
            "parameters": [{"type": "text", "text": param_val}],
        })
    elif tmpl.header_type in ("IMAGE", "VIDEO", "DOCUMENT") and tmpl.header_media_handle:
        media_key = tmpl.header_type.lower()
        components.append({
            "type": "header",
            "parameters": [{
                "type": media_key,
                media_key: {"id": tmpl.header_media_handle},
            }],
        })

    # Body params — support named {{name}} and legacy numeric {{1}} variables
    body_params = []
    named_placeholders = re.findall(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}", tmpl.body_text or "")
    numeric_placeholders = re.findall(r"\{\{(\d+)\}\}", tmpl.body_text or "")

    if named_placeholders:
        seen: set = set()
        unique_names = [n for n in named_placeholders if not (n in seen or seen.add(n))]
        for i, var_name in enumerate(unique_names):
            if i < len(variables) and variables[i]:
                val = variables[i]
            elif var_name in ("name", "customer_name", "client_name", "client"):
                val = contact.name or ""
            else:
                val = variables[i] if i < len(variables) else ""
            body_params.append({"type": "text", "text": str(val) if val else ""})
    else:
        for i, _ in enumerate(numeric_placeholders):
            val = variables[i] if i < len(variables) else (contact.name if i == 0 else "")
            body_params.append({"type": "text", "text": str(val) if val else ""})

    if body_params:
        components.append({"type": "body", "parameters": body_params})

    return components


def _tmpl_out(t: WhatsAppTemplate) -> dict:
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
    Returns resolved config and attempts to auto-detect WABA ID.
    Call this first if Pull from Meta is failing.
    """
    token = (settings.whatsapp_access_token or "").strip()
    phone_id = (settings.whatsapp_phone_number_id or "").strip()
    explicit_waba = (getattr(settings, "whatsapp_business_account_id", None) or "").strip()

    result = {
        "token_set": bool(token),
        "token_preview": token[:12] + "..." if token else None,
        "phone_number_id": phone_id or "NOT SET",
        "explicit_waba_id": explicit_waba or "NOT SET — auto-detecting",
        "graph_version": GV,
        "steps": [],
    }

    if not token:
        result["error"] = "WHATSAPP_ACCESS_TOKEN is not set"
        return result

    # Step 1: /me
    r = requests.get(f"{GRAPH}/{GV}/me", headers=_wa_headers(), timeout=10)
    result["steps"].append({
        "step": "/me — token identity",
        "status": r.status_code,
        "data": r.json() if r.status_code == 200 else r.text[:200],
    })

    # Step 2: phone number → WABA
    if phone_id:
        r2 = requests.get(
            f"{GRAPH}/{GV}/{phone_id}",
            headers=_wa_headers(),
            params={"fields": "id,display_phone_number,whatsapp_business_account"},
            timeout=10,
        )
        d2 = r2.json() if r2.status_code == 200 else r2.text[:300]
        result["steps"].append({
            "step": f"/{phone_id} — phone number info",
            "status": r2.status_code,
            "data": d2,
        })
        if r2.status_code == 200:
            waba_from_phone = r2.json().get("whatsapp_business_account", {})
            result["waba_from_phone_number"] = waba_from_phone or "field not returned (token may lack whatsapp_business_management permission)"

    # Step 3: resolved WABA
    if explicit_waba:
        result["resolved_waba_id"] = explicit_waba
        result["resolution_method"] = "env var WHATSAPP_BUSINESS_ACCOUNT_ID"
    else:
        # Try to resolve
        try:
            global _WABA_ID_CACHE
            _WABA_ID_CACHE = ""  # force re-resolve
            waba = _waba_id()
            result["resolved_waba_id"] = waba
            result["resolution_method"] = "auto-detected"
        except Exception as e:
            result["resolved_waba_id"] = None
            result["resolution_method"] = f"FAILED: {e}"
            result["fix"] = (
                "Go to business.facebook.com/latest/whatsapp_manager → "
                "look at the URL for business_id=XXXXXX — that number is your WABA ID. "
                "Add it to Render as WHATSAPP_BUSINESS_ACCOUNT_ID=XXXXXX"
            )

    # Step 4: try listing templates if WABA resolved
    if result.get("resolved_waba_id"):
        waba = result["resolved_waba_id"]
        r3 = requests.get(
            f"{GRAPH}/{GV}/{waba}/message_templates",
            headers=_wa_headers(),
            params={"limit": 3, "fields": "id,name,status"},
            timeout=15,
        )
        result["steps"].append({
            "step": f"/{waba}/message_templates — list test",
            "status": r3.status_code,
            "data": r3.json() if r3.status_code == 200 else r3.text[:300],
        })

    return result


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
