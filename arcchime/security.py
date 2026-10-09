import hashlib
import hmac
import ipaddress
import socket
import time
from urllib.parse import urlparse


def sign(secret, body, ts=None):
    ts = int(ts if ts is not None else time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret, body, header, tolerance=300, now=None):
    """Receiver-side check: valid signature AND timestamp within tolerance."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
        ts, sig = int(parts["t"]), parts["v1"]
    except Exception:
        return False
    if abs((now if now is not None else time.time()) - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def validate_webhook_url(url, allow_private=False, allow_http=False):
    """Reject URLs that would make the service call internal addresses (SSRF).

    Limitation: DNS is resolved here and again by the HTTP client, so a
    malicious DNS server could still rebind between the two. Redirects are
    disabled at delivery time to close the easier bypass.
    """
    p = urlparse(url)
    if p.scheme not in ("https", "http"):
        raise ValueError("URL must start with https://")
    if p.scheme == "http" and not allow_http:
        raise ValueError("plain http is not allowed; use https")
    if p.username or p.password:
        raise ValueError("credentials in URL are not allowed")
    host = p.hostname
    if not host:
        raise ValueError("URL has no host")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise ValueError("invalid port")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"cannot resolve host {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise ValueError(f"{host} resolves to a non-public address")
