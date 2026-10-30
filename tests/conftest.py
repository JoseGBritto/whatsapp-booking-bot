import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from booking.business import Business  # noqa: E402
from booking.db import Database  # noqa: E402
from booking.flow import Assistant  # noqa: E402

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
OWNER = "5492990000000"
ANA = "5491111111111"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def business():
    return Business.load(ROOT / "business.json")


@pytest.fixture
def clock():
    return Clock(datetime(2026, 11, 2, 8, 0, tzinfo=TZ))  # Monday 08:00


@pytest.fixture
def bot(business, clock):
    return Assistant(business, Database(), OWNER, clock)


class Chat:
    """Helper that talks to the assistant like a WhatsApp user would."""

    def __init__(self, bot, phone=ANA, name="Ana López"):
        self.bot, self.phone, self.name, self.n = bot, phone, name, 0

    def _send(self, **fields):
        self.n += 1
        return self.bot.handle({"id": f"{self.phone}-{self.n}", "from": self.phone, "name": self.name, **fields})

    def say(self, text):
        return self._send(type="text", text=text)

    def tap(self, reply_id):
        return self._send(type="reply", reply_id=reply_id, text="")

    def to_me(self, out):
        return [m for to, m in out if to == self.phone]


def ids(message):
    if message["kind"] == "list":
        return [r[0] for r in message["rows"]]
    if message["kind"] == "buttons":
        return [o[0] for o in message["options"]]
    return []
