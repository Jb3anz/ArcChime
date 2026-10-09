"""Example webhook receiver (standard library only).

    set ARCCHIME_SECRET=whsec_...      (the secret returned when you registered the endpoint)
    py examples/receiver.py            (listens on port 9000)

It shows the three things every receiver should do:
  1. verify the signature over the RAW request body,
  2. reject stale timestamps (replay protection),
  3. dedupe on the event id (delivery is at-least-once).
"""
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["ARCCHIME_SECRET"]
TOLERANCE = 300
seen_ids = set()  # use a database in real life


def verify(body: bytes, header: str) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
        ts, sig = int(parts["t"]), parts["v1"]
    except Exception:
        return False
    if abs(time.time() - ts) > TOLERANCE:
        return False
    expected = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not verify(body, self.headers.get("X-ArcChime-Signature", "")):
            self.send_response(401); self.end_headers(); return
        event = json.loads(body)
        if event["id"] in seen_ids:
            print("duplicate, ignoring", event["id"])
        else:
            seen_ids.add(event["id"])
            if event["type"] == "payment.received":
                print(f"PAID {event['amount']} USDC from {event['from']} (tx {event['tx_hash']})")
            else:
                print("event:", event["type"])
        self.send_response(200); self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("listening on :9000")
    HTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
