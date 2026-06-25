import os, time, threading, requests
from flask import Flask, jsonify
app = Flask(__name__)
WALLET = "0x26437896ed9dfeb2f69765edcafe8fdceaab39ae"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
DISCORD_USER_ID = "221025359884320770"
POLL_INTERVAL = 20
seen_hashes = set()

def get_recent_trades():
    resp = requests.get("https://data-api.polymarket.com/activity",
        params={"user": WALLET, "limit": 10}, timeout=10)
    return resp.json() if resp.ok else []

def price_to_american(price):
    if price <= 0 or price >= 1: return "N/A"
    if price >= 0.5: return f"-{round(price / (1 - price) * 100)}"
    return f"+{round((1 - price) / price * 100)}"

def send_discord_alert(trade):
    side = trade.get("side", "")
    if side not in ("BUY", "SELL"): return
    emoji = "U0001f7e2" if side == "BUY" else "U0001f534"
    action = "NEW BET" if side == "BUY" else "CLOSED POSITION"
    content = (f"<@{DISCORD_USER_ID}>
"
        f"{emoji} **{action} — #0X26 (Sharp)**
"
        f"**{trade.get('title')}**
"
        f"{side} **{trade.get('outcome')}** @ {round(trade.get('price',0)*100,1)}¢  ({price_to_american(trade.get('price',0))})
"
        f"Size: **${trade.get('usdcSize',0):,.0f}**
"
        f"<https://polymarket.com/event/{trade.get('eventSlug','')}>")
    requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=5)

def monitor_loop():
    for t in get_recent_trades(): seen_hashes.add(t.get("transactionHash",""))
    while True:
        time.sleep(POLL_INTERVAL)
        for trade in reversed(get_recent_trades()):
            tx = trade.get("transactionHash","")
            if tx and tx not in seen_hashes:
                seen_hashes.add(tx)
                if trade.get("type") == "TRADE": send_discord_alert(trade)

_thread = threading.Thread(target=monitor_loop, daemon=True)
_thread.start()

@app.route("/")
@app.route("/health")
def health(): return jsonify({"status":"running","wallet":WALLET,"seen_trades":len(seen_hashes)})
