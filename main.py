import os
import time
import threading
import requests
from flask import Flask, jsonify

app = Flask(__name__)

WALLET = "0x26437896ed9dfeb2f69765edcafe8fdceaab39ae"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
POLL_INTERVAL = 20  # seconds

seen_hashes = set()


def get_recent_trades():
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": WALLET, "limit": 10},
            timeout=10,
        )
        return resp.json() if resp.ok else []
    except Exception as e:
        print(f"[ERROR] Fetch failed: {e}")
        return []


def price_to_american(price):
    if price <= 0 or price >= 1:
        return "N/A"
    if price >= 0.5:
        return f"-{round(price / (1 - price) * 100)}"
    return f"+{round((1 - price) / price * 100)}"


def send_discord_alert(trade):
    side = trade.get("side", "")
    if side not in ("BUY", "SELL"):
        return

    outcome = trade.get("outcome", "")
    title = trade.get("title", "Unknown Market")
    price = trade.get("price", 0)
    usdc_size = trade.get("usdcSize", 0)
    event_slug = trade.get("eventSlug", "")
    american = price_to_american(price)

    if side == "BUY":
        emoji = "🟢"
        action = "NEW BET"
    else:
        emoji = "🔴"
        action = "CLOSED POSITION"

    content = (
        f"{emoji} **{action} — #0X26 (Sharp)**\n"
        f"**{title}**\n"
        f"{side} **{outcome}** @ {round(price * 100, 1)}¢  ({american})\n"
        f"Size: **${usdc_size:,.0f}**\n"
        f"<https://polymarket.com/event/{event_slug}>"
    )

    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=5)
        print(f"[DISCORD] Alert sent ({r.status_code}): {title} {side}")
    except Exception as e:
        print(f"[ERROR] Discord post failed: {e}")


def monitor_loop():
    print("[MONITOR] Initializing — loading existing trades...")
    initial = get_recent_trades()
    for t in initial:
        seen_hashes.add(t.get("transactionHash", ""))
    print(f"[MONITOR] Ready. Watching for new trades (checking every {POLL_INTERVAL}s)...")

    while True:
        time.sleep(POLL_INTERVAL)
        try:
            trades = get_recent_trades()
            for trade in reversed(trades):  # process oldest first
                tx = trade.get("transactionHash", "")
                if tx and tx not in seen_hashes:
                    seen_hashes.add(tx)
                    if trade.get("type") == "TRADE":
                        print(f"[NEW] {trade.get('side')} {trade.get('outcome')} — {trade.get('title')}")
                        send_discord_alert(trade)
        except Exception as e:
            print(f"[ERROR] Monitor loop: {e}")


# Start background thread when module loads (gunicorn --workers 1 safe)
_thread = threading.Thread(target=monitor_loop, daemon=True)
_thread.start()


@app.route("/")
@app.route("/health")
def health():
    return jsonify({
        "status": "running",
        "wallet": WALLET,
        "seen_trades": len(seen_hashes),
        "poll_interval_seconds": POLL_INTERVAL,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
