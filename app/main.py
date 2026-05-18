from fastapi import FastAPI, Depends, HTTPException, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from typing import Optional
from .config import settings
from .db import get_db, init_db
from .models import Lead, FollowUp, WhatsAppMessage
from .auth import LoginIn, TokenOut, create_token, require_user
from .schemas import LeadOut, LeadCreate, LeadUpdate, FollowUpIn, ReplyIn, TestWhatsAppIn
from .utils import clean_phone, get_seminar_details, now_iso
from .whatsapp import send_template_for_lead, send_text_reply, save_outgoing_template_message
from .meta import classify_webhook_and_handle, upsert_lead_from_meta

app = FastAPI(title="WOI Lead CRM API", version="1.0.0")

origins = [settings.frontend_origin, "http://localhost:3000", "http://127.0.0.1:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(set(origins)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def _startup():
    init_db()

@app.get("/")
def root():
    return {"status": "ok", "service": "woi-lead-crm-backend"}

@app.get("/debug/config")
def debug_config():
    return {
        "db": "configured" if settings.database_url else "missing",
        "whatsapp_enabled": settings.whatsapp_enabled,
        "phone_number_id": settings.whatsapp_phone_number_id,
        "template": settings.whatsapp_template_name,
        "language": settings.whatsapp_language_code,
        "seminar_venue": settings.seminar_venue,
    }

@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn):
    if data.username != settings.admin_username or data.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return TokenOut(access_token=create_token(data.username))

@app.get("/leads")
def list_leads(
    q: str = "",
    status: str = "",
    day: str = "",
    unread: bool = False,
    limit: int = Query(100, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
):
    query = db.query(Lead)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Lead.full_name.ilike(like), Lead.phone.ilike(like), Lead.campaign_name.ilike(like), Lead.latest_reply_text.ilike(like)))
    if status:
        query = query.filter(Lead.status == status)
    if day:
        query = query.filter(Lead.seminar_day == day)
    if unread:
        query = query.filter(Lead.unread_count > 0)
    total = query.count()
    rows = query.order_by(Lead.id.desc()).offset(offset).limit(limit).all()
    return {"total": total, "rows": [LeadOut.model_validate(r).model_dump() for r in rows]}

@app.post("/leads", response_model=LeadOut)
def create_lead(data: LeadCreate, db: Session = Depends(get_db), user: str = Depends(require_user)):
    seminar = get_seminar_details(data.preferred_day, data.model_dump())
    lead = Lead(**data.model_dump(), phone=clean_phone(data.phone), raw={"source": "manual"}, **seminar)
    db.add(lead); db.commit(); db.refresh(lead)
    return lead

@app.patch("/leads/{lead_id}", response_model=LeadOut)
def update_lead(lead_id: int, data: LeadUpdate, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead: raise HTTPException(404, "Lead not found")
    for k, v in data.model_dump(exclude_unset=True).items():
        if k == "phone" and v: v = clean_phone(v)
        setattr(lead, k, v)
    db.commit(); db.refresh(lead)
    return lead

@app.post("/leads/{lead_id}/send-whatsapp")
def send_lead_whatsapp(lead_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead: raise HTTPException(404, "Lead not found")
    return send_template_for_lead(db, lead)

@app.get("/leads/{lead_id}/followups")
def get_followups(lead_id: int, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead: raise HTTPException(404, "Lead not found")
    rows = db.query(FollowUp).filter(FollowUp.lead_id == lead_id).order_by(FollowUp.id.asc()).all()
    return {"rows": [{"id": r.id, "lead_id": r.lead_id, "followup_no": r.followup_no, "followup_date": r.followup_date, "response": r.response, "confirmed": r.confirmed, "seminar_date": r.seminar_date, "next_followup_date": r.next_followup_date, "remarks": r.remarks, "created_at": str(r.created_at)} for r in rows]}

@app.post("/leads/{lead_id}/followups")
def add_followup(lead_id: int, data: FollowUpIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, lead_id)
    if not lead: raise HTTPException(404, "Lead not found")
    count = db.query(FollowUp).filter(FollowUp.lead_id == lead_id).count()
    fu = FollowUp(lead_id=lead_id, followup_no=count + 1, **data.model_dump())
    lead.status = data.confirmed or lead.status
    lead.next_followup_at = data.next_followup_date or lead.next_followup_at
    if data.remarks:
        lead.notes = ((lead.notes or "") + "\n" + data.remarks).strip()
    db.add(fu); db.commit()
    return {"success": True, "id": fu.id}

@app.get("/reports/summary")
def reports(db: Session = Depends(get_db), user: str = Depends(require_user)):
    total = db.query(Lead).count()
    sent = db.query(Lead).filter(Lead.whatsapp_sent == True).count()
    delivered = db.query(Lead).filter(Lead.whatsapp_delivered == True).count()
    unread = db.query(Lead).filter(Lead.unread_count > 0).count()
    statuses = db.query(Lead.status, func.count(Lead.id)).group_by(Lead.status).all()
    days = db.query(Lead.seminar_day, func.count(Lead.id)).group_by(Lead.seminar_day).all()
    return {"total": total, "sent": sent, "delivered": delivered, "unread": unread, "by_status": dict(statuses), "by_day": dict(days)}

@app.get("/whatsapp/conversations")
def conversations(db: Session = Depends(get_db), user: str = Depends(require_user)):
    leads = db.query(Lead).filter(or_(Lead.latest_reply_text.isnot(None), Lead.whatsapp_message_id.isnot(None))).order_by(Lead.unread_count.desc(), Lead.updated_at.desc()).limit(300).all()
    return {"rows": [LeadOut.model_validate(l).model_dump() for l in leads]}

@app.get("/whatsapp/conversations/{phone}")
def thread(phone: str, db: Session = Depends(get_db), user: str = Depends(require_user)):
    p = clean_phone(phone)
    msgs = db.query(WhatsAppMessage).filter(WhatsAppMessage.phone == p).order_by(WhatsAppMessage.id.asc()).all()
    lead = db.query(Lead).filter(Lead.phone == p).order_by(Lead.id.desc()).first()
    if lead:
        lead.unread_count = 0; db.commit()
    return {"lead": LeadOut.model_validate(lead).model_dump() if lead else None, "messages": [{"id": m.id, "wa_message_id": m.wa_message_id, "direction": m.direction, "body": m.body, "status": m.status, "message_type": m.message_type, "timestamp": m.timestamp, "created_at": str(m.created_at)} for m in msgs]}

@app.post("/whatsapp/reply")
def reply(data: ReplyIn, db: Session = Depends(get_db), user: str = Depends(require_user)):
    lead = db.get(Lead, data.lead_id) if data.lead_id else db.query(Lead).filter(Lead.phone == clean_phone(data.phone)).order_by(Lead.id.desc()).first()
    return send_text_reply(db, data.phone, data.text, lead)

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


@app.post("/test/whatsapp")
def test_whatsapp(data: TestWhatsAppIn, db: Session = Depends(get_db)):
    # Test endpoint intentionally does not need auth so Meta/Windows CMD testing is easy. Remove if desired.
    seminar = get_seminar_details(data.please_choose_a_day_for_the_free_seminar, data.model_dump())
    lead = Lead(full_name=data.name, phone=clean_phone(data.phone), campaign_name="Test", preferred_day=data.please_choose_a_day_for_the_free_seminar, raw={"source":"test"}, **seminar)
    db.add(lead); db.commit(); db.refresh(lead)
    return {"success": True, "lead_id": lead.id, "seminar": seminar, "whatsapp": send_template_for_lead(db, lead)}

@app.post("/test/meta-lead/{lead_id}")
def test_meta_lead(lead_id: str, db: Session = Depends(get_db)):
    lead, wa = upsert_lead_from_meta(db, lead_id, auto_send=True)
    return {"success": True, "lead": LeadOut.model_validate(lead).model_dump(), "whatsapp": wa}
