import hmac
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import security, shop
from .config import Settings
from .db import DB
from .rpc import Rpc
from .worker import Runner

settings = Settings.from_env()
db = DB(settings.db_path)
rpc = Rpc(settings.rpc_url)
runner = Runner(db, rpc, settings)
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
log = logging.getLogger("arcchime")


def seed_demo():
    """Re-create the demo endpoint after a restart (useful on hosts with a non-persistent disk)."""
    if not (settings.demo_address and settings.demo_url and settings.demo_secret):
        return
    if not ADDR_RE.match(settings.demo_address):
        log.warning("DEMO_ADDRESS is not a valid address; skipping seed")
        return
    if not settings.demo_url.startswith(("http://", "https://")):
        log.warning("DEMO_URL must start with http:// or https://; skipping seed")
        return
    db.ensure_endpoint(settings.demo_address, settings.demo_url, settings.demo_secret)


LANDING = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ArcChime</title>
<style>:root{color-scheme:light dark}body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}
code,pre{background:rgba(127,127,127,.15);padding:.1em .35em;border-radius:4px}pre{padding:.8em;overflow:auto}
.ok{color:#1a7f37}.bad{color:#cf222e}</style></head><body>
<h1>ArcChime</h1><p>Signed webhooks for USDC payments on Arc. One normalized event per payment,
whether USDC moved natively or through the ERC-20 interface.</p>
<h3>Live status</h3><pre id="s">loading&hellip;</pre>
<p><a href="/shop">Try the demo shop</a> &middot; <a href="/docs">API docs</a> &middot; <a href="/health">/health</a></p>
<script>fetch('/health').then(r=>r.json()).then(h=>{document.getElementById('s').textContent=
JSON.stringify(h,null,2)}).catch(()=>{document.getElementById('s').textContent='unreachable'})</script>
</body></html>"""


@asynccontextmanager
async def lifespan(app):
    seed_demo()
    if settings.run_workers:
        runner.start()
    yield
    runner.stop()


app = FastAPI(title="ArcChime", version="0.1.0",
              description="Signed webhooks for USDC payments on Arc.", lifespan=lifespan)


def auth(authorization: Optional[str] = Header(default=None)):
    if not settings.admin_token:
        return
    expected = f"Bearer {settings.admin_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


class EndpointIn(BaseModel):
    address: str
    url: str


def create_endpoint_impl(address, url):
    if not ADDR_RE.match(address):
        raise HTTPException(status_code=422, detail="address must be a 0x-prefixed 40-hex-char address")
    try:
        security.validate_webhook_url(url, settings.allow_private_urls, settings.allow_http_urls)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"invalid webhook url: {e}")
    return db.create_endpoint(address, url)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing():
    return LANDING


SECRET_MESSAGE = os.environ.get(
    "SHOP_SECRET_MESSAGE", "You found it. This message was unlocked by a real USDC payment on Arc.")


def _shop_ready():
    return bool(settings.demo_address and settings.demo_secret)


@app.get("/shop", response_class=HTMLResponse, include_in_schema=False)
def shop_page():
    if not _shop_ready():
        return "<p>Demo shop is not configured on this server.</p>"
    net = "Arc Mainnet" if settings.chain_id == 5042 else f"Arc (chain {settings.chain_id})"
    return (shop.PAGE.replace("__NETWORK__", net).replace("__ADDRESS__", settings.demo_address)
            .replace("__CHAIN_ID__", str(settings.chain_id)))


@app.post("/shop/orders", include_in_schema=False)
def shop_create_order():
    if not _shop_ready():
        raise HTTPException(status_code=503, detail="shop not configured")
    order = db.create_order(shop.PRICE_MICRO, shop.ORDER_TTL, shop.MAX_PENDING)
    if not order:
        raise HTTPException(status_code=429, detail="too many open orders, try again later")
    return {"id": order["id"], "amount": shop.fmt_amount(order["amount_micro"]),
            "address": settings.demo_address, "expires_in": shop.ORDER_TTL}


@app.get("/shop/orders/{order_id}", include_in_schema=False)
def shop_order_status(order_id: str):
    o = db.get_order(order_id)
    if not o:
        raise HTTPException(status_code=404, detail="order not found")
    out = {"status": o["status"], "amount": shop.fmt_amount(o["amount_micro"]),
           "expires_in": max(0, int(o["expires_at"] - time.time()))}
    if o["status"] == "paid":
        out.update({"message": SECRET_MESSAGE, "tx_hash": o["tx_hash"]})
        ev = db.get_event(o["event_id"]) if o.get("event_id") else None
        if ev:   # show the visitor the actual event ArcChime delivered
            d = ev["data"]
            out["event"] = {k: d.get(k) for k in
                            ("id", "type", "chain", "source", "from", "amount", "tx_hash", "block_number")}
    return out


@app.post("/shop/webhook", include_in_schema=False)
async def shop_webhook(request: Request):
    body = await request.body()
    code, msg = shop.handle_webhook(db, settings, body, request.headers.get("X-ArcChime-Signature"))
    if code != 200:
        raise HTTPException(status_code=code, detail=msg)
    return {"ok": True, "result": msg}


@app.get("/health")
def health():
    st = dict(runner.status)
    lag = None
    if st.get("head") is not None and st.get("last") is not None:
        lag = st["head"] - st["last"]
    return {"ok": st.get("error") is None, "chain": f"eip155:{settings.chain_id}",
            "confirmations": settings.confirmations, "last_scanned_block": st.get("last"),
            "head_minus_confirmations": st.get("head"), "lag_blocks": lag,
            "error": st.get("error"), **db.stats()}


@app.post("/v1/endpoints", status_code=201, dependencies=[Depends(auth)])
def create_endpoint(body: EndpointIn):
    """Register an address to watch. The signing secret is shown only in this response."""
    return create_endpoint_impl(body.address, body.url)


@app.get("/v1/endpoints", dependencies=[Depends(auth)])
def list_endpoints():
    return {"data": db.list_endpoints()}


@app.delete("/v1/endpoints/{endpoint_id}", dependencies=[Depends(auth)])
def delete_endpoint(endpoint_id: str):
    if not db.delete_endpoint(endpoint_id):
        raise HTTPException(status_code=404, detail="endpoint not found")
    return {"deleted": True}


@app.post("/v1/endpoints/{endpoint_id}/test", dependencies=[Depends(auth)])
def test_endpoint(endpoint_id: str):
    """Queue a signed `endpoint.test` event so you can check your receiver."""
    event_id = db.create_test_event(endpoint_id)
    if not event_id:
        raise HTTPException(status_code=404, detail="endpoint not found")
    return {"event_id": event_id}


@app.get("/v1/events", dependencies=[Depends(auth)])
def list_events(address: Optional[str] = None, limit: int = 50):
    return {"data": db.list_events(address, limit)}


@app.get("/v1/events/{event_id}", dependencies=[Depends(auth)])
def get_event(event_id: str):
    ev = db.get_event(event_id)
    if not ev:
        raise HTTPException(status_code=404, detail="event not found")
    return ev


@app.post("/v1/events/{event_id}/replay", dependencies=[Depends(auth)])
def replay_event(event_id: str, endpoint_id: Optional[str] = None):
    """Re-deliver an event (same id and payload; receivers should dedupe on the id)."""
    if not db.get_event(event_id):
        raise HTTPException(status_code=404, detail="event not found")
    return {"requeued": db.replay(event_id, endpoint_id)}
