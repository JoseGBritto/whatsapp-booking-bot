"""Day-before reminders. Run every hour (cron, Railway cron, GitHub Actions, Task Scheduler)."""
from __future__ import annotations

from datetime import datetime, timedelta

from .business import Business
from .db import Database
from .flow import long_when
from .whatsapp import buttons


def build_reminders(business: Business, db: Database, now: datetime, template: str = "",
                    template_lang: str = "es_AR") -> list[tuple[int, str, dict]]:
    """Returns (appointment_id, phone, message) for every appointment in the next 24 h not yet reminded.

    WhatsApp only allows free-form messages within 24 h of the customer's last message; outside that
    window an approved template is required, so we fall back to it (or skip if none is configured).
    """
    out = []
    for appt in db.due_reminders(now):
        service = business.service(appt.service_id)
        name = service.name if service else appt.service_id
        last = db.get_session(appt.phone)["last_inbound"]
        inside_window = last and now - datetime.fromisoformat(last) < timedelta(hours=23, minutes=30)
        if inside_window:
            body = (f"⏰ Recordatorio: mañana tenés *{name}* el {long_when(appt.start)} en {business.address}.\n"
                    "¿Te esperamos?")
            message = buttons(body, [("menu:home", "Sí, ahí estaré"), (f"cxl:{appt.id}", "Cancelar turno")])
        elif template:
            message = {"kind": "template", "name": template, "lang": template_lang,
                       "params": [appt.customer_name.split()[0] if appt.customer_name else "", name,
                                  long_when(appt.start)]}
        else:
            continue
        out.append((appt.id, appt.phone, message))
    return out


def send_reminders(business: Business, db: Database, client, now: datetime, template: str = "",
                   template_lang: str = "es_AR") -> int:
    sent = 0
    for appt_id, phone, message in build_reminders(business, db, now, template, template_lang):
        if client.send(phone, message):
            db.mark_reminded(appt_id)
            sent += 1
    return sent
