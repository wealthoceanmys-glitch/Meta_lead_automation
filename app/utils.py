from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import re
from .config import settings

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def clean_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if len(digits) == 10:
        return "91" + digits
    return digits

def first_nonempty(*vals):
    for v in vals:
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""

def field_map_from_meta(lead_data: dict):
    out = {}
    for f in lead_data.get("field_data", []) or []:
        name = f.get("name")
        values = f.get("values") or []
        out[name] = values[0] if values else ""
    return out

def detect_preferred_day(data: dict) -> str:
    text = " ".join(str(v or "") for v in data.values()).lower()
    if "sunday" in text or "sun" in text:
        return "Sunday"
    if "thursday" in text or "thu" in text:
        return "Thursday"
    return ""

def format_indian_date(d):
    return d.strftime("%A, %d %B %Y")

def next_weekday_date(now, target_weekday, event_start):
    days_ahead = target_weekday - now.weekday()  # Monday=0
    if days_ahead < 0:
        days_ahead += 7
    if days_ahead == 0 and now.time() >= event_start:
        days_ahead = 7
    return now.date() + timedelta(days=days_ahead)

def get_seminar_details(preferred_day: str = "", data: dict | None = None):
    tz = ZoneInfo(settings.seminar_timezone)
    now = datetime.now(tz)
    pref = (preferred_day or detect_preferred_day(data or {})).lower()

    schedules = {
        "thursday": {
            "label": "Thursday", "weekday": 3, "start": time(18, 0),
            "seminar_time": settings.seminar_thursday_time,
            "arrival_time": settings.seminar_thursday_arrival,
        },
        "sunday": {
            "label": "Sunday", "weekday": 6, "start": time(10, 30),
            "seminar_time": settings.seminar_sunday_time,
            "arrival_time": settings.seminar_sunday_arrival,
        },
    }
    if pref in schedules:
        cfg = schedules[pref]
    else:
        candidates = []
        for _, c in schedules.items():
            d = next_weekday_date(now, c["weekday"], c["start"])
            candidates.append((datetime.combine(d, c["start"], tzinfo=tz), c, d))
        _, cfg, chosen_date = min(candidates, key=lambda x: x[0])
        return {
            "seminar_day": cfg["label"], "seminar_date": format_indian_date(chosen_date),
            "seminar_time": cfg["seminar_time"], "arrival_time": cfg["arrival_time"],
            "venue": settings.seminar_venue,
        }
    d = next_weekday_date(now, cfg["weekday"], cfg["start"])
    return {
        "seminar_day": cfg["label"], "seminar_date": format_indian_date(d),
        "seminar_time": cfg["seminar_time"], "arrival_time": cfg["arrival_time"],
        "venue": settings.seminar_venue,
    }
