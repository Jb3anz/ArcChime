import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from arcchime import chain, security
from arcchime.config import Settings
from arcchime.db import DB
from arcchime.rpc import RpcError
from arcchime.worker import backoff_delay, deliver_due, scan_once

ME = "0x8e2da282cc5832a3c9ab760c808f4be50ac7c856"
SENDER = "0xce3ba9846e4bcfbef1bf611c82dc418ccaa5068a0"
TRANSFER = chain.TRANSFER_TOPIC


def pad(a):
    return "0x" + a[2:].rjust(64, "0")


def make_log(emitter, frm, to, value, block, idx, tx="0x" + "11" * 32, bhash=None):
    return {"address": emitter, "topics": [TRANSFER, pad(frm), pad(to)],
            "data": "0x" + hex(value)[2:].rjust(64, "0"), "blockNumber": hex(block),
            "blockHash": bhash or "0x" + "aa" * 32, "transactionHash": tx, "logIndex": hex(idx)}


class FakeRpc:
    def __init__(self):
        self.head, self.logs, self.hashes, self.fail = 100, [], {}, False

    def block_number(self):
        return self.head

    def get_logs(self, addresses, topics, a, b):
        self.last_call = (addresses, topics, a, b)
        return [l for l in self.logs if a <= int(l["blockNumber"], 16) <= b]

    def block_hash(self, n):
        if self.fail:
            raise RpcError("down")
        return self.hashes.get(n, "0x" + "aa" * 32)


class ChainTests(unittest.TestCase):
    def test_native_18_decimals(self):
        ev = chain.normalize_logs([make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 5, 0)], 5042002)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["amount"], ev[0]["source"], ev[0]["raw_decimals"]), ("1.000000", "native", 18))

    def test_erc20_two_logs_become_one_event(self):
        logs = [make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 5, 0),
                make_log(chain.ERC20_ADDR, SENDER, ME, 10**6, 5, 1)]
        ev = chain.normalize_logs(logs, 5042002)
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual((e["amount"], e["source"], e["raw_decimals"], e["amount_raw"]),
                         ("1.000000", "erc20", 6, "1000000"))

    def test_two_separate_payments_stay_separate(self):
        logs = [make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 5, 0, tx="0x" + "11" * 32),
                make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 6, 0, tx="0x" + "22" * 32)]
        self.assertEqual(len(chain.normalize_logs(logs, 1)), 2)

    def test_zero_value_unknown_emitter_and_nft_ignored(self):
        zero = make_log(chain.ERC20_ADDR, SENDER, ME, 0, 5, 0)
        other = make_log("0x" + "99" * 20, SENDER, ME, 5, 5, 1)
        nft = make_log(chain.ERC20_ADDR, SENDER, ME, 5, 5, 2)
        nft["topics"].append(pad("0x" + "01"))
        self.assertEqual(chain.normalize_logs([zero, other, nft], 1), [])

    def test_fractional_and_dust(self):
        ev = chain.normalize_logs([make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 1_234_567_890_000_000_000, 5, 0)], 1)
        self.assertEqual(ev[0]["amount"], "1.234568")

    def test_event_id_is_deterministic(self):
        l = make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 5, 0)
        self.assertEqual(chain.normalize_logs([l], 1)[0]["id"], chain.normalize_logs([l], 1)[0]["id"])


class SecurityTests(unittest.TestCase):
    def test_sign_verify_roundtrip_and_tamper(self):
        body = b'{"a":1}'
        h = security.sign("sec", body, ts=1000)
        self.assertTrue(security.verify("sec", body, h, now=1100))
        self.assertFalse(security.verify("sec", b'{"a":2}', h, now=1100))
        self.assertFalse(security.verify("other", body, h, now=1100))
        self.assertFalse(security.verify("sec", body, h, now=1000 + 301))   # stale
        self.assertFalse(security.verify("sec", body, "garbage", now=1100))

    def test_url_guard(self):
        bad = ["http://example.com/x", "https://127.0.0.1/x", "https://10.0.0.5/x", "https://localhost/x",
               "https://169.254.169.254/latest", "https://[::1]/x", "ftp://example.com", "https://u:p@93.184.216.34/x",
               "https://192.168.1.1/x", "https://100.64.0.1/x"]
        for u in bad:
            with self.assertRaises(ValueError, msg=u):
                security.validate_webhook_url(u)
        security.validate_webhook_url("https://93.184.216.34/hook")           # public literal ok
        security.validate_webhook_url("http://127.0.0.1:9/x", allow_private=True, allow_http=True)


class DbTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.ep = self.db.create_endpoint(ME, "https://93.184.216.34/hook")
        self.delays = (10, 30)

    def ev(self, tx="0x" + "11" * 32):
        return chain.normalize_logs([make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 5, 0, tx=tx)], 1)

    def test_store_is_idempotent_and_matches_address(self):
        self.assertEqual(self.db.store_events(self.ev(), "k", 5), 1)
        self.assertEqual(self.db.store_events(self.ev(), "k", 6), 0)       # same event again
        self.assertEqual(self.db.get_state("k"), "6")
        other = chain.normalize_logs([make_log(chain.NATIVE_LOG_ADDR, SENDER, "0x" + "77" * 20, 10**18, 5, 0)], 1)
        self.assertEqual(self.db.store_events(other, "k", 7), 0)           # nobody watches that address

    def test_retry_schedule_then_fail_then_replay(self):
        self.db.store_events(self.ev(), "k", 5)
        d = self.db.due_deliveries(time.time() + 1)[0]
        self.db.record_attempt(d["id"], False, 500, "HTTP 500", self.delays)
        self.assertEqual(self.db.due_deliveries(time.time() + 1), [])      # backoff in effect
        self.assertEqual(len(self.db.due_deliveries(time.time() + 11)), 1)
        self.db.record_attempt(d["id"], False, 500, "HTTP 500", self.delays)
        self.db.record_attempt(d["id"], False, 500, "HTTP 500", self.delays)
        eid = d["event_id"]
        self.assertEqual(self.db.get_event(eid)["deliveries"][0]["status"], "failed")
        self.assertEqual(self.db.replay(eid), 1)
        self.assertEqual(self.db.get_event(eid)["deliveries"][0]["status"], "pending")
        self.assertEqual(len(self.db.get_event(eid)["deliveries"][0]["attempt_log"]), 3)

    def test_delete_endpoint_cancels_pending(self):
        self.db.store_events(self.ev(), "k", 5)
        self.assertTrue(self.db.delete_endpoint(self.ep["id"]))
        self.assertEqual(self.db.due_deliveries(time.time() + 1), [])
        self.assertEqual(self.db.active_addresses(), set())


class HostingTests(unittest.TestCase):
    def test_ensure_endpoint_is_idempotent_and_keeps_given_secret(self):
        db = DB(":memory:")
        self.assertTrue(db.ensure_endpoint(ME, "https://93.184.216.34/h", "whsec_fixed"))
        self.assertFalse(db.ensure_endpoint(ME, "https://93.184.216.34/h", "whsec_fixed"))
        eps = db.active_addresses()
        self.assertEqual(eps, {ME})
        self.assertEqual(db.get_endpoint(db.list_endpoints()[0]["id"])["secret"], "whsec_fixed")

    def test_backfill_scans_recent_blocks_on_fresh_start(self):
        db, rpc = DB(":memory:"), FakeRpc()
        db.ensure_endpoint(ME, "https://93.184.216.34/h", "s")
        s = Settings(confirmations=2, backfill_blocks=50)
        rpc.head = 102                                  # head-2 = 100; backfill starts at 50
        rpc.logs = [make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 70, 0)]
        created, behind = scan_once(db, rpc, s)
        self.assertEqual((created, behind), (1, False))
        self.assertEqual(db.get_state(f"last_block:{s.chain_id}"), "100")

    def test_no_backfill_by_default(self):
        db, rpc = DB(":memory:"), FakeRpc()
        db.ensure_endpoint(ME, "https://93.184.216.34/h", "s")
        rpc.logs = [make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 70, 0)]
        self.assertEqual(scan_once(db, rpc, Settings(confirmations=2)), (0, False))


class BackoffTests(unittest.TestCase):
    def test_backoff(self):
        self.assertEqual(backoff_delay(0, 1.0), 1.0)
        self.assertEqual([backoff_delay(n, 1.0) for n in (1, 2, 3, 4)], [2.0, 4.0, 8.0, 16.0])
        self.assertEqual(backoff_delay(10, 1.0), 30.0)           # capped


class ShopTests(unittest.TestCase):
    def setUp(self):
        from arcchime import shop
        self.shop = shop
        self.db = DB(":memory:")
        self.s = Settings(demo_address=ME, demo_url="https://93.184.216.34/shop/webhook",
                          demo_secret="whsec_shop", chain_id=5042, confirmations=2)

    def event_body(self, amount="0.010137", to=ME, type_="payment.received", chain_id="eip155:5042", eid="evt_1"):
        return json.dumps({"id": eid, "type": type_, "to": to, "amount": amount, "chain": chain_id,
                           "tx_hash": "0x" + "ab" * 32}).encode()

    def test_unique_amounts_and_format(self):
        amounts = {self.db.create_order(10_000, 60, 500)["amount_micro"] for _ in range(50)}
        self.assertEqual(len(amounts), 50)
        self.assertTrue(all(10_001 <= a <= 10_999 for a in amounts))
        self.assertEqual(self.shop.fmt_amount(10_137), "0.010137")

    def test_pending_cap(self):
        for _ in range(3):
            self.assertIsNotNone(self.db.create_order(10_000, 60, 3))
        self.assertIsNone(self.db.create_order(10_000, 60, 3))

    def test_webhook_marks_matching_order_paid(self):
        o = self.db.create_order(10_000, 60, 500)
        body = self.event_body(amount=self.shop.fmt_amount(o["amount_micro"]))
        sig = security.sign("whsec_shop", body)
        self.assertEqual(self.shop.handle_webhook(self.db, self.s, body, sig), (200, "paid"))
        self.assertEqual(self.db.get_order(o["id"])["status"], "paid")
        self.assertEqual(self.shop.handle_webhook(self.db, self.s, body, sig), (200, "no matching order"))  # replay

    def test_webhook_rejections(self):
        o = self.db.create_order(10_000, 60, 500)
        amt = self.shop.fmt_amount(o["amount_micro"])
        body = self.event_body(amount=amt)
        self.assertEqual(self.shop.handle_webhook(self.db, self.s, body, "garbage")[0], 401)
        self.assertEqual(self.shop.handle_webhook(self.db, self.s, body, security.sign("wrong", body))[0], 401)
        for b in (self.event_body(amount=amt, to="0x" + "77" * 20), self.event_body(amount=amt, chain_id="eip155:1"),
                  self.event_body(amount=amt, type_="endpoint.test"), self.event_body(amount="0.0100005"),
                  self.event_body(amount="0.5")):
            code, msg = self.shop.handle_webhook(self.db, self.s, b, security.sign("whsec_shop", b))
            self.assertEqual(code, 200)
            self.assertNotEqual(msg, "paid")
        self.assertEqual(self.db.get_order(o["id"])["status"], "pending")

    def test_full_loop_payment_to_unlocked_order(self):
        """payment log -> scan -> ArcChime event -> signed delivery -> shop webhook -> order paid"""
        rpc = FakeRpc()
        self.db.ensure_endpoint(ME, self.s.demo_url, self.s.demo_secret)
        order = self.db.create_order(10_000, 60, 500)
        scan_once(self.db, rpc, self.s)                       # sets cursor
        rpc.logs = [make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, order["amount_micro"] * 10**12, 99, 0)]
        rpc.head = 102
        scan_once(self.db, rpc, self.s)

        class Resp:
            status_code = 200

        def post(url, data, headers, **kw):                   # stands in for the HTTP hop to /shop/webhook
            code, _ = self.shop.handle_webhook(self.db, self.s, data, headers["X-ArcChime-Signature"])
            r = Resp(); r.status_code = code
            return r

        self.s.check_reorg = False
        self.s.allow_private_urls = True
        deliver_due(self.db, rpc, self.s, post=post)
        self.assertEqual(self.db.get_order(order["id"])["status"], "paid")


class Receiver(BaseHTTPRequestHandler):
    calls, fail_first = [], 0

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Receiver.calls.append((body, dict(self.headers)))
        code = 500 if len(Receiver.calls) <= Receiver.fail_first else 200
        self.send_response(code)
        self.end_headers()

    def log_message(self, *a):
        pass


class PipelineTests(unittest.TestCase):
    def setUp(self):
        Receiver.calls, Receiver.fail_first = [], 0
        self.srv = HTTPServer(("127.0.0.1", 0), Receiver)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hook"
        self.s = Settings(allow_private_urls=True, allow_http_urls=True, confirmations=2,
                          retry_delays=(1, 1))
        self.db, self.rpc = DB(":memory:"), FakeRpc()
        self.ep = self.db.create_endpoint(ME, self.url)

    def tearDown(self):
        self.srv.shutdown()

    def test_scan_then_deliver_with_valid_signature_and_retry(self):
        Receiver.fail_first = 1
        scan_once(self.db, self.rpc, self.s)                 # first run: sets cursor to head-2 = 98
        self.rpc.logs = [make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 2 * 10**18, 99, 0),
                         make_log(chain.ERC20_ADDR, SENDER, ME, 2 * 10**6, 99, 1)]
        self.rpc.head = 102                                   # head-2 = 100
        created, behind = scan_once(self.db, self.rpc, self.s)
        self.assertEqual((created, behind), (1, False))
        addrs, topics, a, b = self.rpc.last_call
        self.assertEqual((a, b), (99, 100))
        self.assertEqual(topics[2], [pad(ME)])
        deliver_due(self.db, self.rpc, self.s)                # attempt 1 -> HTTP 500
        deliver_due(self.db, self.rpc, self.s, now=time.time() + 2)   # attempt 2 -> 200
        self.assertEqual(len(Receiver.calls), 2)
        body, headers = Receiver.calls[1]
        self.assertTrue(security.verify(self.ep["secret"], body, headers["X-ArcChime-Signature"]))
        payload = json.loads(body)
        self.assertEqual((payload["amount"], payload["source"], payload["type"]),
                         ("2.000000", "erc20", "payment.received"))
        self.assertEqual(headers["X-ArcChime-Delivery-Attempt"], "2")
        ev = self.db.get_event(payload["id"])
        self.assertEqual(ev["deliveries"][0]["status"], "delivered")
        self.assertEqual(len(ev["deliveries"][0]["attempt_log"]), 2)

    def test_reorged_block_is_not_delivered(self):
        scan_once(self.db, self.rpc, self.s)
        self.rpc.logs = [make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 99, 0)]
        self.rpc.head = 102
        scan_once(self.db, self.rpc, self.s)
        self.rpc.hashes[99] = "0x" + "bb" * 32               # chain now has a different block 99
        deliver_due(self.db, self.rpc, self.s)
        self.assertEqual(Receiver.calls, [])
        eid = self.db.list_events()[0]["id"]
        self.assertEqual(self.db.get_event(eid)["deliveries"][0]["status"], "reorged")

    def test_rpc_down_defers_instead_of_dropping(self):
        scan_once(self.db, self.rpc, self.s)
        self.rpc.logs = [make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 99, 0)]
        self.rpc.head = 102
        scan_once(self.db, self.rpc, self.s)
        self.rpc.fail = True
        deliver_due(self.db, self.rpc, self.s)
        self.assertEqual(Receiver.calls, [])
        self.rpc.fail = False
        deliver_due(self.db, self.rpc, self.s)
        self.assertEqual(len(Receiver.calls), 1)

    def test_private_url_blocked_by_default(self):
        strict = Settings(confirmations=2, retry_delays=(1, 1))
        self.db.store_events(chain.normalize_logs(
            [make_log(chain.NATIVE_LOG_ADDR, SENDER, ME, 10**18, 5, 0)], 1), "k", 5)
        deliver_due(self.db, self.rpc, strict)
        self.assertEqual(Receiver.calls, [])
        eid = self.db.list_events()[0]["id"]
        self.assertIn("blocked url", self.db.get_event(eid)["deliveries"][0]["attempt_log"][0]["error"])

    def test_operator_demo_url_may_be_loopback_but_user_urls_stay_blocked(self):
        strict = Settings(confirmations=2, retry_delays=(1, 1), demo_url=self.url)   # guard fully ON
        self.db.create_test_event(self.ep["id"])                                      # endpoint url == demo_url
        other = self.db.create_endpoint(ME, self.url + "?x=1")                         # user-style url, loopback
        self.db.create_test_event(other["id"])
        deliver_due(self.db, self.rpc, strict)
        self.assertEqual(len(Receiver.calls), 1)                                       # only the demo url got through
        blocked = [e for e in self.db.list_events() if e["type"] == "endpoint.test"]
        errs = [a["error"] for e in blocked for d in self.db.get_event(e["id"])["deliveries"]
                for a in d["attempt_log"] if a["error"]]
        self.assertTrue(any("blocked url" in x for x in errs))

    def test_test_event(self):
        eid = self.db.create_test_event(self.ep["id"])
        deliver_due(self.db, self.rpc, self.s)
        self.assertEqual(json.loads(Receiver.calls[0][0])["type"], "endpoint.test")
        self.assertEqual(self.db.get_event(eid)["deliveries"][0]["status"], "delivered")


if __name__ == "__main__":
    unittest.main(verbosity=2)
