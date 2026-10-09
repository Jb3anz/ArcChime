"""Turn raw Arc logs into one normalized payment event per USDC movement.

Observed on Arc testnet:
  * native USDC send   -> ONE Transfer log from 0xFFFF...FFFE (18 decimals)
  * ERC-20 transfer()  -> TWO logs: 0xFFFF...FFFE (18 dec) and 0x3600...0000 (6 dec)
We emit a single event per movement and always report amounts in plain USDC.
"""
import hashlib
from decimal import Decimal, localcontext

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ERC20_ADDR = "0x3600000000000000000000000000000000000000"
NATIVE_LOG_ADDR = "0xfffffffffffffffffffffffffffffffffffffffe"
DECIMALS = {ERC20_ADDR: 6, NATIVE_LOG_ADDR: 18}
SOURCE = {ERC20_ADDR: "erc20", NATIVE_LOG_ADDR: "native"}
QUANT = Decimal("0.000001")


def hx(v):
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    return v


def to_int(v):
    return int(v, 16) if isinstance(v, str) else int(v)


def pad_address(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def _addr(topic):
    return "0x" + hx(topic).lower()[-40:]


def _amount(value, decimals):
    with localcontext() as ctx:
        ctx.prec = 60
        return Decimal(value).scaleb(-decimals).quantize(QUANT)


def normalize_logs(logs, chain_id):
    ordered = sorted(logs, key=lambda l: (to_int(l["blockNumber"]), to_int(l["logIndex"])))
    events, seen = [], {}
    for log in ordered:
        emitter = hx(log["address"]).lower()
        if emitter not in DECIMALS:
            continue
        topics = [hx(t).lower() for t in log["topics"]]
        if len(topics) != 3 or topics[0] != TRANSFER_TOPIC:
            continue  # not a fungible Transfer (e.g. ERC-721 has 4 topics)
        data = hx(log["data"])
        if not data or len(data) <= 2:
            continue
        value = int(data, 16)
        if value == 0:
            continue  # zero-value transfers are not payments
        dec = DECIMALS[emitter]
        tx = hx(log["transactionHash"]).lower()
        log_index = to_int(log["logIndex"])
        ev = {
            "id": "evt_" + hashlib.sha256(f"{tx}:{log_index}".encode()).hexdigest()[:24],
            "type": "payment.received",
            "chain": f"eip155:{chain_id}",
            "token": "USDC",
            "source": SOURCE[emitter],
            "from": _addr(topics[1]),
            "to": _addr(topics[2]),
            "amount": format(_amount(value, dec), "f"),
            "amount_raw": str(value),
            "raw_decimals": dec,
            "tx_hash": tx,
            "block_number": to_int(log["blockNumber"]),
            "block_hash": hx(log["blockHash"]).lower(),
            "log_index": log_index,
        }
        key = (ev["tx_hash"], ev["from"], ev["to"], ev["amount"])
        if key in seen:
            prev = seen[key]
            if ev["source"] == "erc20" and prev["source"] != "erc20":
                prev["source"] = "erc20"          # report the token's own view
                prev["amount_raw"] = ev["amount_raw"]
                prev["raw_decimals"] = ev["raw_decimals"]
            continue
        seen[key] = ev
        events.append(ev)
    return events
