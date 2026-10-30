from datetime import date, datetime, timedelta

from conftest import TZ

from booking.slots import available_slots, days_with_availability


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def test_slots_follow_split_opening_hours(business):
    facial = business.service("facial")  # 60 min, 30 min step
    monday = date(2026, 11, 2)
    slots = available_slots(business, facial, monday, [], at(monday, "07:00"))
    times = [f"{s:%H:%M}" for s in slots]
    assert times[0] == "09:00" and "12:00" in times and "12:30" not in times  # must end by 13:00
    assert "16:00" in times and times[-1] == "19:00"


def test_minimum_notice_and_existing_bookings(business):
    facial, monday = business.service("facial"), date(2026, 11, 2)
    booked = [(at(monday, "10:00"), at(monday, "11:00"))]
    slots = [f"{s:%H:%M}" for s in available_slots(business, facial, monday, booked, at(monday, "08:30"))]
    assert "10:00" not in slots and "09:30" not in slots and "10:30" not in slots  # overlaps
    assert slots[0] == "11:00"  # 08:30 + 2h notice = 10:30, and 10:30 overlaps the booking


def test_closed_days_and_dates(business):
    facial = business.service("facial")
    assert available_slots(business, facial, date(2026, 11, 8), [], at(date(2026, 11, 1), "08:00")) == []  # Sunday
    assert available_slots(business, facial, date(2026, 12, 25), [], at(date(2026, 12, 1), "08:00")) == []


def test_days_with_availability_skips_full_and_closed_days(business):
    eyebrows = business.service("cejas")
    days = days_with_availability(business, eyebrows, [], at(date(2026, 11, 7), "13:50"), limit=3)
    assert [d for d, _ in days] == [date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)]  # Sat afternoon + Sunday skipped
