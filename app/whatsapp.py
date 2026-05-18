import requests
from sqlalchemy.orm import Session
from .config import settings
from .models import Lead, WhatsAppMessage
from .utils import now_iso, clean_phone

GRAPH_VERSION = "v25.0"

def send_template_for_lead(db: Session, lead: Lead):
    if not settings.whatsapp_enabled:
        return {"skipped": True, "reason": "WHATSAPP_ENABLED=false"}
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return {"ok": False, "error": "Missing WhatsApp token or phone number id"}
    phone = clean_phone(lead.phone)
    if not phone:
        return {"ok": False, "error": "Missing lead phone"}

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": settings.whatsapp_template_name,
            "language": {"code": settings.whatsapp_language_code},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "parameter_name": "customer_name", "text": lead.full_name or "Customer"},
                    {"type": "text", "parameter_name": "seminar_date", "text": lead.seminar_date or ""},
                    {"type": "text", "parameter_name": "seminar_time", "text": lead.seminar_time or ""},
                    {"type": "text", "parameter_name": "arrival_time", "text": lead.arrival_time or ""},
                    {"type": "text", "parameter_name": "venue", "text": lead.venue or settings.seminar_venue},
                ]
            }]
        }
    }
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}", "Content-Type": "application/json"}
    print("WhatsApp send request:", payload)
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    print("WhatsApp send response:", r.status_code, data)
    if r.status_code in (200, 201):
        msg = (data.get("messages") or [{}])[0]
        wamid = msg.get("id")
        lead.whatsapp_sent = True
        lead.whatsapp_status = msg.get("message_status", "accepted")
        lead.whatsapp_message_id = wamid
        lead.whatsapp_sent_at = now_iso()
        db.add(WhatsAppMessage(
            wa_message_id=wamid, lead_id=lead.id, phone=phone, contact_name=lead.full_name,
            direction="outgoing", message_type="template", body=settings.whatsapp_template_name,
            status=lead.whatsapp_status, raw=data, timestamp=lead.whatsapp_sent_at,
        ))
        db.commit()
        db.refresh(lead)
        return {"ok": True, "result": data}
    return {"ok": False, "status_code": r.status_code, "result": data}

def send_text_reply(db: Session, phone: str, text: str, lead: Lead | None = None):
    phone = clean_phone(phone)
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}", "Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    if r.status_code not in (200, 201):
        return {"ok": False, "status_code": r.status_code, "result": data}
    wamid = ((data.get("messages") or [{}])[0]).get("id")
    db.add(WhatsAppMessage(
        wa_message_id=wamid, lead_id=lead.id if lead else None, phone=phone,
        contact_name=lead.full_name if lead else None, direction="outgoing", message_type="text",
        body=text, status="accepted", raw=data, timestamp=now_iso()
    ))
    if lead:
        lead.unread_count = 0
    db.commit()
    return {"ok": True, "result": data}
