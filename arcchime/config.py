import os
from dataclasses import dataclass


def _bool(name, default="0"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


@dataclass
class Settings:
    rpc_url: str = "https://rpc.testnet.arc.network"
    chain_id: int = 5042002          # 5042 = Arc mainnet, 5042002 = Arc testnet
    confirmations: int = 2           # blocks to wait before reporting a payment
    poll_seconds: float = 1.0
    max_range: int = 500             # max blocks per eth_getLogs call
    db_path: str = "arcchime.db"
    admin_token: str = ""            # if set, API calls need "Authorization: Bearer <token>"
    allow_private_urls: bool = False  # set 1 only for local testing (disables SSRF guard)
    allow_http_urls: bool = False     # set 1 only for local testing
    check_reorg: bool = True
    retry_delays: tuple = (10, 30, 120, 600, 3600)  # seconds after attempt 1..5
    run_workers: bool = True
    backfill_blocks: int = 0         # on a fresh start, also scan this many recent blocks
    demo_address: str = ""           # optional: auto-register this endpoint at startup
    demo_url: str = ""
    demo_secret: str = ""

    @classmethod
    def from_env(cls):
        e = os.environ
        return cls(
            rpc_url=e.get("ARC_RPC", cls.rpc_url),
            chain_id=int(e.get("CHAIN_ID", cls.chain_id)),
            confirmations=int(e.get("CONFIRMATIONS", cls.confirmations)),
            poll_seconds=float(e.get("POLL_SECONDS", cls.poll_seconds)),
            max_range=int(e.get("MAX_RANGE", cls.max_range)),
            db_path=e.get("DB_PATH", cls.db_path),
            admin_token=e.get("ADMIN_TOKEN", ""),
            allow_private_urls=_bool("ALLOW_PRIVATE_URLS"),
            allow_http_urls=_bool("ALLOW_HTTP_URLS"),
            check_reorg=_bool("CHECK_REORG", "1"),
            run_workers=_bool("RUN_WORKERS", "1"),
            backfill_blocks=int(e.get("BACKFILL_BLOCKS", 0)),
            demo_address=e.get("DEMO_ADDRESS", ""),
            demo_url=e.get("DEMO_URL", ""),
            demo_secret=e.get("DEMO_SECRET", ""),
        )
