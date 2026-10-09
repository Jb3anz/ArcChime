// Verify an ArcChime webhook in Node (no dependencies).
// `rawBody` must be the exact bytes received (a Buffer or string), NOT re-serialized JSON.
const crypto = require("crypto");

function verify(secret, rawBody, header, toleranceSec = 300) {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split(/=(.*)/s).slice(0, 2)));
  const ts = Number(parts.t), sig = parts.v1;
  if (!ts || !sig) return false;
  if (Math.abs(Date.now() / 1000 - ts) > toleranceSec) return false;
  const expected = crypto.createHmac("sha256", secret).update(`${ts}.`).update(rawBody).digest("hex");
  const a = Buffer.from(expected), b = Buffer.from(sig);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

module.exports = { verify };
