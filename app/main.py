import os
import requests
from sqlalchemy.orm import Session
from .config import settings
from .models import Lead, WhatsAppMessage, WhatsAppStatusLog
from .utils import clean_phone, field_map_from_meta, get_lead_field, get_seminar_details, now_iso
from .whatsapp import send_template_for_lead, save_outgoing_template_message

GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v25.0")


def _clean_meta_value(value):
    text = str(value or "").strip()
    for prefix in ("l:", "ag:", "as:", "c:", "f:", "p:"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text




def normalize_source(value):
    """Normalize Meta platform into a clean CRM source label."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in ("ig", "instagram"):
        return "IG"
    if text in ("fb", "facebook"):
        return "FB"
    if "instagram" in text:
        return "IG"
    if "facebook" in text:
        return "FB"
    return text.upper()


def _extract_incoming_text(m: dict, msg_type: str) -> str:
    """Extract readable text from any WhatsApp incoming message type.

    Handles text, button quick-replies, interactive (button/list) replies,
    reactions, and media captions. Falls back gracefully for unknown types
    instead of showing a bare [unsupported] label.
    """
    try:
        if msg_type == "text":
            return (m.get("text") or {}).get("body", "") or "[empty message]"

        if msg_type == "button":
            # Quick-reply button on a template
            return (m.get("button") or {}).get("text", "") or "[button reply]"

        if msg_type == "interactive":
            inter = m.get("interactive") or {}
            itype = inter.get("type", "")
            if itype == "button_reply":
                return (inter.get("button_reply") or {}).get("title", "") or "[button reply]"
            if itype == "list_reply":
                lr = inter.get("list_reply") or {}
                return lr.get("title", "") or lr.get("description", "") or "[list reply]"
            return "[interactive reply]"

        if msg_type == "reaction":
            emoji = (m.get("reaction") or {}).get("emoji", "")
            return f"Reacted: {emoji}" if emoji else "[reaction]"

        # Media types with optional captions
        if msg_type in ("image", "video", "document", "audio", "sticker", "voice"):
            caption = (m.get(msg_type) or {}).get("caption", "")
            label = {
                "image": "📷 Photo",
                "video": "🎥 Video",
                "document": "📄 Document",
                "audio": "🎵 Audio",
                "voice": "🎙️ Voice message",
                "sticker": "Sticker",
            }.get(msg_type, msg_type)
            return f"{label}{(': ' + caption) if caption else ''}"

        if msg_type == "location":
            loc = m.get("location") or {}
            nm = loc.get("name") or ""
            return f"📍 Location{(': ' + nm) if nm else ''}"

        if msg_type == "contacts":
            return "👤 Contact card"

        if msg_type == "unsupported":
            # WhatsApp couldn't process the user's message format
            errs = m.get("errors") or []
            if errs:
                title = errs[0].get("title", "") or errs[0].get("message", "")
                return f"[Unsupported message{(': ' + title) if title else ''}]"
            return "[Unsupported message type]"

        # Any other / future type
        return f"[{msg_type} message]"
    except Exception as exc:
        print(f"[WARN] Failed to extract incoming text for type={msg_type}: {exc}", flush=True)
        return f"[{msg_type}]"


def get_meta_token():
    """
    Keep compatibility with the old Sheets backend env names.
    Your Render already has META_PAGE_ACCESS_TOKEN, so this must be accepted.
    """
    token = (
        os.getenv("META_PAGE_ACCESS_TOKEN")
        or os.getenv("META_ACCESS_TOKEN")
        or os.getenv("PAGE_ACCESS_TOKEN")
        or os.getenv("GRAPH_ACCESS_TOKEN")
        or os.getenv("FACEBOOK_ACCESS_TOKEN")
        or getattr(settings, "meta_access_token", "")
    )
    return str(token or "").strip()


def graph_get(object_id: str, fields: str, timeout: int = 25):
    token = get_meta_token()
    object_id = _clean_meta_value(object_id)

    if not token:
        print("Meta lead fetch skipped: no token found. Expected META_PAGE_ACCESS_TOKEN or META_ACCESS_TOKEN", flush=True)
        return {"id": object_id, "field_data": [], "fetch_error": "missing_meta_token"}

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{object_id}"
    params = {"access_token": token, "fields": fields}
    response = requests.get(url, params=params, timeout=timeout)

    print(f"Meta GET /{object_id}?fields={fields}: {response.status_code} {response.text}", flush=True)

    try:
        data = response.json()
    except Exception:
        data = {"id": object_id, "field_data": [], "error_text": response.text}

    if response.status_code >= 400:
        data.setdefault("id", object_id)
        data.setdefault("field_data", [])
        data["fetch_error"] = "graph_error"
        data["http_status"] = response.status_code

    return data


def fetch_lead_details(lead_id: str):
    """
    Fetch only safe Lead object fields first.
    Do NOT request ad_name/adset_name/campaign_name/form_name directly from Lead object.
    Meta rejects unsupported fields and then field_data becomes unavailable.
    """
    safe_fields = "id,created_time,field_data,ad_id,adset_id,campaign_id,form_id,is_organic,platform"
    return graph_get(lead_id, safe_fields, timeout=30)


def fetch_optional_object(object_id: str, fields: str):
    object_id = _clean_meta_value(object_id)
    if not object_id:
        return {}
    data = graph_get(object_id, fields, timeout=15)
    if data.get("fetch_error"):
        print(f"Optional Meta object fetch failed for {object_id}: {data}", flush=True)
        return {}
    return data


def enrich_ad_form_metadata(data: dict, webhook_value: dict | None = None):
    """Fill ad/adset/campaign/form names like the old Sheets backend did."""
    webhook_value = webhook_value or {}

    data["ad_id"] = _clean_meta_value(data.get("ad_id") or webhook_value.get("ad_id") or webhook_value.get("adgroup_id") or "")
    data["form_id"] = _clean_meta_value(data.get("form_id") or webhook_value.get("form_id") or "")
    data["adset_id"] = _clean_meta_value(data.get("adset_id") or webhook_value.get("adset_id") or webhook_value.get("adgroup_id") or "")
    data["campaign_id"] = _clean_meta_value(data.get("campaign_id") or webhook_value.get("campaign_id") or "")

    # Meta normally sends "facebook" or "instagram". Keep CRM display clean as FB / IG.
    source = (
        data.get("platform")
        or data.get("source")
        or webhook_value.get("platform")
        or webhook_value.get("source")
        or webhook_value.get("publisher_platform")
        or ""
    )
    data["platform"] = normalize_source(source)

    # --- Step 1: Resolve form name (works with leads_retrieval scope only) ---
    # Do this FIRST so form_name is available as a campaign_name fallback below.
    if data.get("form_id"):
        form = fetch_optional_object(data["form_id"], "id,name,page{id,name}")
        data["form_name"] = data.get("form_name") or form.get("name", "")

    # --- Step 2: Resolve ad → adset → campaign chain (requires ads_read scope) ---
    if data.get("ad_id"):
        ad = fetch_optional_object(data["ad_id"], "id,name,adset_id,campaign_id")
        if not ad:
            print(
                f"[WARN] Ad fetch returned empty for ad_id={data['ad_id']} — "
                "token may lack ads_read scope. campaign_name will fall back to form_name.",
                flush=True,
            )
        data["ad_name"] = data.get("ad_name") or ad.get("name", "")
        data["adset_id"] = _clean_meta_value(data.get("adset_id") or ad.get("adset_id", ""))
        data["campaign_id"] = _clean_meta_value(data.get("campaign_id") or ad.get("campaign_id", ""))

    if data.get("adset_id"):
        adset = fetch_optional_object(data["adset_id"], "id,name,campaign_id")
        data["adset_name"] = data.get("adset_name") or adset.get("name", "")
        data["campaign_id"] = _clean_meta_value(data.get("campaign_id") or adset.get("campaign_id", ""))

    if data.get("campaign_id"):
        campaign = fetch_optional_object(data["campaign_id"], "id,name")
        data["campaign_name"] = data.get("campaign_name") or campaign.get("name", "")

    # --- Step 3: Fallback — use form name as campaign_name when ads_read is unavailable ---
    # The leadgen form name (e.g. "WOI Free Seminar - FB June") is a reliable
    # identifier even when the token cannot resolve the campaign hierarchy.
    if not data.get("campaign_name") and data.get("form_name"):
        data["campaign_name"] = data["form_name"]
        print(
            f"[INFO] campaign_name not resolved via ad chain — using form_name as fallback: {data['form_name']}",
            flush=True,
        )

    return data


def upsert_lead_from_meta(db: Session, lead_id: str, raw: dict | None = None, auto_send=True, webhook_value: dict | None = None):
    """
    Required sequence:
    1. Fetch full lead details from Meta
    2. Save/update Neon database FIRST
    3. Commit database row
    4. Send WhatsApp only after DB row exists
    5. Update same row with WhatsApp id/status and chatbox message
    """
    print("Processing leadgen id:", lead_id, flush=True)

    data = raw or fetch_lead_details(lead_id)
    data = enrich_ad_form_metadata(data, webhook_value=webhook_value)
    fields = field_map_from_meta(data)
    merged = {**data, **fields}
    merged["platform"] = normalize_source(merged.get("platform") or merged.get("source") or "")

    raw_phone = get_lead_field(
        merged,
        "phone", "phone_number", "phone number", "mobile", "mobile_number", "mobile number",
        "whatsapp_number", "whatsapp number", "your_phone_number", "your mobile number",
    )
    phone = clean_phone(raw_phone)

    name = get_lead_field(
        merged,
        "full_name", "full name", "name", "your_name", "your name", "customer_name",
        "first_name", "first name",
    )

    email = get_lead_field(merged, "email", "email_address", "email address")
    city = get_lead_field(merged, "city", "location", "place")
    experience = get_lead_field(
        merged,
        "what_is_your_experience_level_in_stock_market?",
        "what_is_your_current_experience_level?",
        "what is your current experience level?",
        "experience", "experience_level", "current experience level",
    )
    preferred_day = get_lead_field(
        merged,
        "please_choose_a_day_for_the_free_seminar",
        "please choose a day for the free seminar",
        "which_session_will_you_attend?",
        "seminar_day", "seminar day", "preferred_day", "preferred day", "day",
    )
    seminar = get_seminar_details(preferred_day, merged)

    lead = db.query(Lead).filter(Lead.meta_lead_id == str(lead_id)).first()
    created = False
    if not lead:
        lead = Lead(meta_lead_id=str(lead_id))
        created = True

    # DB update FIRST. Do not send WhatsApp before this commit.
    lead.created_time = data.get("created_time") or lead.created_time
    lead.full_name = name or lead.full_name
    lead.phone = phone or lead.phone
    lead.email = email or lead.email
    lead.city = city or lead.city
    lead.experience = experience or lead.experience
    lead.preferred_day = preferred_day or lead.preferred_day
    lead.status = lead.status or "New"

    for k in [
        "campaign_id", "campaign_name", "adset_id", "adset_name", "ad_id", "ad_name",
        "form_id", "form_name", "platform", "is_organic",
    ]:
        value = data.get(k)
        if k == "platform":
            value = normalize_source(value)
        # Always write if we have a fresh value — never skip when DB already has one.
        # This ensures campaign_name/form_name are updated on re-fetch.
        if value:
            setattr(lead, k, value)
        # If no new value, preserve existing DB value (don't blank it out).

    lead.raw = merged

    for k, v in seminar.items():
        setattr(lead, k, v)

    db.add(lead)
    db.commit()
    db.refresh(lead)

    print(
        "Lead saved before WhatsApp:",
        {
            "id": lead.id,
            "meta_lead_id": lead.meta_lead_id,
            "name": lead.full_name,
            "phone": lead.phone,
            "campaign": lead.campaign_name,
            "form": lead.form_name,
            "created": created,
            "fetch_error": (lead.raw or {}).get("fetch_error"),
        },
        flush=True,
    )

    wa = None
    if auto_send and not lead.whatsapp_sent and not lead.whatsapp_message_id:
        if not lead.phone:
            print(
                "WhatsApp skipped after DB save: phone missing",
                {"lead_id": lead.id, "meta_lead_id": lead.meta_lead_id, "raw": lead.raw},
                flush=True,
            )
            wa = {"ok": False, "skipped": True, "reason": "phone_missing_after_db_save", "lead_id": lead.id}
        else:
            wa = send_template_for_lead(db, lead)
            db.refresh(lead)
            print("WhatsApp result after DB save:", wa, flush=True)

    return lead, wa


def handle_leadgen_payload(db: Session, payload: dict):
    lead_ids = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            lead_id = value.get("leadgen_id") or value.get("lead_id")
            if lead_id:
                lead_ids.append(str(lead_id))
                upsert_lead_from_meta(db, str(lead_id), auto_send=True, webhook_value=value)
    return {"lead_ids": lead_ids}


def _find_or_create_lead_by_phone(db: Session, phone: str, name: str = ""):
    phone = clean_phone(phone)
    lead = db.query(Lead).filter(Lead.phone == phone).order_by(Lead.id.desc()).first()
    if not lead:
        lead = Lead(phone=phone, full_name=name or None, status="WhatsApp")
        db.add(lead)
        db.commit()
        db.refresh(lead)
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
                if lead and not msg:
                    msg = save_outgoing_template_message(db, lead, wamid=mid, status=status, raw={"source": "status_webhook_backfill", "status": st})
                if lead:
                    lead.whatsapp_status = status
                    lead.whatsapp_last_status_at = now_iso()
                    if status == "sent":
                        lead.whatsapp_sent = True
                        lead.whatsapp_sent_at = lead.whatsapp_sent_at or now_iso()
                    elif status == "delivered":
                        lead.whatsapp_delivered = True
                        lead.whatsapp_delivered_at = now_iso()
                    elif status == "read":
                        lead.whatsapp_read = True
                        lead.whatsapp_read_at = now_iso()
                    elif status == "failed":
                        lead.whatsapp_failed = True
                        lead.whatsapp_failed_at = now_iso()
                statuses.append(f"{mid}:{status}:{bool(lead)}")
            for m in value.get("messages", []) or []:
                phone = clean_phone(m.get("from"))
                name = contacts.get(phone) or ""
                msg_type = m.get("type", "unknown")
                text = _extract_incoming_text(m, msg_type)
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
