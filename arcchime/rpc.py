"""Minimal JSON-RPC client (no web3 dependency)."""
import requests


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self, url, timeout=20, session=None):
        self.url = url
        self.timeout = timeout
        self.session = session or requests.Session()

    def call(self, method, params):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            r = self.session.post(self.url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # network, HTTP, or JSON problems
            raise RpcError(f"{type(e).__name__}: {str(e)[:200]}")
        if "error" in data:
            raise RpcError(str(data["error"])[:300])
        return data["result"]

    def chain_id(self):
        return int(self.call("eth_chainId", []), 16)

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def get_logs(self, addresses, topics, from_block, to_block):
        return self.call("eth_getLogs", [{
            "address": addresses,
            "topics": topics,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }])

    def block_hash(self, number):
        blk = self.call("eth_getBlockByNumber", [hex(number), False])
        return blk["hash"].lower() if blk else None
