from fastapi import FastAPI, Depends, HTTPException, Query, Request, BackgroundTasks
import os
import asyncio
import httpx
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func, and_
from typing import Optional
from datetime import date, datetime, timedelta
from .config import settings
from .db import get_db, init_db
from .models import Lead, FollowUp, WhatsAppMessage
from .auth import LoginIn, TokenOut, create_token, require_user
from .schemas import LeadOut, LeadCreate, LeadUpdate, FollowUpIn, ReplyIn, TestWhatsAppIn
from .utils import clean_phone, get_seminar_details, now_iso
from .whatsapp import send_template_for_lead, send_text_reply, save_outgoing_template_message
from .meta import classify_webhook_and_handle, upsert_lead_from_meta

app = FastAPI(title="WOI Lead CRM API", version="2.0.0")

KEEP_ALIVE_TASK = None

# ---------------------------------------------------------------------------
# Keep-alive
# ---------------------------------------------------------------------------

async def keep_alive_ping():
    public_url = (
        os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or os.getenv("APP_URL")
    )
    if not public_url:
        print("Keep-alive disabled: RENDER_EXTERNAL_URL/PUBLIC_BASE_URL/APP_URL missing", flush=True)
        return

    ping_url = public_url.rstrip("/") + "/"
    print(f"Keep-alive enabled. Pinging every 13 minutes: {ping_url}", flush=True)

    while True:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(ping_url)
                print(f"Keep-alive ping: {response.status_code}", flush=True)
        except Exception as exc:
            print(f"Keep-alive ping failed: {exc}", flush=True)
        await asyncio.sleep(13 * 60)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

origins = [settings.frontend_origin, "http://localhost:3000", "http://127.0.0.1:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(set(origins)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup():
    init_db()
    global KEEP_ALIVE_TASK
    keep_alive_enabled = os.getenv("KEEP_ALIVE_ENABLED", "true").lower() in ("true", "1", "yes", "on")
    if keep_alive_enabled:
        KEEP_ALIVE_TASK = asyncio.create_task(keep_alive_ping())


@app.on_event("shutdown")
async def _shutdown():
    global KEEP_ALIVE_TASK
    if KEEP_ALIVE_TASK:
        KEEP_ALIVE_TASK.cancel()
        try:
            await KEEP_ALIVE_TASK
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok", "service": "woi-lead-crm-backend"}


# ---------------------------------------------------------------------------
# Debug — SECURED (requires auth)
# ---------------------------------------------------------------------------

@app.get("/debug/config")
def debug_config(user: str = Depends(require_user)):
    """Configuration sanity check — protected by auth."""
    return {
        "db": "configured" if settings.database_url else "missing",
        "meta_page_access_token": "configured" if os.getenv("META_PAGE_ACCESS_TOKEN") else "missing",
        "meta_access_token": "configured" if os.getenv("META_ACCESS_TOKEN") else "missing",
        "meta_graph_version": os.getenv("META_GRAPH_VERSION", "v25.0"),
        "whatsapp_enabled": settings.whatsapp_enabled,
        "whatsapp_access_token": "configured" if settings.whatsapp_access_token else "missing",
        "phone_number_id": settings.whatsapp_phone_number_id,
        "template": settings.whatsapp_template_name,
        "language": settings.whatsapp_language_code,
        "seminar_venue": settings.seminar_venue,
        "keep_alive_enabled": os.getenv("KEEP_ALIVE_ENABLED", "true"),
        "render_external_url": os.getenv("RENDER_EXTERNAL_URL", ""),
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn):
    if data.username != settings.admin_username or data.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return TokenOut(access_token=create_token(data.username))


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

@app.get("/leads")
def list_leads(
    q: str = "",
    status: str = "",
    day: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "latest",
    unread: bool = False,
    limit: int = Query(500, le=5000),
    offset: int = 0,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    from datetime import datetime as _dt
    query = db.query(Lead)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Lead.full_name.ilike(like),
            Lead.phone.ilike(like),
            Lead.campaign_name.ilike(like),
            Lead.ad_name.ilike(like),
            Lead.platform.ilike(like),
            Lead.latest_reply_text.ilike(like),
        ))
    if status:
        query = query.filter(Lead.status == status)
    if day:
        query = query.filter(Lead.seminar_day == day)
    if unread:
        query = query.filter(Lead.unread_count > 0)
    # Filter by lead created_time date range
    if date_from:
        try:
            query = query.filter(Lead.created_time >= date_from)
        except Exception:
            pass
    if date_to:
        try:
            query = query.filter(Lead.created_time <= date_to + "T23:59:59")
        except Exception:
            pass
    total = query.count()
    # Sort order
    if sort == "oldest":
        query = query.order_by(Lead.id.asc())
    else:
        query = query.order_by(Lead.id.desc())
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "rows": [LeadOut.model_validate(r).model_dump() for r in rows]}


@app.post("/leads", response_model=LeadOut)
def create_lead(data: LeadCreate, db: Session = Depends(get_db), user: str = Depends(require_user)):
    payload = data.model_dump()

    # Clean/normalise values before creating the SQLAlchemy model.
    # Do NOT pass phone separately again after **payload, otherwise Python raises:
    # TypeError: Lead() got multiple values for keyword argument 'phone'
    payload["phone"] = clean_phone(data.phone)
    payload["platform"] = payload.get("platform") or "FB"
    payload["campaign_name"] = payload.get("campaign_name") or "Manual"

    seminar = get_seminar_details(data.preferred_day, payload)
    lead = Lead(
        **payload,
        raw={"source": "manual", "platform": payload.get("platform")},
        **seminar,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


@app.patch("/leads/{lead_id}", response_model=LeadOut)
def update_lead(lead_id: int, data: LeadUpdate, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        if k == "phone" and v:
            v = clean_phone(v)
        setattr(lead, k, v)
    db.commit()
    db.refresh(lead)
    return lead


@app.post("/leads/{lead_id}/send-whatsapp")
def send_lead_whatsapp(lead_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return send_template_for_lead(db, lead)


# ---------------------------------------------------------------------------
# Follow-ups (per lead)
# ---------------------------------------------------------------------------

@app.get("/leads/{lead_id}/followups")
def get_followups(lead_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    rows = db.query(FollowUp).filter(FollowUp.lead_id == lead_id).order_by(FollowUp.id.asc()).all()
    return {"rows": [
        {
            "id": r.id, "lead_id": r.lead_id, "followup_no": r.followup_no,
            "followup_date": r.followup_date, "response": r.response,
            "confirmed": r.confirmed, "seminar_date": r.seminar_date,
            "next_followup_date": r.next_followup_date, "remarks": r.remarks,
            "created_at": str(r.created_at),
        }
        for r in rows
    ]}


def _sync_lead_from_followups(db: Session, lead: Lead):
    """Keep lead status / next follow-up in sync with the latest follow-up."""
    latest = (
        db.query(FollowUp)
        .filter(FollowUp.lead_id == lead.id)
        .order_by(FollowUp.id.desc())
        .first()
    )
    if latest:
        if latest.confirmed:
            lead.status = latest.confirmed
        lead.next_followup_at = latest.next_followup_date or lead.next_followup_at
    else:
        lead.next_followup_at = ""


def _renumber_followups(db: Session, lead_id: int):
    rows = db.query(FollowUp).filter(FollowUp.lead_id == lead_id).order_by(FollowUp.id.asc()).all()
    for idx, row in enumerate(rows, start=1):
        row.followup_no = idx


@app.post("/leads/{lead_id}/followups")
def add_followup(lead_id: int, data: FollowUpIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    count = db.query(FollowUp).filter(FollowUp.lead_id == lead_id).count()
    fu = FollowUp(lead_id=lead_id, followup_no=count + 1, **data.model_dump())
    lead.status = data.confirmed or lead.status
    lead.next_followup_at = data.next_followup_date or lead.next_followup_at
    if data.remarks:
        lead.notes = ((lead.notes or "") + "\n" + data.remarks).strip()
    db.add(fu)
    db.commit()
    db.refresh(fu)
    return {"success": True, "id": fu.id}


@app.patch("/leads/{lead_id}/followups/{followup_id}")
def update_followup(lead_id: int, followup_id: int, data: FollowUpIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    fu = db.query(FollowUp).filter(FollowUp.id == followup_id, FollowUp.lead_id == lead_id).first()
    if not fu:
        raise HTTPException(404, "Follow-up not found")

    for key, value in data.model_dump().items():
        setattr(fu, key, value)

    _sync_lead_from_followups(db, lead)
    db.commit()
    db.refresh(fu)
    return {"success": True, "id": fu.id}


@app.delete("/leads/{lead_id}/followups/{followup_id}")
def delete_followup(lead_id: int, followup_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    fu = db.query(FollowUp).filter(FollowUp.id == followup_id, FollowUp.lead_id == lead_id).first()
    if not fu:
        raise HTTPException(404, "Follow-up not found")

    db.delete(fu)
    db.flush()
    _renumber_followups(db, lead_id)
    _sync_lead_from_followups(db, lead)
    db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# Follow-ups worklist — OPTIMISED (single query, no N+1)
# ---------------------------------------------------------------------------

def _parse_followup_date(value):
    """Parse YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY into date object."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            pass
    return None


def _date_to_iso(value):
    d = _parse_followup_date(value)
    return d.isoformat() if d else ""


@app.get("/followups/due")
def list_due_followups(
    bucket: str = Query("today", description="today, tomorrow, overdue, week, all, range"),
    from_date: str = "",
    to_date: str = "",
    status: str = "",
    q: str = "",
    limit: int = Query(300, le=1000),
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Follow-up worklist. Uses a single JOIN query (no N+1) to load leads
    with their latest follow-up in one round-trip to the database.
    """
    today = date.today()
    bucket = (bucket or "today").lower().strip()

    if bucket == "tomorrow":
        start = end = today + timedelta(days=1)
    elif bucket == "week":
        start = today
        end = today + timedelta(days=7)
    elif bucket == "overdue":
        start = None
        end = today - timedelta(days=1)
    elif bucket == "all":
        start = end = None
    elif bucket == "range":
        start = _parse_followup_date(from_date)
        end = _parse_followup_date(to_date) or start
    else:
        start = end = today

    # -----------------------------------------------------------------------
    # Single query: fetch leads + eagerly load all their followups at once.
    # SQLAlchemy resolves the latest followup in Python from the preloaded
    # collection — zero additional queries regardless of lead count.
    # -----------------------------------------------------------------------
    query = db.query(Lead).options(joinedload(Lead.followups))

    if status:
        query = query.filter(Lead.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Lead.full_name.ilike(like),
            Lead.phone.ilike(like),
            Lead.campaign_name.ilike(like),
            Lead.ad_name.ilike(like),
            Lead.platform.ilike(like),
            Lead.latest_reply_text.ilike(like),
        ))

    # Only fetch leads that have a next_followup_at set, or have at least one followup
    candidates = (
        query
        .order_by(Lead.updated_at.desc(), Lead.id.desc())
        .limit(5000)
        .all()
    )

    rows = []
    for lead in candidates:
        # Latest followup is already loaded — no extra query
        sorted_fus = sorted(lead.followups, key=lambda f: f.id, reverse=True)
        latest = sorted_fus[0] if sorted_fus else None

        due_raw = lead.next_followup_at or (latest.next_followup_date if latest else "")
        due = _parse_followup_date(due_raw)

        if not due:
            continue

        include = True
        if bucket == "overdue":
            include = due <= end
        elif bucket == "all":
            include = True
        else:
            if start and due < start:
                include = False
            if end and due > end:
                include = False

        if not include:
            continue

        rows.append({
            "lead": LeadOut.model_validate(lead).model_dump(),
            "due_date": due.isoformat(),
            "followup": {
                "id": latest.id if latest else None,
                "followup_no": latest.followup_no if latest else None,
                "followup_date": latest.followup_date if latest else "",
                "response": latest.response if latest else "",
                "confirmed": latest.confirmed if latest else "",
                "seminar_date": latest.seminar_date if latest else "",
                "next_followup_date": latest.next_followup_date if latest else due_raw,
                "remarks": latest.remarks if latest else "",
                "created_at": str(latest.created_at) if latest else "",
            },
        })

    rows.sort(key=lambda r: (r["due_date"] or "9999-99-99", -(r["lead"]["id"] or 0)))
    return {"total": len(rows), "rows": rows[:limit]}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@app.get("/reports/summary")
def reports(db: Session = Depends(get_db), user: str = Depends(require_user)):
    total = db.query(Lead).count()
    sent = db.query(Lead).filter(Lead.whatsapp_sent == True).count()
    delivered = db.query(Lead).filter(Lead.whatsapp_delivered == True).count()
    unread = db.query(Lead).filter(Lead.unread_count > 0).count()
    from sqlalchemy import case as sa_case
    # Normalise status case so 'new' and 'New' merge into one bucket
    status_label = func.initcap(func.lower(Lead.status))
    statuses = db.query(status_label, func.count(Lead.id)).group_by(status_label).all()
    days = db.query(Lead.seminar_day, func.count(Lead.id)).filter(Lead.seminar_day.isnot(None), Lead.seminar_day != "").group_by(Lead.seminar_day).all()
    return {
        "total": total, "sent": sent, "delivered": delivered, "unread": unread,
        "by_status": dict(statuses), "by_day": dict(days),
    }


@app.get("/reports/seminar")
def reports_seminar(
    date_from: str = None,
    date_to: str = None,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Returns per-seminar-date breakdown with:
      - total leads registered
      - attended
      - confirmed (will attend)
      - call_picked   (any response that is NOT a call-not-picked variant)
      - call_not_picked
      - missed
    Filtered by seminar_date text range (parsed as DD Month YYYY).

    Query params:
      date_from / date_to  -- ISO dates YYYY-MM-DD, inclusive
    """
    from datetime import date as _date, datetime as _dt
    import re as _re

    # Fetch every lead that has a seminar_date set
    q = db.query(Lead).filter(Lead.seminar_date.isnot(None), Lead.seminar_date != "")
    all_leads = q.all()

    def parse_seminar_date(s):
        """Parse 'Thursday, 28 May 2026' -> date object, or None."""
        try:
            # strip weekday prefix if present
            parts = s.split(", ", 1)
            date_part = parts[-1].strip()
            return _dt.strptime(date_part, "%d %B %Y").date()
        except Exception:
            return None

    # Parse filter bounds
    from_date = _dt.strptime(date_from, "%Y-%m-%d").date() if date_from else None
    to_date   = _dt.strptime(date_to,   "%Y-%m-%d").date() if date_to   else None

    # Group leads by seminar_date string
    from collections import defaultdict
    buckets = defaultdict(list)
    for lead in all_leads:
        d = parse_seminar_date(lead.seminar_date)
        if d is None:
            continue
        if from_date and d < from_date:
            continue
        if to_date and d > to_date:
            continue
        buckets[lead.seminar_date].append(lead)

    NOT_PICKED_KEYWORDS = [
        "not picked", "switched off", "not reachable", "wrong number",
    ]

    def is_not_picked(response):
        if not response:
            return False
        r = response.lower()
        return any(k in r for k in NOT_PICKED_KEYWORDS)

    rows = []
    for seminar_date_str, leads in sorted(
        buckets.items(),
        key=lambda x: parse_seminar_date(x[0]) or _date.min
    ):
        total        = len(leads)
        attended     = sum(1 for l in leads if (l.confirmed or "").lower() == "attended")
        confirmed    = sum(1 for l in leads if (l.confirmed or "").lower() == "confirmed")
        missed       = sum(1 for l in leads if (l.confirmed or "").lower() == "missed")
        call_np      = sum(1 for l in leads if is_not_picked(l.response))
        call_picked  = sum(1 for l in leads if l.response and not is_not_picked(l.response))

        rows.append({
            "seminar_date":   seminar_date_str,
            "total":          total,
            "attended":       attended,
            "confirmed":      confirmed,
            "missed":         missed,
            "call_picked":    call_picked,
            "call_not_picked":call_np,
        })

    return {"rows": rows}


# ---------------------------------------------------------------------------
# WhatsApp Inbox
# ---------------------------------------------------------------------------

@app.get("/whatsapp/conversations")
def conversations(db: Session = Depends(get_db), user: str = Depends(require_user)):
    leads = (
        db.query(Lead)
        .filter(or_(Lead.latest_reply_text.isnot(None), Lead.whatsapp_message_id.isnot(None)))
        .order_by(Lead.unread_count.desc(), Lead.updated_at.desc())
        .limit(300)
        .all()
    )
    return {"rows": [LeadOut.model_validate(l).model_dump() for l in leads]}


@app.get("/whatsapp/conversations/{phone}")
def thread(phone: str, db: Session = Depends(get_db), user: str = Depends(require_user)):
    p = clean_phone(phone)
    msgs = db.query(WhatsAppMessage).filter(WhatsAppMessage.phone == p).order_by(WhatsAppMessage.id.asc()).all()
    lead = db.query(Lead).filter(Lead.phone == p).order_by(Lead.id.desc()).first()
    if lead:
        lead.unread_count = 0
        db.commit()
    return {
        "lead": LeadOut.model_validate(lead).model_dump() if lead else None,
        "messages": [
            {
                "id": m.id, "wa_message_id": m.wa_message_id, "direction": m.direction,
                "body": m.body, "status": m.status, "message_type": m.message_type,
                "timestamp": m.timestamp, "created_at": str(m.created_at),
            }
            for m in msgs
        ],
    }


@app.post("/whatsapp/reply")
def reply(data: ReplyIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = (
        db.get(Lead, data.lead_id)
        if data.lead_id
        else db.query(Lead).filter(Lead.phone == clean_phone(data.phone)).order_by(Lead.id.desc()).first()
    )
    return send_text_reply(db, data.phone, data.text, lead)


# ---------------------------------------------------------------------------
# Meta webhook
# ---------------------------------------------------------------------------

@app.get("/webhook/meta-leads")
def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == settings.meta_verify_token:
        return int(params.get("hub.challenge", "0"))
    raise HTTPException(status_code=403, detail="Invalid verify token")


@app.post("/webhook/meta-leads")
async def meta_webhook(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    payload = await request.json()
    print("Webhook hit:", payload)
    result = classify_webhook_and_handle(db, payload)
    print("Webhook result:", result)
    return {"success": True, "result": result}


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

@app.post("/maintenance/backfill-whatsapp-chatbox")
def backfill_whatsapp_chatbox(db: Session = Depends(get_db), user: str = Depends(require_user)):
    """Create outgoing chat bubbles for older leads whose WhatsApp template was already sent."""
    leads = db.query(Lead).filter(Lead.whatsapp_sent == True).all()
    created_or_updated = 0
    skipped = 0
    for lead in leads:
        if not lead.phone:
            skipped += 1
            continue
        save_outgoing_template_message(db, lead)
        created_or_updated += 1
    return {"success": True, "created_or_updated": created_or_updated, "skipped": skipped}


@app.post("/maintenance/backfill-meta-metadata")
def backfill_meta_metadata(
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    """
    Re-fetch old Meta leads to fill campaign name, ad name, form name and FB/IG source.
    This does not resend WhatsApp messages.
    """
    leads = (
        db.query(Lead)
        .filter(Lead.meta_lead_id.isnot(None))
        .order_by(Lead.id.desc())
        .limit(limit)
        .all()
    )

    updated = 0
    errors = []
    for lead in leads:
        try:
            before = (lead.campaign_name, lead.ad_name, lead.form_name, lead.platform)
            upsert_lead_from_meta(db, str(lead.meta_lead_id), auto_send=False)
            db.refresh(lead)
            after = (lead.campaign_name, lead.ad_name, lead.form_name, lead.platform)
            if before != after:
                updated += 1
        except Exception as exc:
            errors.append({"lead_id": lead.id, "meta_lead_id": lead.meta_lead_id, "error": str(exc)})

    return {"success": True, "checked": len(leads), "updated": updated, "errors": errors[:20]}


# ---------------------------------------------------------------------------
# Test endpoints — gated by TEST_ENDPOINTS_ENABLED env flag
# ---------------------------------------------------------------------------

def _require_test_mode():
    """Raises 403 if TEST_ENDPOINTS_ENABLED is not explicitly set to true."""
    enabled = os.getenv("TEST_ENDPOINTS_ENABLED", "false").lower() in ("true", "1", "yes")
    if not enabled:
        raise HTTPException(status_code=403, detail="Test endpoints disabled. Set TEST_ENDPOINTS_ENABLED=true to enable.")


@app.post("/test/whatsapp")
def test_whatsapp(data: TestWhatsAppIn, db: Session = Depends(get_db)):
    _require_test_mode()
    seminar = get_seminar_details(data.please_choose_a_day_for_the_free_seminar, data.model_dump())
    lead = Lead(
        full_name=data.name, phone=clean_phone(data.phone), campaign_name="Test",
        preferred_day=data.please_choose_a_day_for_the_free_seminar,
        raw={"source": "test"}, **seminar,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return {"success": True, "lead_id": lead.id, "seminar": seminar, "whatsapp": send_template_for_lead(db, lead)}


@app.post("/test/meta-lead/{lead_id}")
def test_meta_lead(lead_id: str, db: Session = Depends(get_db)):
    _require_test_mode()
    lead, wa = upsert_lead_from_meta(db, lead_id, auto_send=True)
    return {"success": True, "lead": LeadOut.model_validate(lead).model_dump(), "whatsapp": wa}
