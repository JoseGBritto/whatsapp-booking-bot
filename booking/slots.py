"""Availability: which start times are free for a service on a given day."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .business import WEEKDAYS, Business, Service


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def available_slots(business: Business, service: Service, day: date,
                    booked: list[tuple[datetime, datetime]], now: datetime) -> list[datetime]:
    """Free start times on `day`, respecting opening hours, minimum notice and existing bookings."""
    if day in business.closed_dates:
        return []
    tz = business.tz
    duration = timedelta(minutes=service.duration_minutes)
    step = timedelta(minutes=business.slot_step_minutes)
    earliest = now + timedelta(hours=business.min_notice_hours)
    slots = []
    for open_t, close_t in business.hours.get(WEEKDAYS[day.weekday()], []):
        start = datetime.combine(day, open_t, tz)
        close = datetime.combine(day, close_t, tz)
        while start + duration <= close:
            end = start + duration
            if start >= earliest and not any(overlaps(start, end, b0, b1) for b0, b1 in booked):
                slots.append(start)
            start += step
    return slots


def days_with_availability(business: Business, service: Service, booked: list[tuple[datetime, datetime]],
                           now: datetime, limit: int = 10) -> list[tuple[date, int]]:
    """Next days (within the booking window) that have at least one free slot, with the count."""
    today = now.astimezone(business.tz).date()
    result = []
    for offset in range(business.booking_window_days + 1):
        day = today + timedelta(days=offset)
        count = len(available_slots(business, service, day, booked, now))
        if count:
            result.append((day, count))
        if len(result) == limit:
            break
    return result
