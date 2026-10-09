"""Demo shop: pay a small amount of USDC to unlock a page.

It consumes ArcChime's own signed webhooks, so it demonstrates the whole loop:
payment on Arc -> ArcChime event -> signed webhook -> order marked paid -> page unlocks.
Orders are matched by a unique amount (0.010001 .. 0.010999 USDC).
"""
import json
from decimal import Decimal, InvalidOperation

from . import security

PRICE_MICRO = 10_000          # 0.010000 USDC base price
ORDER_TTL = 1800              # seconds
MAX_PENDING = 500


def fmt_amount(micro):
    return f"{micro // 1_000_000}.{micro % 1_000_000:06d}"


def handle_webhook(db, settings, body, signature_header):
    """Returns (http_status, message). Always 200 for validly signed events so ArcChime won't retry."""
    if not (settings.demo_secret and settings.demo_address):
        return 503, "shop not configured"
    if not security.verify(settings.demo_secret, body, signature_header or ""):
        return 401, "bad signature"
    try:
        ev = json.loads(body)
        if ev.get("type") != "payment.received":
            return 200, "ignored"
        if str(ev.get("to", "")).lower() != settings.demo_address.lower():
            return 200, "ignored"
        if ev.get("chain") != f"eip155:{settings.chain_id}":
            return 200, "ignored"
        micro = Decimal(ev["amount"]) * 1_000_000
        if micro != micro.to_integral_value():
            return 200, "ignored"
        order_id = db.mark_order_paid(int(micro), ev.get("id"), ev.get("tx_hash"))
    except (ValueError, KeyError, InvalidOperation, TypeError):
        return 200, "ignored"
    return 200, "paid" if order_id else "no matching order"


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ArcChime demo shop</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#14171c;--muted:#5c6673;--line:#e1e5ea;--accent:#2a5bd7;--ok:#13804a;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#171b22;--text:#e8ecf1;--muted:#97a3b2;--line:#2a313b;--accent:#6b8cff;--ok:#3ccf83;--chip:#202632}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:1rem 1rem 3rem}
.wrap{max-width:34rem;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:center;margin:.5rem 0 1.25rem}
.brand{font-weight:700}.brand span{color:var(--muted);font-weight:500}
.pill{font-size:.8rem;padding:.2rem .65rem;border-radius:999px;background:var(--chip);color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:1.25rem;margin-bottom:1rem}
h1{font-size:1.65rem;line-height:1.2;margin:.1rem 0 .5rem}h2{font-size:1.2rem;margin:.1rem 0 .5rem}
p{margin:.4rem 0}.muted{color:var(--muted)}.small{font-size:.88rem}
ol.steps{list-style:none;display:flex;gap:.4rem;padding:0;margin:0 0 1rem;font-size:.78rem;color:var(--muted)}
ol.steps li{flex:1;border-top:3px solid var(--line);padding-top:.35rem}
ol.steps li.active{border-color:var(--accent);color:var(--text);font-weight:600}
ol.steps li.done{border-color:var(--ok);color:var(--ok)}
.row{display:flex;gap:.6rem;align-items:center;justify-content:space-between;background:var(--chip);border-radius:10px;padding:.6rem .8rem;margin:.5rem 0}
.row code{font:600 .95rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all}
.lbl{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-top:.8rem}
button{font:inherit;cursor:pointer;border-radius:10px}
.primary{width:100%;padding:.85rem 1rem;border:0;background:var(--accent);color:#fff;font-weight:600}
.ghost{padding:.3rem .7rem;border:1px solid var(--line);background:var(--card);color:var(--text);font-size:.85rem;flex:none}
.wait{display:flex;gap:.6rem;align-items:center;margin-top:1rem;color:var(--muted)}
.spin{width:1rem;height:1rem;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:s 1s linear infinite;flex:none}
@keyframes s{to{transform:rotate(360deg)}}
.ok{color:var(--ok)}
dl{display:grid;grid-template-columns:auto 1fr;gap:.35rem .9rem;margin:.6rem 0 0;font-size:.88rem}
dt{color:var(--muted)}dd{margin:0;word-break:break-all;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
footer{text-align:center;margin-top:1.5rem}footer a{color:var(--muted)}
[hidden]{display:none!important}
</style></head><body><div class="wrap">
<header><div class="brand">ArcChime <span>demo shop</span></div><div class="pill">__NETWORK__</div></header>

<div class="card">
  <h1>Unlock the secret page for 0.01 USDC</h1>
  <p class="muted">A live test of ArcChime. You pay on Arc, ArcChime spots the payment and sends a signed webhook,
  and this page unlocks on its own. No account, no wallet connection.</p>
  <ol class="steps" id="steps"><li>Create order</li><li>Send USDC</li><li>Signed webhook</li><li>Unlocked</li></ol>

  <div id="start"><button class="primary" id="go">Create my order</button></div>

  <div id="order" hidden>
    <div class="lbl">Send exactly</div>
    <div class="row"><code><span id="amt"></span> USDC</code><button class="ghost" id="cpa">Copy</button></div>
    <div class="lbl">To this address on __NETWORK__ (chain ID __CHAIN_ID__)</div>
    <div class="row"><code id="addr">__ADDRESS__</code><button class="ghost" id="cpd">Copy</button></div>
    <p class="small muted">A native USDC send or an ERC-20 transfer both work. The amount is unique to your order, so
    send it exactly. Sending a different amount will not unlock anything.</p>
    <div class="wait" aria-live="polite"><div class="spin"></div><span id="left">Waiting for your payment&hellip;</span></div>
  </div>

  <div id="done" hidden>
    <h2 class="ok">&#10003; Unlocked</h2>
    <p id="msg"></p>
    <div class="lbl">The signed event ArcChime delivered</div>
    <dl id="ev"></dl>
  </div>
  <p id="err" class="small" style="color:#cf222e"></p>
</div>
<footer class="small"><a href="/">About</a> &middot; <a href="/health">Status</a> &middot; <a href="/docs">API</a></footer>
</div>
<script>
const $=id=>document.getElementById(id);
let oid=null,timer=null;
function step(n){[...$('steps').children].forEach((el,i)=>{el.className=i<n?'done':(i===n?'active':'')})}
async function copy(text,btn){try{await navigator.clipboard.writeText(text);const t=btn.textContent;btn.textContent='Copied';setTimeout(()=>{btn.textContent=t},1200)}catch(e){}}
step(0);
$('cpd').onclick=()=>copy($('addr').textContent,$('cpd'));
$('cpa').onclick=()=>copy($('amt').textContent,$('cpa'));
$('go').onclick=async()=>{
  $('err').textContent='';$('go').disabled=true;
  try{
    const r=await fetch('/shop/orders',{method:'POST'});
    if(!r.ok){$('err').textContent='Could not create an order right now. Please try again shortly.';$('go').disabled=false;return}
    const o=await r.json();oid=o.id;$('amt').textContent=o.amount;
    $('start').hidden=true;$('order').hidden=false;step(1);
    timer=setInterval(poll,2000);poll();
  }catch(e){$('err').textContent='Network error. Please try again.';$('go').disabled=false}
};
function fill(ev){
  const dl=$('ev');dl.textContent='';
  const rows=[['event id',ev.id],['type',ev.type],['amount',ev.amount+' USDC'],['how it moved',ev.source],
    ['block',String(ev.block_number)],['tx hash',ev.tx_hash],['from',ev.from],['chain',ev.chain]];
  rows.forEach(([k,v])=>{const dt=document.createElement('dt');dt.textContent=k;const dd=document.createElement('dd');dd.textContent=v||'';dl.append(dt,dd)});
}
async function poll(){
  const r=await fetch('/shop/orders/'+oid);if(!r.ok)return;const o=await r.json();
  if(o.status==='paid'){
    clearInterval(timer);step(4);$('order').hidden=true;$('done').hidden=false;$('msg').textContent=o.message;
    if(o.event)fill(o.event);
  }else if(o.status==='expired'){
    clearInterval(timer);$('order').hidden=true;$('start').hidden=false;$('go').disabled=false;step(0);
    $('err').textContent='That order expired. Create a new one.';
  }else{
    $('left').textContent='Waiting for your payment\u2026 '+Math.max(0,Math.round(o.expires_in/60))+' min left on this order';
  }
}
</script></body></html>"""
