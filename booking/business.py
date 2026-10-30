"""Business profile (services, opening hours, FAQ) loaded from business.json."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


@dataclass
class Service:
    id: str
    name: str
    duration_minutes: int
    price: str = ""


@dataclass
class Business:
    name: str
    timezone: str
    services: list[Service]
    hours: dict[str, list[tuple[time, time]]]
    address: str = ""
    maps_url: str = ""
    slot_step_minutes: int = 30
    booking_window_days: int = 14
    min_notice_hours: float = 2
    closed_dates: set[date] = field(default_factory=set)
    faq: list[dict] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def service(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)

    def prices_text(self) -> str:
        lines = [f"• {s.name} ({s.duration_minutes} min): {s.price}" for s in self.services]
        return "Nuestros servicios:\n" + "\n".join(lines)

    def hours_text(self) -> str:
        lines = []
        for key, label in zip(WEEKDAYS, DAY_NAMES_ES):
            ranges = self.hours.get(key, [])
            text = " y ".join(f"{a:%H:%M} a {b:%H:%M}" for a, b in ranges) if ranges else "cerrado"
            lines.append(f"• {label}: {text}")
        return "Nuestros horarios:\n" + "\n".join(lines)

    def address_text(self) -> str:
        return f"Estamos en {self.address}." + (f"\nCómo llegar: {self.maps_url}" if self.maps_url else "")

    @classmethod
    def load(cls, path: Path | str) -> "Business":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        parse = lambda hhmm: time.fromisoformat(hhmm)  # noqa: E731
        return cls(
            name=data["name"],
            timezone=data.get("timezone", "UTC"),
            services=[Service(**s) for s in data["services"]],
            hours={d: [(parse(a), parse(b)) for a, b in data.get("hours", {}).get(d, [])] for d in WEEKDAYS},
            address=data.get("address", ""),
            maps_url=data.get("maps_url", ""),
            slot_step_minutes=int(data.get("slot_step_minutes", 30)),
            booking_window_days=int(data.get("booking_window_days", 14)),
            min_notice_hours=float(data.get("min_notice_hours", 2)),
            closed_dates={date.fromisoformat(d) for d in data.get("closed_dates", [])},
            faq=data.get("faq", []),
        )
