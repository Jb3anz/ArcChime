import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import chain, security
from .rpc import RpcError

log = logging.getLogger("arcchime")


def backoff_delay(failures, poll, cap=30.0):
    """Seconds to wait before the next scan: `poll` when healthy, doubling per consecutive failure up to `cap`."""
    return poll if failures <= 0 else min(poll * (2 ** failures), cap)


def scan_once(db, rpc, settings, status=None):
    """Scan one block range. Returns (new_deliveries, still_behind)."""
    key = f"last_block:{settings.chain_id}"
    head = rpc.block_number() - settings.confirmations
    if status is not None:
        status["head"] = head
    stored = db.get_state(key)
    if stored is None:                       # fresh start: tip, minus optional backfill window
        start = max(head - max(settings.backfill_blocks, 0), 0)
        db.set_state(key, start)
        if status is not None:
            status["last"] = start
        if start >= head:
            return 0, False
        stored = str(start)
    last = int(stored)
    if head <= last:
        return 0, False
    end = min(head, last + settings.max_range)
    addrs = db.active_addresses()
    events = []
    if addrs:
        topics = [chain.TRANSFER_TOPIC, None, [chain.pad_address(a) for a in sorted(addrs)]]
        logs = rpc.get_logs([chain.ERC20_ADDR, chain.NATIVE_LOG_ADDR], topics, last + 1, end)
        events = chain.normalize_logs(logs, settings.chain_id)
    created = db.store_events(events, key, end)
    if status is not None:
        status["last"] = end
    return created, end < head


def deliver_due(db, rpc, settings, post=None, now=None):
    post = post or requests.post
    rows = db.due_deliveries(now if now is not None else time.time(), 50)

    def one(r):
        try:
            if r["attempts"] == 0 and settings.check_reorg and r["block_hash"]:
                try:
                    canonical_hash = rpc.block_hash(r["block_number"])
                except RpcError:
                    return  # can't verify right now; try again next loop
                if canonical_hash != r["block_hash"].lower():
                    db.mark_reorged(r["id"])
                    return
            try:
                # DEMO_URL is set by the operator (not by API users), so it may point at this
                # server itself (e.g. http://127.0.0.1:8000/shop/webhook). Everything else is guarded.
                if not (settings.demo_url and r["url"] == settings.demo_url):
                    security.validate_webhook_url(r["url"], settings.allow_private_urls,
                                                  settings.allow_http_urls)
            except ValueError as e:
                db.record_attempt(r["id"], False, None, f"blocked url: {e}", settings.retry_delays)
                return
            body = r["payload"].encode()
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "ArcChime/0.1",
                "X-ArcChime-Event-Id": r["event_id"],
                "X-ArcChime-Delivery-Attempt": str(r["attempts"] + 1),
                "X-ArcChime-Signature": security.sign(r["secret"], body),
            }
            resp = post(r["url"], data=body, headers=headers, timeout=10, allow_redirects=False)
            ok = 200 <= resp.status_code < 300
            db.record_attempt(r["id"], ok, resp.status_code, None if ok else f"HTTP {resp.status_code}",
                              settings.retry_delays)
        except Exception as e:
            db.record_attempt(r["id"], False, None, f"{type(e).__name__}: {str(e)[:200]}",
                              settings.retry_delays)

    if rows:
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(one, rows))
    return len(rows)


class Runner:
    def __init__(self, db, rpc, settings):
        self.db, self.rpc, self.settings = db, rpc, settings
        self.status = {"head": None, "last": None, "error": None, "rpc_chain_id": None}
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        for target in (self._scan_loop, self._deliver_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()

    def _scan_loop(self):
        verified = False
        failures = 0
        while not self._stop.is_set():
            try:
                if not verified:
                    cid = self.rpc.chain_id()
                    self.status["rpc_chain_id"] = cid
                    if cid != self.settings.chain_id:
                        self.status["error"] = (f"RPC chain id {cid} != configured "
                                                f"{self.settings.chain_id}; not scanning")
                        self._stop.wait(5)
                        continue
                    verified = True
                while True:
                    _, behind = scan_once(self.db, self.rpc, self.settings, self.status)
                    if not behind or self._stop.is_set():
                        break
                self.status["error"] = None
                failures = 0
            except Exception as e:
                failures += 1
                self.status["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                log.warning("scan error: %s", self.status["error"])
            self._stop.wait(backoff_delay(failures, self.settings.poll_seconds))

    def _deliver_loop(self):
        while not self._stop.is_set():
            try:
                deliver_due(self.db, self.rpc, self.settings)
            except Exception as e:
                log.warning("delivery loop error: %s", e)
            self._stop.wait(1.0)
