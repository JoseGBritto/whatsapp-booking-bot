import hashlib
import hmac
import json
from datetime import timedelta

import pytest

from conftest import ANA, OWNER, Chat

from booking.config import Settings
from booking.reminders import build_reminders, send_reminders
from booking.server import create_app
from booking.whatsapp import buttons, menu_list, parse_incoming, text, to_api_payload, verify_signature

SECRET = "app-secret"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def webhook(messages, contacts=None):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "contacts": contacts or [{"wa_id": ANA, "profile": {"name": "Ana López"}}], "messages": messages}}]}]}


class FakeClient:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def send(self, to, message):
        self.sent.append((to, message))
        return self.ok


def test_payloads_respect_whatsapp_limits():
    p = to_api_payload(ANA, buttons("Body", [("a", "A very long button title here"), ("b", "B"), ("c", "C"), ("d", "D")]))
    btns = p["interactive"]["action"]["buttons"]
    assert len(btns) == 3 and len(btns[0]["reply"]["title"]) == 20
    rows = [(f"r{i}", "Row title that is far too long", "") for i in range(12)]
    lst = to_api_payload(ANA, menu_list("Body", "Choose an option please", rows))["interactive"]["action"]
    assert len(lst["sections"][0]["rows"]) == 10 and len(lst["button"]) == 20
    assert "description" not in lst["sections"][0]["rows"][0]  # empty descriptions are omitted
    assert to_api_payload(ANA, text("hi"))["text"]["body"] == "hi"


def test_signature_verification():
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, "sha256=deadbeef")
    assert not verify_signature(SECRET, body, None)


def test_parse_incoming_text_list_and_template_buttons():
    msgs = parse_incoming(webhook([
        {"id": "1", "from": ANA, "type": "text", "text": {"body": "hola"}},
        {"id": "2", "from": ANA, "type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "svc:facial", "title": "Limpieza"}}},
        {"id": "3", "from": ANA, "type": "button", "button": {"payload": "menu:home", "text": "Ok"}},
        {"id": "4", "from": ANA, "type": "image", "image": {}},
    ]))
    assert [(m["type"], m["reply_id"] or m["text"]) for m in msgs] == [
        ("text", "hola"), ("reply", "svc:facial"), ("reply", "menu:home"), ("other", "")]
    assert msgs[0]["name"] == "Ana López"


@pytest.fixture
def server(bot):
    settings = Settings(verify_token="vt", app_secret=SECRET)
    client = FakeClient()
    return create_app(settings, bot, client).test_client(), client


def test_webhook_verification(server):
    app, _ = server
    ok = app.get("/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=123")
    assert ok.status_code == 200 and ok.get_data(as_text=True) == "123"
    assert app.get("/webhook?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=1").status_code == 403


def test_webhook_rejects_unsigned_requests(server):
    app, client = server
    body = json.dumps(webhook([{"id": "x", "from": ANA, "type": "text", "text": {"body": "hola"}}])).encode()
    assert app.post("/webhook", data=body, content_type="application/json").status_code == 401
    assert client.sent == []


def test_webhook_replies_once_even_if_meta_retries(server):
    app, client = server
    body = json.dumps(webhook([{"id": "wamid.1", "from": ANA, "type": "text", "text": {"body": "hola"}}])).encode()
    for _ in range(3):
        r = app.post("/webhook", data=body, content_type="application/json", headers={"X-Hub-Signature-256": sign(body)})
        assert r.status_code == 200
    assert len(client.sent) == 1 and client.sent[0][1]["kind"] == "list"


def book(bot):
    chat = Chat(bot)
    chat.tap("svc:facial")
    chat.tap("slot:facial:2026-11-03T10:00:00-03:00")
    chat.say("Ana López")
    chat.tap("confirm")


def test_reminder_inside_24h_window_uses_buttons(bot, clock):
    book(bot)
    clock.now += timedelta(hours=3)  # Mon 11:00; appointment Tue 10:00 is within 24 h
    (appt_id, phone, msg), = build_reminders(bot.biz, bot.db, clock.now)
    assert phone == ANA and msg["kind"] == "buttons" and ("cxl:1", "Cancelar turno") in msg["options"]


def test_reminder_outside_window_needs_a_template(bot, clock):
    book(bot)
    clock.now += timedelta(hours=23, minutes=45)
    clock.now -= timedelta(hours=0)
    bot.db.save_session(ANA, "idle", {}, last_inbound=(clock.now - timedelta(hours=30)).isoformat())
    assert build_reminders(bot.biz, bot.db, clock.now) == []
    (_, _, msg), = build_reminders(bot.biz, bot.db, clock.now, template="turno_recordatorio")
    assert msg["kind"] == "template" and msg["params"][0] == "Ana"
    assert to_api_payload(ANA, msg)["template"]["name"] == "turno_recordatorio"


def test_send_reminders_marks_only_successful_sends(bot, clock):
    book(bot)
    clock.now += timedelta(hours=3)
    assert send_reminders(bot.biz, bot.db, FakeClient(ok=False), clock.now) == 0
    assert send_reminders(bot.biz, bot.db, FakeClient(), clock.now) == 1
    assert send_reminders(bot.biz, bot.db, FakeClient(), clock.now) == 0  # not sent twice


def test_bookings_for_today_or_tomorrow_get_no_reminder(bot, clock):
    chat = Chat(bot)
    chat.tap("svc:facial")
    chat.tap("slot:facial:2026-11-02T16:00:00-03:00")  # this afternoon, booked just now
    chat.say("Ana López")
    out = chat.tap("confirm")
    assert "recordatorio" not in out[0][1]["body"]  # nor promised
    assert build_reminders(bot.biz, bot.db, clock.now) == []


def test_reminder_for_an_old_booking_happening_today_says_today(bot, clock):
    start = clock.now + timedelta(hours=8)
    bot.db.book(ANA, "Ana López", "facial", start, start + timedelta(hours=1), created_at=clock.now - timedelta(days=3))
    bot.db.save_session(ANA, "idle", {}, last_inbound=clock.now.isoformat())
    (_, _, msg), = build_reminders(bot.biz, bot.db, clock.now)
    assert msg["body"].startswith("⏰ Recordatorio: hoy tenés")
    assert ids_of(msg)[0].startswith("ok:")


def test_confirming_attendance_gets_an_acknowledgement(bot, clock):
    start = clock.now + timedelta(hours=26)
    appt_id = bot.db.book(ANA, "Ana López", "facial", start, start + timedelta(hours=1), created_at=clock.now - timedelta(days=3))
    out = Chat(bot).tap(f"ok:{appt_id}")
    assert out[0][1]["body"].startswith("¡Perfecto! Te esperamos")


def ids_of(message):
    return [o[0] for o in message["options"]]


def test_template_reminder_never_sends_an_empty_name(bot, clock):
    bot.db.book("5491144444444", "", "facial", *(clock.now + timedelta(hours=5), clock.now + timedelta(hours=6)))
    (_, _, msg), = build_reminders(bot.biz, bot.db, clock.now, template="turno_recordatorio")
    assert msg["params"][0] == "cliente"


def test_server_refuses_to_start_without_app_secret(bot):
    with pytest.raises(RuntimeError, match="WHATSAPP_APP_SECRET"):
        create_app(Settings(verify_token="vt"), bot, FakeClient())
    dev = create_app(Settings(verify_token="vt", allow_unsigned_webhooks=True), bot, FakeClient()).test_client()
    body = json.dumps(webhook([{"id": "dev1", "from": ANA, "type": "text", "text": {"body": "hola"}}])).encode()
    assert dev.post("/webhook", data=body, content_type="application/json").status_code == 200


def test_owner_alerts_use_the_template_when_configured(bot):
    client = FakeClient()
    settings = Settings(verify_token="vt", app_secret=SECRET, owner_phone=OWNER, owner_alert_template="aviso_dueno")
    app = create_app(settings, bot, client).test_client()
    body = json.dumps(webhook([{"id": "h1", "from": ANA, "type": "text", "text": {"body": "quiero hablar con una persona"}}])).encode()
    app.post("/webhook", data=body, content_type="application/json", headers={"X-Hub-Signature-256": sign(body)})
    to_owner = [m for to, m in client.sent if to == OWNER]
    assert to_owner and to_owner[0]["kind"] == "template" and to_owner[0]["name"] == "aviso_dueno"
    assert "\n" not in to_owner[0]["params"][0] and "wa.me" in to_owner[0]["params"][0]
    assert [m for to, m in client.sent if to == ANA][0]["kind"] == "text"  # the customer still gets plain text
