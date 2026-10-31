from datetime import timedelta

from conftest import ANA, OWNER, Chat, ids

from booking.db import Database
from booking.flow import Assistant


def book_first_slot(chat, service="facial", day="2026-11-02"):
    chat.tap("menu:book")
    chat.tap(f"svc:{service}")
    out = chat.tap(f"day:{service}:{day}")
    first_slot = ids(chat.to_me(out)[0])[0]
    return first_slot, chat.tap(first_slot)


def test_greeting_shows_menu_with_profile_name(bot):
    out = Chat(bot).say("Hola!")
    msg = out[0][1]
    assert msg["kind"] == "list" and "¡Hola Ana!" in msg["body"]
    assert ids(msg) == ["menu:book", "menu:mine", "menu:prices", "menu:info", "menu:human"]


def test_full_booking_flow_notifies_owner(bot):
    chat = Chat(bot)
    slot, out = book_first_slot(chat)
    assert slot == "slot:facial:2026-11-02T10:00:00-03:00"  # 08:00 + 2 h notice
    assert "nombre" in out[0][1]["body"]
    out = chat.say("ana lópez")
    assert out[0][1]["kind"] == "buttons" and "Lunes 2/11 a las 10:00" in out[0][1]["body"]
    out = chat.tap("confirm")
    assert "✅" in out[0][1]["body"]
    assert out[1][0] == OWNER and "Nuevo turno #1: Ana López" in out[1][1]["body"]
    assert bot.db.upcoming_for(ANA, bot.clock())[0].customer_name == "Ana López"


def test_returning_customer_skips_name_question(bot):
    chat = Chat(bot)
    book_first_slot(chat)
    chat.say("Ana López")
    chat.tap("confirm")
    _, out = book_first_slot(chat, service="cejas")
    assert out[0][1]["kind"] == "buttons" and "Confirmar" in [o[1] for o in out[0][1]["options"]]


def test_invalid_name_is_rejected(bot):
    chat = Chat(bot)
    book_first_slot(chat)
    assert "solo tu nombre" in chat.say("1234 @@")[0][1]["body"]


def test_two_customers_cannot_take_the_same_slot(bot):
    ana, bea = Chat(bot), Chat(bot, phone="5491122222222", name="Bea")
    slot, _ = book_first_slot(ana)
    book_first_slot(bea)
    ana.say("Ana López")
    bea.say("Bea Ruiz")
    assert "✅" in ana.tap("confirm")[0][1]["body"]
    out = bea.tap("confirm")
    assert "alguien reservó ese horario" in out[0][1]["body"]
    assert slot not in ids(out[1][1])  # the taken slot is no longer offered


def test_stale_slot_is_refused(bot):
    chat = Chat(bot)
    out = chat.tap("slot:facial:2026-11-02T08:30:00-03:00")  # inside the 2 h notice window
    assert "ya no está disponible" in out[0][1]["body"]


def test_long_days_are_paginated(bot):
    chat = Chat(bot)
    out = chat.tap("day:cejas:2026-11-06")  # Friday 09-18, 20 min service: 18 slots
    rows = ids(chat.to_me(out)[0])
    assert len(rows) == 10 and rows[-1] == "more:cejas:2026-11-06:9"
    rest = ids(chat.to_me(chat.tap(rows[-1]))[0])
    assert len(rest) == 9 and not any(r.startswith("more") for r in rest)


def test_my_appointments_and_cancellation(bot):
    chat = Chat(bot)
    book_first_slot(chat)
    chat.say("Ana López")
    chat.tap("confirm")
    out = chat.say("quiero ver mis turnos")  # must not start a new booking
    rows = ids(out[0][1])
    assert rows == ["cxl:1"]
    assert ids(chat.tap("cxl:1")[0][1]) == ["cxlok:1", "menu:home"]
    out = chat.tap("cxlok:1")
    assert "cancelado" in out[0][1]["body"] and out[1][0] == OWNER
    assert bot.db.upcoming_for(ANA, bot.clock()) == []


def test_customers_cannot_cancel_other_peoples_appointments(bot):
    ana, eve = Chat(bot), Chat(bot, phone="5491199999999", name="Eve")
    book_first_slot(ana)
    ana.say("Ana López")
    ana.tap("confirm")
    eve.tap("cxlok:1")
    assert bot.db.get(1).status == "booked"


def test_faq_answers(bot):
    chat = Chat(bot)
    assert "Limpieza facial (60 min): $28.000" in chat.say("cuánto sale?")[0][1]["body"]
    assert "Av. Argentina 450" in chat.say("¿Dónde están ubicados?")[0][1]["body"]
    assert "Mercado Pago" in chat.say("puedo pagar con transferencia")[0][1]["body"]


def test_unknown_text_falls_back_to_menu(bot):
    out = Chat(bot).say("asdf qwerty")
    assert "No te entendí" in out[0][1]["body"] and out[1][1]["kind"] == "list"


def test_handoff_pauses_bot_until_customer_or_owner_resumes(bot, clock):
    chat, owner = Chat(bot), Chat(bot, phone=OWNER)
    out = chat.say("quiero hablar con una persona")
    assert out[1][0] == OWNER and "wa.me/" + ANA in out[1][1]["body"]
    assert chat.say("tengo una consulta sobre mi piel") == []  # the human is answering
    assert "reactivado" in owner.say(f"reactivar {ANA}")[0][1]["body"]
    assert chat.say("turno")[0][1]["kind"] == "list"

    chat.say("hablar con un asesor")
    clock.now += timedelta(hours=13)  # pause expires on its own
    assert chat.say("hola")[0][1]["kind"] == "list"


def test_owner_agenda(bot, clock):
    chat, owner = Chat(bot), Chat(bot, phone=OWNER)
    book_first_slot(chat)
    chat.say("Ana López")
    chat.tap("confirm")
    out = owner.say("agenda")
    assert "Agenda del Lun 2/11 (1 turnos)" in out[0][1]["body"] and "10:00 Limpieza facial — Ana López" in out[0][1]["body"]
    assert "No hay turnos" in owner.say("agenda mañana")[0][1]["body"]
    assert "Comandos" in owner.say("?")[0][1]["body"]


def test_non_text_messages_get_a_friendly_reply(bot):
    out = bot.handle({"id": "x", "from": ANA, "name": "", "type": "other"})
    assert "solo leo mensajes de texto" in out[0][1]["body"]


def test_without_owner_no_notifications(business, clock):
    bot = Assistant(business, Database(), "", clock)
    chat = Chat(bot)
    book_first_slot(chat)
    chat.say("Ana López")
    assert len(chat.tap("confirm")) == 1


def test_name_step_does_not_swallow_commands(bot):
    chat = Chat(bot)
    book_first_slot(chat)
    out = chat.say("mejor quiero ver mis turnos")
    assert out[0][1]["kind"] == "buttons" and "No tenés turnos" in out[0][1]["body"]


def test_handoff_message_does_not_promise_the_same_chat(bot):
    out = Chat(bot).say("quiero hablar con una persona")
    assert "una persona te va a escribir" in out[0][1]["body"]


def test_times_are_stored_in_utc_and_shown_in_local_time(bot):
    chat = Chat(bot)
    book_first_slot(chat)
    chat.say("Ana López")
    chat.tap("confirm")
    raw = bot.db.conn.execute("SELECT start FROM appointments").fetchone()[0]
    assert raw == "2026-11-02T13:00:00+00:00"           # 10:00 in Buenos Aires
    assert f"{bot.db.get(1).start:%H:%M}" == "10:00"


def test_bookings_stay_correct_across_a_daylight_saving_change(business):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from conftest import Clock
    business.timezone = "Europe/Madrid"  # clocks went back on 25 Oct 2026
    madrid = ZoneInfo("Europe/Madrid")
    bot = Assistant(business, Database(), OWNER, Clock(datetime(2026, 10, 24, 8, 0, tzinfo=madrid)))
    before = Chat(bot)
    before.tap("slot:facial:2026-10-26T10:00:00+01:00")  # booked while still on summer time (+02:00)
    before.say("Ana López")
    before.tap("confirm")
    other = Chat(bot, phone="5491133333333", name="Eve")
    out = other.tap("day:facial:2026-10-26")
    assert "slot:facial:2026-10-26T10:00:00+01:00" not in ids(other.to_me(out)[0])
    assert "slot:facial:2026-10-26T11:00:00+01:00" in ids(other.to_me(out)[0])


def test_names_that_contain_keywords_are_accepted(bot):
    chat = Chat(bot)
    book_first_slot(chat)
    out = chat.say("Felicitas Gómez")  # contains "cita"
    assert out[0][1]["kind"] == "buttons" and "Felicitas Gómez" in out[0][1]["body"]


def test_keywords_only_match_at_word_start(bot):
    out = Chat(bot).say("Felicitas")
    assert "No te entendí" in out[0][1]["body"]


def test_hola_does_not_interrupt_a_human_conversation(bot):
    chat = Chat(bot)
    chat.say("quiero hablar con una persona")
    assert chat.say("hola") == []
    assert chat.say("menu")[0][1]["kind"] == "list"


def test_more_days_row_reaches_the_whole_booking_window(bot):
    chat = Chat(bot)
    rows = ids(chat.to_me(chat.tap("svc:cejas"))[0])
    assert len(rows) == 10 and rows[-1] == "days:cejas:9"
    rest = ids(chat.to_me(chat.tap(rows[-1]))[0])
    assert rest and all(r.startswith("day:cejas:") for r in rest)
    assert rest[-1] == "day:cejas:2026-11-16"  # last open day inside the 14-day window
