"""Conversation engine: one incoming message in, a list of (recipient, message) out.

It never calls the network, so the whole booking flow is unit-testable and can be tried
offline with `python -m booking simulate`.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Callable

from .business import DAY_NAMES_ES, Business
from .db import Appointment, Database, SlotTaken
from .slots import available_slots, days_with_availability
from .whatsapp import buttons, menu_list, text

Outgoing = list[tuple[str, dict]]
PAGE = 9            # list rows per page when there are more slots ("see more" uses the 10th row)
HUMAN_PAUSE_HOURS = 12

GREETINGS = {"hola", "buenas", "buen dia", "buenos dias", "buenas tardes", "buenas noches", "menu", "inicio", "hi", "hello", "volver"}
INTENTS = [  # order matters: "mis turnos" must not be read as "turno" (book)
    ("mine", ("mis turnos", "cancel", "mi turno", "reprogram")),
    ("human", ("humano", "persona", "asesor", "hablar con", "operador", "alguien")),
    ("book", ("turno", "reserv", "agend", "sacar", "cita")),
]


# Exact messages that may leave the "what's your name?" step; anything else is taken as the name,
# so a customer called "Felicitas" or "Reina" is never mistaken for a command.
EXIT_COMMANDS = GREETINGS | {"turno", "reservar"}
NAME_STEP_EXITS = ("mis turnos", "mi turno", "cancelar", "hablar con", "humano", "persona")


def has_keyword(said: str, keys) -> bool:
    """Keywords match at the start of a word: "cancel" matches "cancelar", "cita" doesn't match "Felicitas"."""
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k), said) for k in keys)


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower().strip())
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip(" .!?¡¿")


def short_day(d: date) -> str:
    return f"{DAY_NAMES_ES[d.weekday()][:3]} {d.day}/{d.month}"


def long_when(dt: datetime) -> str:
    return f"{DAY_NAMES_ES[dt.weekday()].lower()} {dt.day}/{dt.month} a las {dt:%H:%M}"


class Assistant:
    def __init__(self, business: Business, db: Database, owner_phone: str = "",
                 clock: Callable[[], datetime] | None = None):
        self.biz, self.db, self.owner = business, db, owner_phone
        db.use_timezone(business.tz)
        self.clock = clock or (lambda: datetime.now(business.tz))

    # ================================================================ entry point
    def handle(self, msg: dict) -> Outgoing:
        phone, now = msg["from"], self.clock()
        session = self.db.get_session(phone)
        self.db.save_session(phone, session["state"], session["data"], last_inbound=now.isoformat())

        if self.owner and phone == self.owner:
            return self._owner_command(msg.get("text", ""))

        if session["paused_until"] and now < datetime.fromisoformat(session["paused_until"]):
            if normalize(msg.get("text", "")) in {"menu", "bot"}:  # "hola" alone won't interrupt the person
                self.db.save_session(phone, "idle", {}, paused_until=None)
                return self._main_menu(phone, session["name"])
            return []  # a person from the team is handling this chat

        if msg.get("type") == "reply" and msg.get("reply_id"):
            return self._on_reply(phone, msg["reply_id"], session, msg.get("name", ""))
        if msg.get("type") == "text":
            return self._on_text(phone, msg["text"], session, msg.get("name", ""))
        return [(phone, text("Por ahora solo leo mensajes de texto 🙂 Escribí *menu* para ver las opciones."))]

    # ================================================================ free text
    def _on_text(self, phone: str, raw: str, session: dict, profile_name: str) -> Outgoing:
        said = normalize(raw)
        if session["state"] == "ask_name" and said not in EXIT_COMMANDS and not has_keyword(said, NAME_STEP_EXITS):
            name = re.sub(r"\s+", " ", raw.strip())[:40]
            if not re.fullmatch(r"[A-Za-zÀ-ÿñÑ' .-]{2,40}", name):
                return [(phone, text("Escribí solo tu nombre y apellido, por favor."))]
            self.db.save_session(phone, "confirm", session["data"], name=name.title())
            return self._confirm(phone, session["data"]["service"], session["data"]["start"], name.title())

        if said in GREETINGS:
            return self._main_menu(phone, session["name"] or profile_name)
        for intent, keys in INTENTS:
            if has_keyword(said, keys):
                return self._on_reply(phone, f"menu:{intent}", session, profile_name)
        answer = self._faq(said)
        if answer:
            return [(phone, text(answer + "\n\n¿Querés reservar un turno? Escribí *turno* o *menu*."))]
        return [(phone, text("No te entendí del todo 🙈")), *self._main_menu(phone, session["name"], greet=False)]

    def _faq(self, said: str) -> str | None:
        words = set(re.findall(r"[a-z0-9]+", said))
        for item in self.biz.faq:
            if words & {normalize(k) for k in item["keywords"]}:
                return {"__PRICES__": self.biz.prices_text(), "__HOURS__": self.biz.hours_text(),
                        "__ADDRESS__": self.biz.address_text()}.get(item["answer"], item["answer"])
        return None

    # ================================================================ buttons and list replies
    def _on_reply(self, phone: str, reply_id: str, session: dict, profile_name: str) -> Outgoing:
        kind, _, arg = reply_id.partition(":")
        if kind == "menu":
            return {
                "home": lambda: self._main_menu(phone, session["name"], greet=False),
                "book": lambda: self._choose_service(phone),
                "mine": lambda: self._my_appointments(phone),
                "prices": lambda: [(phone, text(self.biz.prices_text())), *self._after_info(phone)],
                "info": lambda: [(phone, text(self.biz.hours_text() + "\n\n" + self.biz.address_text())),
                                 *self._after_info(phone)],
                "human": lambda: self._handoff(phone, session["name"] or profile_name),
            }.get(arg, lambda: self._main_menu(phone, session["name"]))()
        if kind == "svc":
            return self._choose_day(phone, arg)
        if kind == "days":
            service_id, offset = arg.split(":")
            return self._choose_day(phone, service_id, int(offset))
        if kind == "ok":
            return self._confirm_attendance(phone, int(arg))
        if kind == "day":
            service_id, day = arg.split(":", 1)
            return self._choose_slot(phone, service_id, date.fromisoformat(day), 0)
        if kind == "more":
            service_id, day, offset = arg.split(":")
            return self._choose_slot(phone, service_id, date.fromisoformat(day), int(offset))
        if kind == "slot":
            service_id, start = arg.split(":", 1)
            return self._picked_slot(phone, service_id, start, session)
        if kind == "confirm":
            return self._book(phone, session)
        if kind == "cxl":
            return self._ask_cancel(phone, int(arg))
        if kind == "cxlok":
            return self._cancel(phone, int(arg))
        return self._main_menu(phone, session["name"])

    # ================================================================ screens
    def _main_menu(self, phone: str, name: str | None = None, greet: bool = True) -> Outgoing:
        first = f" {name.split()[0]}" if name else ""
        body = (f"¡Hola{first}! 👋 Soy el asistente virtual de *{self.biz.name}*.\n¿Qué necesitás?"
                if greet else "¿En qué te puedo ayudar?")
        self.db.save_session(phone, "menu", {})
        return [(phone, menu_list(body, "Ver opciones", [
            ("menu:book", "Reservar un turno", "Elegí servicio, día y horario"),
            ("menu:mine", "Mis turnos", "Ver o cancelar tus reservas"),
            ("menu:prices", "Servicios y precios", ""),
            ("menu:info", "Horarios y dirección", ""),
            ("menu:human", "Hablar con una persona", "Te responde alguien del equipo"),
        ]))]

    def _after_info(self, phone: str) -> Outgoing:
        return [(phone, buttons("¿Querés hacer algo más?", [("menu:book", "Reservar turno"), ("menu:home", "Ver menú")]))]

    def _choose_service(self, phone: str) -> Outgoing:
        self.db.save_session(phone, "choose_service", {})
        rows = [(f"svc:{s.id}", s.name, f"{s.duration_minutes} min · {s.price}") for s in self.biz.services]
        return [(phone, menu_list("¿Qué servicio querés reservar?", "Elegir servicio", rows, "Servicios"))]

    def _booked(self, now: datetime) -> list[tuple[datetime, datetime]]:
        return self.db.booked_intervals(now - timedelta(days=1), now + timedelta(days=self.biz.booking_window_days + 2))

    def _choose_day(self, phone: str, service_id: str, offset: int = 0) -> Outgoing:
        service = self.biz.service(service_id)
        if not service:
            return self._choose_service(phone)
        now = self.clock()
        all_days = days_with_availability(self.biz, service, self._booked(now), now,
                                          limit=self.biz.booking_window_days + 1)
        days = all_days[offset: offset + PAGE] if len(all_days) - offset > 10 else all_days[offset:]
        if not days:
            return [(phone, text(f"No quedan turnos libres para {service.name} en los próximos "
                                 f"{self.biz.booking_window_days} días 😕")), *self._after_info(phone)]
        self.db.save_session(phone, "choose_day", {"service": service_id})
        rows = [(f"day:{service_id}:{d.isoformat()}", short_day(d), f"{n} horario{'s' if n > 1 else ''} libre{'s' if n > 1 else ''}")
                for d, n in days]
        if offset + len(days) < len(all_days):
            rows.append((f"days:{service_id}:{offset + PAGE}", "Ver más días ›", ""))
        return [(phone, menu_list(f"*{service.name}* ({service.duration_minutes} min). ¿Qué día te queda bien?",
                                  "Elegir día", rows, "Días disponibles"))]

    def _choose_slot(self, phone: str, service_id: str, day: date, offset: int) -> Outgoing:
        service = self.biz.service(service_id)
        if not service:
            return self._choose_service(phone)
        now = self.clock()
        slots = available_slots(self.biz, service, day, self._booked(now), now)
        if not slots:
            return [(phone, text("Ese día ya no tiene horarios libres.")), *self._choose_day(phone, service_id)]
        page = slots[offset: offset + PAGE] if len(slots) - offset > 10 else slots[offset:]
        rows = [(f"slot:{service_id}:{s.isoformat()}", f"{s:%H:%M} hs", "") for s in page]
        if offset + len(page) < len(slots):
            rows.append((f"more:{service_id}:{day.isoformat()}:{offset + PAGE}", "Ver más horarios ›", ""))
        self.db.save_session(phone, "choose_slot", {"service": service_id, "day": day.isoformat()})
        return [(phone, menu_list(f"Horarios para el *{short_day(day)}*:", "Elegir horario", rows, "Horarios"))]

    def _picked_slot(self, phone: str, service_id: str, start_iso: str, session: dict) -> Outgoing:
        service, start, now = self.biz.service(service_id), datetime.fromisoformat(start_iso), self.clock()
        if not service or start not in available_slots(self.biz, service, start.date(), self._booked(now), now):
            return [(phone, text("Ese horario ya no está disponible.")),
                    *(self._choose_slot(phone, service_id, start.date(), 0) if service else self._choose_service(phone))]
        data = {"service": service_id, "start": start_iso}
        if session["name"]:
            self.db.save_session(phone, "confirm", data)
            return self._confirm(phone, service_id, start_iso, session["name"])
        self.db.save_session(phone, "ask_name", data)
        return [(phone, text("¿A nombre de quién hago la reserva? (nombre y apellido)"))]

    def _confirm(self, phone: str, service_id: str, start_iso: str, name: str) -> Outgoing:
        service, start = self.biz.service(service_id), datetime.fromisoformat(start_iso)
        body = (f"Revisá tu reserva:\n\n💆 *{service.name}*\n📅 {long_when(start).capitalize()}\n"
                f"👤 {name}\n💲 {service.price}\n📍 {self.biz.address}")
        return [(phone, buttons(body, [("confirm", "Confirmar"), (f"svc:{service_id}", "Cambiar horario"),
                                       ("menu:home", "Cancelar")]))]

    def _book(self, phone: str, session: dict) -> Outgoing:
        data = session["data"]
        if session["state"] != "confirm" or "start" not in data:
            return self._main_menu(phone, session["name"])
        service, start = self.biz.service(data["service"]), datetime.fromisoformat(data["start"])
        try:
            now = self.clock()
            appt_id = self.db.book(phone, session["name"], service.id, start,
                                   start + timedelta(minutes=service.duration_minutes), created_at=now)
        except SlotTaken:
            return [(phone, text("Uy, alguien reservó ese horario hace un instante 😅 Elegí otro:")),
                    *self._choose_slot(phone, service.id, start.date(), 0)]
        self.db.save_session(phone, "idle", {})
        out = [(phone, text(f"✅ ¡Listo, {session['name'].split()[0]}! Tu turno quedó reservado:\n\n"
                            f"*{service.name}* el {long_when(start)}.\n📍 {self.biz.address}\n\n"
                            + ("Te mando un recordatorio el día anterior. " if start - now >= timedelta(hours=24) else "")
                            + "Si necesitás cancelar, escribí *mis turnos*."))]
        return out + self._notify_owner(f"🆕 Nuevo turno #{appt_id}: {session['name']} (+{phone}) — "
                                        f"{service.name}, {long_when(start)}.")

    def _confirm_attendance(self, phone: str, appt_id: int) -> Outgoing:
        appt = self.db.get(appt_id)
        if not appt or appt.phone != phone or appt.status != "booked":
            return self._my_appointments(phone)
        return [(phone, text(f"¡Perfecto! Te esperamos el {long_when(appt.start)} 😊"))]

    def _my_appointments(self, phone: str) -> Outgoing:
        upcoming = self.db.upcoming_for(phone, self.clock())
        if not upcoming:
            return [(phone, buttons("No tenés turnos próximos.", [("menu:book", "Reservar turno"), ("menu:home", "Ver menú")]))]
        rows = [(f"cxl:{a.id}", f"{short_day(a.start)} {a.start:%H:%M}", self._service_name(a)) for a in upcoming[:10]]
        return [(phone, menu_list("Estos son tus próximos turnos. Tocá uno si querés cancelarlo.",
                                  "Ver mis turnos", rows, "Tus turnos"))]

    def _ask_cancel(self, phone: str, appt_id: int) -> Outgoing:
        appt = self.db.get(appt_id)
        if not appt or appt.phone != phone or appt.status != "booked":
            return self._my_appointments(phone)
        return [(phone, buttons(f"¿Cancelamos *{self._service_name(appt)}* del {long_when(appt.start)}?",
                                [(f"cxlok:{appt_id}", "Sí, cancelar"), ("menu:home", "No, volver")]))]

    def _cancel(self, phone: str, appt_id: int) -> Outgoing:
        appt = self.db.get(appt_id)
        if not appt or not self.db.cancel(appt_id, phone):
            return [(phone, text("Ese turno ya estaba cancelado o no existe."))]
        out = [(phone, buttons("Turno cancelado. ¡Gracias por avisar! ¿Querés sacar otro?",
                               [("menu:book", "Reservar turno"), ("menu:home", "Ver menú")]))]
        return out + self._notify_owner(f"❌ Cancelado turno #{appt_id}: {appt.customer_name} (+{phone}) — "
                                        f"{self._service_name(appt)}, {long_when(appt.start)}. El horario quedó libre.")

    def _handoff(self, phone: str, name: str | None) -> Outgoing:
        until = self.clock() + timedelta(hours=HUMAN_PAUSE_HOURS)
        self.db.save_session(phone, "human", {}, paused_until=until.isoformat())
        who = f"{name} (+{phone})" if name else f"+{phone}"
        return [(phone, text("Listo, le avisé al equipo y una persona te va a escribir a la brevedad 🙌\n"
                             "Si querés volver al asistente, escribí *menu*."))] + self._notify_owner(
            f"📲 {who} quiere hablar con una persona: https://wa.me/{phone}\n"
            f"El asistente queda en pausa {HUMAN_PAUSE_HOURS} h en ese chat. Para reactivarlo: *reactivar {phone}*")

    # ================================================================ owner tools
    def _owner_command(self, raw: str) -> Outgoing:
        said = normalize(raw)
        if said.startswith("reactivar"):
            target = re.sub(r"\D", "", said)
            if target:
                self.db.save_session(target, "idle", {}, paused_until=None)
                return [(self.owner, text(f"Asistente reactivado para +{target}."))]
        if said.startswith("agenda"):
            day = self.clock().date() + timedelta(days=1 if "manana" in said else 0)
            start = datetime.combine(day, datetime.min.time(), self.biz.tz)
            appts = self.db.between(start, start + timedelta(days=1))
            if not appts:
                return [(self.owner, text(f"No hay turnos para el {short_day(day)}."))]
            lines = [f"• {a.start:%H:%M} {self._service_name(a)} — {a.customer_name} (+{a.phone})" for a in appts]
            return [(self.owner, text(f"Agenda del {short_day(day)} ({len(appts)} turnos):\n" + "\n".join(lines)))]
        return [(self.owner, text("Comandos: *agenda*, *agenda mañana*, *reactivar <número>*"))]

    def _notify_owner(self, message: str) -> Outgoing:
        return [(self.owner, text(message))] if self.owner else []

    def _service_name(self, appt: Appointment) -> str:
        service = self.biz.service(appt.service_id)
        return service.name if service else appt.service_id
