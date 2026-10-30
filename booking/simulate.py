"""Try the whole conversation in the terminal, no WhatsApp account needed."""
from __future__ import annotations

import itertools
import tempfile
from pathlib import Path

from .business import Business
from .db import Database
from .flow import Assistant


def render(message: dict) -> list[tuple[str, str]]:
    """Prints a message and returns the numbered options [(number, reply_id)]."""
    print("\n🤖 " + message.get("body", "").replace("\n", "\n   "))
    options = []
    if message["kind"] == "buttons":
        options = [(i, t) for i, t in message["options"]]
    elif message["kind"] == "list":
        print(f"   [{message['button']}]")
        options = [(i, f"{t}" + (f" — {d}" if d else "")) for i, t, d in message["rows"]]
    numbered = []
    for n, (reply_id, label) in enumerate(options, start=1):
        print(f"   {n}. {label}")
        numbered.append((str(n), reply_id, label))
    return numbered


def main(business_file: Path, owner: str = "5490000000000") -> None:
    db = Database(Path(tempfile.mkdtemp()) / "sim.sqlite3")
    bot = Assistant(Business.load(business_file), db, owner)
    me, ids = "5491100000000", itertools.count(1)
    print("Simulated WhatsApp chat. Type a message or the number of an option. Ctrl+C to exit.")
    print("Tip: start with 'hola'. Messages for the business owner are shown with 📣.\n")
    options: list = []
    while True:
        try:
            said = input("\n🙋 ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        choice = next((o for o in options if o[0] == said), None)
        msg = ({"id": f"m{next(ids)}", "from": me, "name": "Ana López", "type": "reply", "reply_id": choice[1], "text": choice[2]}
               if choice else {"id": f"m{next(ids)}", "from": me, "name": "Ana López", "type": "text", "text": said})
        options = []
        for to, out in bot.handle(msg):
            if to == owner:
                print("\n📣 (to owner) " + out["body"].replace("\n", "\n   "))
            else:
                options = render(out) or options
