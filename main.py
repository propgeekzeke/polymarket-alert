import os, time, threading, requests
from flask import Flask, jsonify
app = Flask(__name__)

WALLETS = {
    "0x26437896ed9dfeb2f69765edcafe8fdceaab39ae": "#Latina",
    "0x709e8dcb133555794decc598e07f2c923b8366f5": "#0X70",
    "0xf0318c32136c2db7fec88b84869aee6a1106c80c": "#BTB",
    "0xfea31bc088000ff909be1dfd8d0e3f2c7ef2d227": "#0XFE",
}

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
DISCORD_USER_ID = "221025359884320770"
POLL_INTERVAL = 20
MIN_SIZE = 1000
seen_hashes = set()
position_totals = {}  # (wallet, eventSlug, outcome) -> running USDC total

_thread = None
_thread_lock = threading.Lock()
_seeded = False  # track whether we've seeded seen_hashes yet

def get_recent_trades(wallet):
    try:
        resp = requests.get("https://data-api.polymarket.com/activity",
            params={"user": wallet, "limit": 10}, timeout=10)
        if not resp.ok:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

def price_to_american(price):
    if price <= 0 or price >= 1: return "N/A"
    if price >= 0.5: return f"-{round(price / (1 - price) * 100)}"
    return f"+{round((1 - price) / price * 100)}"

def send_discord_alert(trade, label, wallet):
    side = trade.get("side", "")
    if side not in ("BUY", "SELL"): return
    event_slug = trade.get("eventSlug", "")
    outcome = trade.get("outcome", "")
    if not event_slug or not outcome: return
    fill_size = trade.get("usdcSize", 0)
    key = (wallet, event_slug, outcome)
    if side == "BUY":
        position_totals[key] = position_totals.get(key, 0) + fill_size
    else:
        position_totals[key] = position_totals.get(key, 0) - fill_size
    total = position_totals[key]
    if fill_size < MIN_SIZE: return
    emoji = "\U0001f7e2" if side == "BUY" else "\U0001f534"
    action = "NEW BET" if side == "BUY" else "CLOSED POSITION"
    content = (f"<@{DISCORD_USER_ID}>\n"
        f"{emoji} **{action} — {label} (Sharp)**\n"
        f"**{trade.get('title')}**\n"
        f"{side} **{outcome}** @ {round(trade.get('price',0)*100,1)}¢  ({price_to_american(trade.get('price',0))})\n"
        f"Fill: **${fill_size:,.0f}** | Total position: **${total:,.0f}**\n"
        f"<https://polymarket.com/event/{event_slug}>\n"
        f"<https://polymarket.com/@{wallet}>")
    try:
        requests.post(DISCORD_WEBHOOK, json={
            "content": content,
            "allowed_mentions": {"users": [DISCORD_USER_ID]}
        }, timeout=5)
    except Exception:
        pass

def monitor_loop():
    """Polling loop only — seeding is done synchronously in ensure_monitor()."""
    print(f"Monitor thread running in pid={os.getpid()}", flush=True)
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            for wallet, label in WALLETS.items():
                try:
                    for trade in reversed(get_recent_trades(wallet)):
                        tx = trade.get("transactionHash", "")
                        if tx and tx not in seen_hashes:
                            seen_hashes.add(tx)
                            if trade.get("type") == "TRADE":
                                send_discord_alert(trade, label, wallet)
                except Exception as e:
                    print(f"Error polling {label}: {e}", flush=True)
        except Exception as e:
            print(f"Monitor loop error: {e}", flush=True)
            time.sleep(5)

def ensure_monitor():
    """Seed seen_hashes synchronously (in calling thread), then start poll thread.

    Seeding MUST happen in the Flask request-handler thread, not in the daemon
    thread. After a gunicorn gthread fork, daemon-thread network calls hang
    indefinitely — but the request-handler thread works fine.
    """
    global _thread, _seeded
    with _thread_lock:
        # Seed synchronously in the calling thread if not done yet in this process
        if not _seeded:
            for wallet in WALLETS:
                for t in get_recent_trades(wallet):
                    h = t.get("transactionHash", "")
                    if h:
                        seen_hashes.add(h)
            _seeded = True
            print(f"Seeded {len(seen_hashes)} hashes in pid={os.getpid()}", flush=True)

        # Start/restart poll thread if needed
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=monitor_loop, daemon=True)
            _thread.start()

# Try at import time (works when module loads directly in the worker)
ensure_monitor()

@app.route("/")
@app.route("/health")
def health():
    # Re-seeds and restarts thread in worker after gunicorn fork
    # (gthread pre-loads in master; thread + seed state die on fork)
    ensure_monitor()
    return jsonify({
        "status": "running",
        "wallets": list(WALLETS.values()),
        "min_size": MIN_SIZE,
        "seen_trades": len(seen_hashes),
        "thread_alive": _thread.is_alive() if _thread else False,
        "pid": os.getpid()
    })
