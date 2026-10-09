import json
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints(
  id TEXT PRIMARY KEY, address TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ep_addr ON endpoints(address, active);
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, type TEXT NOT NULL, to_address TEXT, payload TEXT NOT NULL,
  block_number INTEGER, block_hash TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ev_to ON events(to_address, created_at);
CREATE TABLE IF NOT EXISTS deliveries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, endpoint_id TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
  last_status INTEGER, last_error TEXT, delivered_at REAL,
  UNIQUE(event_id, endpoint_id));
CREATE INDEX IF NOT EXISTS ix_dl_due ON deliveries(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id INTEGER NOT NULL, at REAL NOT NULL,
  status_code INTEGER, error TEXT, ok INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
  id TEXT PRIMARY KEY, amount_micro INTEGER NOT NULL, status TEXT NOT NULL,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, event_id TEXT, tx_hash TEXT, paid_at REAL);
CREATE INDEX IF NOT EXISTS ix_orders_pending ON orders(status, amount_micro);
"""
# delivery.status: pending | delivered | failed | reorged | cancelled


def canonical(obj):
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


class DB:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    # ---- endpoints -------------------------------------------------------
    def create_endpoint(self, address, url, secret=None):
        ep = {"id": "ep_" + secrets.token_hex(8), "address": address.lower(), "url": url,
              "secret": secret or "whsec_" + secrets.token_urlsafe(32), "created_at": time.time()}
        with self.lock:
            self.conn.execute(
                "INSERT INTO endpoints(id,address,url,secret,active,created_at) VALUES(?,?,?,?,1,?)",
                (ep["id"], ep["address"], ep["url"], ep["secret"], ep["created_at"]))
        return ep

    def ensure_endpoint(self, address, url, secret):
        """Idempotent registration (used to re-create a demo endpoint after a restart)."""
        with self.lock:
            r = self.conn.execute(
                "SELECT id FROM endpoints WHERE active=1 AND address=? AND url=?",
                (address.lower(), url)).fetchone()
            if r:
                return False
        self.create_endpoint(address, url, secret)
        return True

    def list_endpoints(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id,address,url,created_at FROM endpoints WHERE active=1 ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def get_endpoint(self, ep_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM endpoints WHERE id=? AND active=1", (ep_id,)).fetchone()
        return dict(r) if r else None

    def delete_endpoint(self, ep_id):
        with self.lock:
            cur = self.conn.execute("UPDATE endpoints SET active=0 WHERE id=? AND active=1", (ep_id,))
            self.conn.execute(
                "UPDATE deliveries SET status='cancelled' WHERE endpoint_id=? AND status='pending'", (ep_id,))
            return cur.rowcount > 0

    def active_addresses(self):
        with self.lock:
            rows = self.conn.execute("SELECT DISTINCT address FROM endpoints WHERE active=1").fetchall()
        return {r["address"] for r in rows}

    # ---- state -----------------------------------------------------------
    def get_state(self, key):
        with self.lock:
            r = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_state(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", (key, str(value)))

    # ---- events / deliveries --------------------------------------------
    def store_events(self, events, state_key, last_block):
        """Atomically store events, create deliveries, and advance the scan cursor."""
        now = time.time()
        created = 0
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for ev in events:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO events(id,type,to_address,payload,block_number,block_hash,created_at)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (ev["id"], ev["type"], ev["to"], canonical(ev), ev["block_number"], ev["block_hash"], now))
                    eps = self.conn.execute(
                        "SELECT id FROM endpoints WHERE active=1 AND address=?", (ev["to"],)).fetchall()
                    for ep in eps:
                        cur = self.conn.execute(
                            "INSERT OR IGNORE INTO deliveries(event_id,endpoint_id,status,attempts,next_attempt_at)"
                            " VALUES(?,?,'pending',0,?)", (ev["id"], ep["id"], now))
                        created += cur.rowcount
                self.conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)",
                                  (state_key, str(last_block)))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return created

    def create_test_event(self, ep_id):
        ep = self.get_endpoint(ep_id)
        if not ep:
            return None
        now = time.time()
        ev = {"id": "evt_test_" + secrets.token_hex(8), "type": "endpoint.test",
              "to": ep["address"], "message": "ArcChime test event", "block_number": None,
              "block_hash": None}
        with self.lock:
            self.conn.execute(
                "INSERT INTO events(id,type,to_address,payload,block_number,block_hash,created_at)"
                " VALUES(?,?,?,?,NULL,NULL,?)", (ev["id"], ev["type"], ep["address"], canonical(ev), now))
            self.conn.execute(
                "INSERT INTO deliveries(event_id,endpoint_id,status,attempts,next_attempt_at)"
                " VALUES(?,?,'pending',0,?)", (ev["id"], ep_id, now))
        return ev["id"]

    def due_deliveries(self, now, limit=50):
        with self.lock:
            rows = self.conn.execute(
                "SELECT d.id, d.attempts, d.event_id, e.payload, e.block_number, e.block_hash,"
                " ep.url, ep.secret FROM deliveries d"
                " JOIN events e ON e.id=d.event_id JOIN endpoints ep ON ep.id=d.endpoint_id"
                " WHERE d.status='pending' AND d.next_attempt_at<=? AND ep.active=1"
                " ORDER BY d.next_attempt_at LIMIT ?", (now, limit)).fetchall()
        return [dict(r) for r in rows]

    def record_attempt(self, delivery_id, ok, status_code, error, retry_delays):
        now = time.time()
        with self.lock:
            row = self.conn.execute("SELECT attempts FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            n = row["attempts"] + 1
            self.conn.execute("INSERT INTO attempts(delivery_id,at,status_code,error,ok) VALUES(?,?,?,?,?)",
                              (delivery_id, now, status_code, error, 1 if ok else 0))
            if ok:
                self.conn.execute(
                    "UPDATE deliveries SET status='delivered', attempts=?, last_status=?, last_error=NULL,"
                    " delivered_at=? WHERE id=?", (n, status_code, now, delivery_id))
            elif n > len(retry_delays):
                self.conn.execute(
                    "UPDATE deliveries SET status='failed', attempts=?, last_status=?, last_error=? WHERE id=?",
                    (n, status_code, error, delivery_id))
            else:
                self.conn.execute(
                    "UPDATE deliveries SET status='pending', attempts=?, last_status=?, last_error=?,"
                    " next_attempt_at=? WHERE id=?",
                    (n, status_code, error, now + retry_delays[n - 1], delivery_id))

    def mark_reorged(self, delivery_id):
        with self.lock:
            self.conn.execute(
                "UPDATE deliveries SET status='reorged', last_error='block no longer canonical' WHERE id=?",
                (delivery_id,))

    def replay(self, event_id, endpoint_id=None):
        sql = ("UPDATE deliveries SET status='pending', attempts=0, next_attempt_at=?, last_error=NULL"
               " WHERE event_id=? AND status IN ('delivered','failed','pending')"
               " AND endpoint_id IN (SELECT id FROM endpoints WHERE active=1)")
        args = [time.time(), event_id]
        if endpoint_id:
            sql += " AND endpoint_id=?"
            args.append(endpoint_id)
        with self.lock:
            return self.conn.execute(sql, args).rowcount

    def list_events(self, address=None, limit=50):
        sql, args = "SELECT id,type,to_address,payload,created_at FROM events", []
        if address:
            sql += " WHERE to_address=?"
            args.append(address.lower())
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [{"id": r["id"], "type": r["type"], "created_at": r["created_at"],
                 "data": json.loads(r["payload"])} for r in rows]

    def get_event(self, event_id):
        with self.lock:
            e = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not e:
                return None
            dls = self.conn.execute(
                "SELECT id,endpoint_id,status,attempts,last_status,last_error,delivered_at,next_attempt_at"
                " FROM deliveries WHERE event_id=?", (event_id,)).fetchall()
            out = []
            for d in dls:
                atts = self.conn.execute(
                    "SELECT at,status_code,error,ok FROM attempts WHERE delivery_id=? ORDER BY id",
                    (d["id"],)).fetchall()
                item = dict(d)
                item["attempt_log"] = [dict(a) for a in atts]
                out.append(item)
        return {"id": e["id"], "type": e["type"], "created_at": e["created_at"],
                "data": json.loads(e["payload"]), "deliveries": out}

    # ---- demo shop orders -----------------------------------------------
    def create_order(self, base_micro, ttl, max_pending):
        """Each pending order gets a unique amount (base + 1..999 micro-USDC) so a payment identifies it."""
        now = time.time()
        with self.lock:
            self.conn.execute("UPDATE orders SET status='expired' WHERE status='pending' AND expires_at<?", (now,))
            used = {r["amount_micro"] for r in self.conn.execute(
                "SELECT amount_micro FROM orders WHERE status='pending'").fetchall()}
            if len(used) >= max_pending:
                return None
            free = [base_micro + i for i in range(1, 1000) if base_micro + i not in used]
            if not free:
                return None
            order = {"id": "ord_" + secrets.token_urlsafe(9), "amount_micro": secrets.choice(free),
                     "status": "pending", "created_at": now, "expires_at": now + ttl}
            self.conn.execute(
                "INSERT INTO orders(id,amount_micro,status,created_at,expires_at) VALUES(?,?,?,?,?)",
                (order["id"], order["amount_micro"], "pending", now, now + ttl))
        return order

    def get_order(self, order_id):
        now = time.time()
        with self.lock:
            self.conn.execute("UPDATE orders SET status='expired' WHERE status='pending' AND expires_at<?", (now,))
            r = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return dict(r) if r else None

    def mark_order_paid(self, amount_micro, event_id, tx_hash):
        now = time.time()
        with self.lock:
            r = self.conn.execute(
                "SELECT id FROM orders WHERE status='pending' AND amount_micro=? AND expires_at>=?"
                " ORDER BY created_at LIMIT 1", (amount_micro, now)).fetchone()
            if not r:
                return None
            self.conn.execute("UPDATE orders SET status='paid', event_id=?, tx_hash=?, paid_at=? WHERE id=?",
                              (event_id, tx_hash, now, r["id"]))
        return r["id"]

    def stats(self):
        with self.lock:
            r = self.conn.execute("SELECT COUNT(*) c FROM deliveries WHERE status='pending'").fetchone()
        return {"pending_deliveries": r["c"]}
