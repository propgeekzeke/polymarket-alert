import os, time, threading, requests
from flask import Flask, jsonify
app = Flask(__name__)

WALLETS = {
    "0x26437896ed9dfeb2f69765edcafe8fdceaab39ae": "#Latina",
    "0x709e8dcb133555794decc598e07f2c923b8366f5": "#0X70",
    "0xf0318c32136c2db7fec88b84869aee6a1106c80c": "#BTB",
}

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
DISCORD_USER_ID = "221025359884320770"
POLL_INTERVAL = 20
MIN_SIZE = 1000
seen_hashes = set()
position_totals = {}  # (wallet, eventSlug, outcome) -> running USDC total

def get_recent_trades(wallet):
    resp = requests.get("https://data-api.polymarket.com/activity",
        params={"user": wallet, "limit": 10}, timeout=10)
    return resp.json() if resp.ok else []

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
        f"{side} **{outcome}** @ {round(trade.get('price',0)*100,1)}\u00a2  ({price_to_american(trade.get('price',0))})\n"
        f"Fill: **${fill_size:,.0f}** | Total position: **${total:,.0f}**\n"
        f"<https://polymarket.com/event/{event_slug}>\n"
        f"<https://polymarket.com/@{wallet}>")
    requests.post(DISCORD_WEBHOOK, json={
        "content": content,
        "allowed_mentions": {"users": [DISCORD_USER_ID]}
    }, timeout=5)

def monitor_loop():
    for wallet in WALLETS:
        for t in get_recent_trades(wallet): seen_hashes.add(t.get("transactionHash",""))
    while True:
        time.sleep(POLL_INTERVAL)
        for wallet, label in WALLETS.items():
            for trade in reversed(get_recent_trades(wallet)):
                tx = trade.get("transactionHash","")
                if tx and tx not in seen_hashes:
                    seen_hashes.add(tx)
                    if trade.get("type") == "TRADE": send_discord_alert(trade, label, wallet)

_thread = threading.Thread(target=monitor_loop, daemon=True)
_thread.start()

@app.route("/")
@app.route("/health")
def health(): return jsonify({"status":"running","wallets":list(WALLETS.values()),"min_size":MIN_SIZE,"seen_trades":len(seen_hashes)})
