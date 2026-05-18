import requests
from sqlalchemy.orm import Session
from .config import settings
from .models import Lead, WhatsAppMessage, WhatsAppStatusLog
from .utils import clean_phone, field_map_from_meta, first_nonempty, get_seminar_details, now_iso
from .whatsapp import send_template_for_lead

GRAPH_VERSION = "v25.0"

def fetch_lead_details(lead_id: str):
    if not settings.meta_access_token:
        return {"id": lead_id, "field_data": []}
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{lead_id}"
    params = {"access_token": settings.meta_access_token, "fields": "id,created_time,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,form_id,form_name,is_organic,platform,field_data"}
    r = requests.get(url, params=params, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"id": lead_id, "error_text": r.text}
    print("Fetched lead details:", data)
    return data

def upsert_lead_from_meta(db: Session, lead_id: str, raw: dict | None = None, auto_send=True):
    data = raw or fetch_lead_details(lead_id)
    fields = field_map_from_meta(data)
    merged = {**data, **fields}
    phone = clean_phone(first_nonempty(fields.get("phone"), fields.get("phone_number"), fields.get("mobile"), fields.get("mobile_number")))
    name = first_nonempty(fields.get("full_name"), fields.get("name"), fields.get("first_name"))
    preferred_day = first_nonempty(fields.get("please_choose_a_day_for_the_free_seminar"), fields.get("which_session_will_you_attend?"), fields.get("seminar_day"))
    seminar = get_seminar_details(preferred_day, merged)

    lead = db.query(Lead).filter(Lead.meta_lead_id == str(lead_id)).first()
    created = False
    if not lead:
        lead = Lead(meta_lead_id=str(lead_id))
        created = True
    lead.created_time = data.get("created_time")
    lead.full_name = name or lead.full_name
    lead.phone = phone or lead.phone
    lead.email = first_nonempty(fields.get("email"), fields.get("email_address"), lead.email)
    lead.city = first_nonempty(fields.get("city"), lead.city)
    lead.experience = first_nonempty(fields.get("what_is_your_experience_level_in_stock_market?"), fields.get("experience"), lead.experience)
    lead.preferred_day = preferred_day or lead.preferred_day
    for k in ["campaign_id", "campaign_name", "adset_id", "adset_name", "ad_id", "ad_name", "form_id", "form_name", "platform", "is_organic"]:
        setattr(lead, k, data.get(k) or getattr(lead, k))
    lead.raw = merged
    for k, v in seminar.items():
        setattr(lead, k, v)
    db.add(lead)
    db.commit()
    db.refresh(lead)

    wa = None
    if auto_send and created and not lead.whatsapp_message_id:
        wa = send_template_for_lead(db, lead)
    return lead, wa

def handle_leadgen_payload(db: Session, payload: dict):
    lead_ids = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            lead_id = value.get("leadgen_id") or value.get("lead_id")
            if lead_id:
                lead_ids.append(str(lead_id))
                upsert_lead_from_meta(db, str(lead_id), auto_send=True)
    return {"lead_ids": lead_ids}

def _find_or_create_lead_by_phone(db: Session, phone: str, name: str = ""):
    phone = clean_phone(phone)
    lead = db.query(Lead).filter(Lead.phone == phone).order_by(Lead.id.desc()).first()
    if not lead:
        lead = Lead(phone=phone, full_name=name or None, status="WhatsApp")
        db.add(lead); db.commit(); db.refresh(lead)
    return lead

def handle_whatsapp_payload(db: Session, payload: dict):
    statuses, messages = [], []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            contacts = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", []) or []}
            for st in value.get("statuses", []) or []:
                mid, status = st.get("id"), st.get("status")
                ts = st.get("timestamp")
                db.add(WhatsAppStatusLog(wa_message_id=mid, status=status, recipient_id=st.get("recipient_id"), timestamp=ts, raw=st))
                lead = db.query(Lead).filter(Lead.whatsapp_message_id == mid).first()
                msg = db.query(WhatsAppMessage).filter(WhatsAppMessage.wa_message_id == mid).first()
                if msg:
                    msg.status = status
                if lead:
                    lead.whatsapp_status = status
                    lead.whatsapp_last_status_at = now_iso()
                    if status == "sent":
                        lead.whatsapp_sent = True; lead.whatsapp_sent_at = lead.whatsapp_sent_at or now_iso()
                    elif status == "delivered":
                        lead.whatsapp_delivered = True; lead.whatsapp_delivered_at = now_iso()
                    elif status == "read":
                        lead.whatsapp_read = True; lead.whatsapp_read_at = now_iso()
                    elif status == "failed":
                        lead.whatsapp_failed = True; lead.whatsapp_failed_at = now_iso()
                statuses.append(f"{mid}:{status}:{bool(lead)}")
            for m in value.get("messages", []) or []:
                phone = clean_phone(m.get("from"))
                name = contacts.get(phone) or ""
                text = ""
                msg_type = m.get("type", "unknown")
                if msg_type == "text":
                    text = (m.get("text") or {}).get("body", "")
                elif msg_type == "button":
                    text = (m.get("button") or {}).get("text", "")
                elif msg_type == "interactive":
                    text = str(m.get("interactive") or {})
                else:
                    text = f"[{msg_type}]"
                lead = _find_or_create_lead_by_phone(db, phone, name)
                lead.latest_reply_text = text
                lead.latest_reply_at = now_iso()
                lead.unread_count = (lead.unread_count or 0) + 1
                db.add(WhatsAppMessage(wa_message_id=m.get("id"), lead_id=lead.id, phone=phone, contact_name=name or lead.full_name, direction="incoming", message_type=msg_type, body=text, raw=m, timestamp=m.get("timestamp")))
                messages.append(f"{phone}:{msg_type}")
    db.commit()
    return {"statuses": statuses, "messages": messages}

def classify_webhook_and_handle(db: Session, payload: dict):
    text = str(payload)
    if "leadgen_id" in text or "lead_id" in text:
        return {"type": "leadgen", **handle_leadgen_payload(db, payload)}
    if "statuses" in text or "messages" in text:
        return {"type": "whatsapp", **handle_whatsapp_payload(db, payload)}
    return {"type": "unknown"}
