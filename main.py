import os, time, threading, requests
from flask import Flask, jsonify
app = Flask(__name__)

WALLETS = {
    "0x26437896ed9dfeb2f69765edcafe8fdceaab39ae": "#0X26",
    "0x0346afae2603313d2bbee96b628536c8cbe352a5": "#0X03",
    "0x709e8dcb133555794decc598e07f2c923b8366f5": "#0X70",
}

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
DISCORD_USER_ID = "221025359884320770"
POLL_INTERVAL = 20
MIN_SIZE = 1000
seen_hashes = set()

def get_recent_trades(wallet):
    resp = requests.get("https://data-api.polymarket.com/activity",
        params={"user": wallet, "limit": 10}, timeout=10)
    return resp.json() if resp.ok else []

def price_to_american(price):
    if price <= 0 or price >= 1: return "N/A"
    if price >= 0.5: return f"-{round(price / (1 - price) * 100)}"
    return f"+{round((1 - price) / price * 100)}"

def send_discord_alert(trade, label):
    side = trade.get("side", "")
    if side not in ("BUY", "SELL"): return
    event_slug = trade.get("eventSlug", "")
    outcome = trade.get("outcome", "")
    if not event_slug or not outcome: return
    if trade.get('usdcSize', 0) < MIN_SIZE: return
    emoji = "U0001f7e2" if side == "BUY" else "U0001f534"
    action = "NEW BET" if side == "BUY" else "CLOSED POSITION"
    content = (f"<@{DISCORD_USER_ID}>\n"
        f"{emoji} **{action} — {label} (Sharp)**\n"
        f"**{trade.get('title')}**\n"
        f"{side} **{outcome}** @ {round(trade.get('price',0)*100,1)}\u00a2  ({price_to_american(trade.get('price',0))})\n"
        f"Size: **${trade.get('usdcSize',0):,.0f}**\n"
        f"<https://polymarket.com/event/{event_slug}>")
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
                    if trade.get("type") == "TRADE": send_discord_alert(trade, label)

_thread = threading.Thread(target=monitor_loop, daemon=True)
_thread.start()

@app.route("/")
@app.route("/health")
def health(): return jsonify({"status":"running","wallets":list(WALLETS.values()),"min_size":MIN_SIZE,"seen_trades":len(seen_hashes)})
