import os, time, threading, requests, statistics
from datetime import datetime, timezone
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
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
POLL_INTERVAL = 20
MIN_SIZE = 1000

# Per-wallet minimum fill size overrides
WALLET_MIN_SIZE = {
    "0xf0318c32136c2db7fec88b84869aee6a1106c80c": 10000,  # #BTB
}

seen_hashes = set()
position_totals = {}   # (wallet, eventSlug, outcome) -> running USDC total
wallet_profiles = {}   # wallet -> profile dict (cached)

_thread = None
_thread_lock = threading.Lock()
_seeded = False

# Keyword -> The Odds API sport key
SPORT_MAP = [
    (["mlb-", "-mlb-", "/mlb"],            "baseball_mlb"),
    (["nba-", "-nba-", "/nba"],            "basketball_nba"),
    (["nfl-", "-nfl-", "/nfl"],            "americanfootball_nfl"),
    (["nhl-", "-nhl-", "/nhl"],            "icehockey_nhl"),
    (["fifwc", "world-cup", "2026-fifa"],  "soccer_fifa_world_cup"),
    (["ufc-", "-ufc-"],                    "mma_mixed_martial_arts"),
]

# --- Polymarket API ----------------------------------------------------------

def get_recent_trades(wallet):
    try:
        r = requests.get("https://data-api.polymarket.com/activity",
                         params={"user": wallet, "limit": 10}, timeout=10)
        if not r.ok:
            return []
        d = r.json()
        return d if isinstance(d, list) else []
    except Exception:
        return []

# --- Wallet profiler ---------------------------------------------------------

def fetch_wallet_profile(wallet):
    """Paginate through ALL activity records for a wallet and compute summary stats.

    P&L = REDEEMs (winning payouts) + SELL proceeds - BUY costs.
    avg_stake / max_stake use NET position size per (eventSlug, outcome),
    not individual fill sizes.
    """
    try:
        raw = []
        offset = 0
        while True:
            r = requests.get("https://data-api.polymarket.com/activity",
                             params={"user": wallet, "limit": 500, "offset": offset},
                             timeout=15)
            if not r.ok:
                break
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            raw.extend(batch)
            if len(batch) < 500:
                break
            offset += 500
        if not raw:
            return {}

        months = set()
        month_pnl = {}   # "YYYY-MM" -> float net P&L
        cats = {}
        net_pos = {}     # (slug, outcome) -> net USDC (BUY positive, SELL negative)
        total_cost = 0.0
        total_proceeds = 0.0

        for t in raw:
            usdc = t.get("usdcSize", 0)
            if not usdc or usdc <= 0:
                continue
            ts   = t.get("timestamp", 0)
            mo   = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m") if ts else None
            slug = (t.get("eventSlug") or "").lower()
            typ  = t.get("type", "")
            side = t.get("side", "")

            if mo:
                months.add(mo)

            if typ == "TRADE" and side == "BUY":
                total_cost += usdc
                if mo:
                    month_pnl[mo] = month_pnl.get(mo, 0) - usdc
                cat = _slug_to_cat(slug)
                cats[cat] = cats.get(cat, 0) + 1
                key = (slug, (t.get("outcome") or "").lower())
                net_pos[key] = net_pos.get(key, 0) + usdc

            elif typ == "TRADE" and side == "SELL":
                total_proceeds += usdc
                if mo:
                    month_pnl[mo] = month_pnl.get(mo, 0) + usdc
                key = (slug, (t.get("outcome") or "").lower())
                net_pos[key] = net_pos.get(key, 0) - usdc

            elif typ == "REDEEM":
                # Winning payout - counts as proceeds
                total_proceeds += usdc
                if mo:
                    month_pnl[mo] = month_pnl.get(mo, 0) + usdc

        all_trades = [t for t in raw if t.get("type") == "TRADE"
                      and t.get("side") in ("BUY", "SELL") and t.get("usdcSize", 0) > 0]
        if not all_trades:
            return {}

        # Net position sizes (positive = open or partially closed)
        position_sizes = [v for v in net_pos.values() if v > 0]
        if not position_sizes:
            position_sizes = [t["usdcSize"] for t in all_trades]

        top_cats = [c for c, _ in sorted(cats.items(), key=lambda x: -x[1])[:3]]

        total_pnl = total_proceeds - total_cost
        roi_pct   = round(total_pnl / total_cost * 100, 1) if total_cost > 0 else 0
        profitable_months = sum(1 for v in month_pnl.values() if v > 0)
        total_months = len(month_pnl)

        return {
            "total_trades":      len(all_trades),
            "num_positions":     len(position_sizes),
            "avg_stake":         round(statistics.mean(position_sizes)),
            "max_stake":         round(max(position_sizes)),
            "months_active":     len(months),
            "profitable_months": profitable_months,
            "total_months":      total_months,
            "total_pnl":         round(total_pnl),
            "roi_pct":           roi_pct,
            "top_cats":          top_cats,
        }
    except Exception as e:
        print(f"Profile error {wallet[:8]}: {e}", flush=True)
        return {}


def _slug_to_cat(slug):
    if any(k in slug for k in ["mlb-", "-mlb"]):      return "MLB"
    if any(k in slug for k in ["nba-", "-nba"]):      return "NBA"
    if any(k in slug for k in ["nhl-", "-nhl"]):      return "NHL"
    if any(k in slug for k in ["nfl-", "-nfl"]):      return "NFL"
    if any(k in slug for k in ["fifwc", "world-cup",
                                "soccer", "copa",
                                "champions"]):         return "Soccer"
    if any(k in slug for k in ["ufc-", "-ufc"]):      return "UFC"
    return "Other"

# --- Market p90 --------------------------------------------------------------

def get_market_p90(event_slug, fill_size):
    """Return (p90_value, multiple). Both None on failure."""
    try:
        r = requests.get("https://data-api.polymarket.com/activity",
                         params={"eventSlug": event_slug, "limit": 200}, timeout=10)
        if not r.ok:
            return None, None
        trades = r.json()
        if not isinstance(trades, list):
            return None, None
        sizes = sorted([t.get("usdcSize", 0) for t in trades if t.get("usdcSize", 0) > 5])
        if len(sizes) < 5:
            return None, None
        p90 = sizes[int(len(sizes) * 0.9)]
        if p90 <= 0:
            return None, None
        return p90, round(fill_size / p90, 1)
    except Exception:
        return None, None

# --- Pinnacle devig ----------------------------------------------------------

def _detect_sport_key(event_slug):
    slug = (event_slug or "").lower()
    for keywords, sport_key in SPORT_MAP:
        if any(kw in slug for kw in keywords):
            return sport_key
    return None


def get_pinnacle_devig(event_slug, title, outcome, pm_price):
    """Fetch Pinnacle odds, devig to fair probability, compare to pm_price."""
    if not ODDS_API_KEY:
        return None
    sport_key = _detect_sport_key(event_slug)
    if not sport_key:
        return None

    title_lower = (title or "").lower()
    is_totals = any(x in title_lower for x in ["o/u", "over/under", " over ", " under "])
    market_type = "totals" if is_totals else "h2h"

    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
            params={
                "apiKey":     ODDS_API_KEY,
                "bookmakers": "pinnacle",
                "markets":    market_type,
                "regions":    "us",
                "oddsFormat": "american",
            }, timeout=10)
        if not r.ok:
            print(f"Odds API {r.status_code}: {r.text[:120]}", flush=True)
            return None
        games = r.json()
        if not isinstance(games, list) or not games:
            return None

        # Match game by overlapping words in title vs home/away team names
        title_words = set(title_lower.replace("-", " ").split())
        best_game, best_score = None, 0
        for game in games:
            home = (game.get("home_team") or "").lower().replace("-", " ")
            away = (game.get("away_team") or "").lower().replace("-", " ")
            score = len(title_words & set((home + " " + away).split()))
            if score > best_score:
                best_score, best_game = score, game
        if not best_game or best_score < 1:
            return None

        pinnacle = next((b for b in best_game.get("bookmakers", [])
                         if b["key"] == "pinnacle"), None)
        if not pinnacle:
            return None
        mkt = next((m for m in pinnacle.get("markets", [])
                    if m["key"] == market_type), None)
        if not mkt:
            return None

        outcomes = mkt.get("outcomes", [])
        is_3way = len(outcomes) == 3

        def am_to_prob(p):
            p = float(p)
            return 100 / (p + 100) if p > 0 else abs(p) / (abs(p) + 100)

        raw_probs = {o["name"].lower(): am_to_prob(o["price"]) for o in outcomes}
        total = sum(raw_probs.values())
        fair = {k: v / total for k, v in raw_probs.items()}

        outcome_lower = outcome.lower()
        fair_prob = fair.get(outcome_lower)
        if fair_prob is None:
            for name, prob in fair.items():
                if outcome_lower in name or name in outcome_lower:
                    fair_prob = prob
                    break

        # "No" market: find which team "Yes" maps to via title overlap, then No = 1 - Yes
        if fair_prob is None and outcome_lower == "no":
            best_team_prob, best_team_score = None, 0
            for name, prob in fair.items():
                if name == "draw":
                    continue
                score = len(title_words & set(name.replace("-", " ").split()))
                if score > best_team_score:
                    best_team_score, best_team_prob = score, prob
            if best_team_prob is not None and best_team_score > 0:
                fair_prob = 1.0 - best_team_prob

        if fair_prob is None:
            return None

        gap = round((pm_price - fair_prob) * 100, 2)  # positive = PM overpriced vs fair
        agrees = pm_price <= fair_prob                  # buying at/below fair = +EV

        if abs(gap) <= 1.5:
            edge_label = "IN-LINE"
        elif gap < -1.5:
            edge_label = f"EDGE +{abs(gap):.1f}pp below fair"
        else:
            edge_label = f"STALE {gap:+.1f}pp above fair"

        method = f"{'3way' if is_3way else '2way'}-proportional-devig(pinnacle)"
        return {
            "fair":       round(fair_prob, 4),
            "gap":        gap,
            "agrees":     agrees,
            "edge_label": edge_label,
            "method":     method,
            "home":       best_game.get("home_team"),
            "away":       best_game.get("away_team"),
        }
    except Exception as e:
        print(f"Pinnacle devig error: {e}", flush=True)
        return None

# --- Futures filter ----------------------------------------------------------

FUTURES_TITLE_KW = [
    "to win the", "win the world cup", "win the cup", "win the championship",
    "win the title", "win the league", "win the series", "win the tournament",
    "who wins the", "outright winner", "wc winner", "world cup winner",
]
FUTURES_SLUG_KW = ["-winner", "winner-", "-champion", "champion-", "outright"]

def _is_futures(title, slug):
    t = (title or "").lower()
    s = (slug or "").lower()
    if any(kw in t for kw in FUTURES_TITLE_KW):
        return True
    if any(kw in s for kw in FUTURES_SLUG_KW):
        return True
    return False

# --- Discord alert -----------------------------------------------------------

def price_to_american(price):
    if price <= 0 or price >= 1: return "N/A"
    if price >= 0.5: return f"-{round(price / (1 - price) * 100)}"
    return f"+{round((1 - price) / price * 100)}"


def send_discord_alert(trade, label, wallet):
    side = trade.get("side", "")
    if side not in ("BUY", "SELL"): return
    event_slug = trade.get("eventSlug", "")
    outcome    = trade.get("outcome", "")
    title      = trade.get("title", "")
    if not event_slug or not outcome: return
    if _is_futures(title, event_slug): return

    fill_size = trade.get("usdcSize", 0)
    price     = trade.get("price", 0)

    key = (wallet, event_slug, outcome)
    if side == "BUY":
        position_totals[key] = position_totals.get(key, 0) + fill_size
    else:
        position_totals[key] = position_totals.get(key, 0) - fill_size
    total = position_totals[key]

    min_size = WALLET_MIN_SIZE.get(wallet, MIN_SIZE)
    if fill_size < min_size: return

    # Enrichment
    _, p90_mult = get_market_p90(event_slug, fill_size)
    pin         = get_pinnacle_devig(event_slug, title, outcome, price)
    profile     = wallet_profiles.get(wallet, {})

    emoji  = "\U0001f7e2" if side == "BUY" else "\U0001f534"
    action = "NEW BET" if side == "BUY" else "CLOSED POSITION"

    lines = [
        f"<@{DISCORD_USER_ID}>",
        f"{emoji} **{action} -- {label} (Sharp)**",
        f"**{title}**",
        f"{side} **{outcome}** @ {round(price*100,1)}c  ({price_to_american(price)})",
        f"Fill: **${fill_size:,.0f}** | Total position: **${total:,.0f}**",
    ]

    # Size / EV row
    meta = []
    if p90_mult:
        meta.append(f"\U0001f4ca **{p90_mult}x p90**")
    if pin:
        ev_tag = "✅ **+EV vs Pinnacle**" if pin["agrees"] else "❌ **-EV vs Pinnacle**"
        meta.append(ev_tag)
    if meta:
        lines.append("  |  ".join(meta))

    # Pinnacle anchor row
    if pin:
        gap_str = f"{'+' if pin['gap'] > 0 else ''}{pin['gap']}pp"
        lines.append(
            f"\U0001f3af Pinnacle fair **{round(pin['fair']*100,1)}c** vs PM **{round(price*100,1)}c**"
            f" -> **{gap_str}** - {pin['edge_label']}"
        )
        lines.append(f"_{pin['method']} - {pin['home']} vs {pin['away']}_")

    # Wallet profile row
    if profile:
        cats  = ", ".join(profile.get("top_cats", [])) or "-"
        n_pos = profile.get("num_positions", "?")
        pnl   = profile.get("total_pnl", None)
        roi   = profile.get("roi_pct", None)
        p_mo  = profile.get("profitable_months", None)
        t_mo  = profile.get("total_months", None)
        if pnl is not None:
            arrow = "▲" if pnl >= 0 else "▼"
            sign  = "+" if pnl >= 0 else ""
            pnl_str = f"{arrow} **${abs(pnl):,}** ({sign}{roi}% ROI)"
        else:
            pnl_str = ""
        mo_str = f"{p_mo}/{t_mo} mo profitable" if p_mo is not None else ""
        lines.append(
            f"\U0001f4cb **{label}**: {profile['total_trades']} fills / {n_pos} positions | "
            f"Avg ${profile['avg_stake']:,} / Max ${profile['max_stake']:,} | {cats}"
        )
        if pnl_str or mo_str:
            sep = "  |  " if pnl_str and mo_str else ""
            lines.append(f"  {pnl_str}{sep}{mo_str}")

    lines.append(f"<https://polymarket.com/@{wallet}>")

    content = "\n".join(lines)
    try:
        requests.post(DISCORD_WEBHOOK, json={
            "content": content,
            "allowed_mentions": {"users": [DISCORD_USER_ID]}
        }, timeout=5)
    except Exception:
        pass

# --- Monitor loop ------------------------------------------------------------

def monitor_loop():
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

# --- Startup -----------------------------------------------------------------

def ensure_monitor():
    """Seed seen_hashes + pre-fetch wallet profiles, then start polling thread."""
    global _thread, _seeded
    with _thread_lock:
        if not _seeded:
            for wallet in WALLETS:
                for t in get_recent_trades(wallet):
                    h = t.get("transactionHash", "")
                    if h:
                        seen_hashes.add(h)
            for wallet, label in WALLETS.items():
                profile = fetch_wallet_profile(wallet)
                wallet_profiles[wallet] = profile
                print(f"Profile {label}: {profile.get('total_trades', 0)} trades, "
                      f"pnl=${profile.get('total_pnl', '?')}, "
                      f"roi={profile.get('roi_pct', '?')}%", flush=True)
            _seeded = True
            print(f"Seeded {len(seen_hashes)} hashes in pid={os.getpid()}", flush=True)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=monitor_loop, daemon=True)
            _thread.start()


ensure_monitor()


@app.route("/")
@app.route("/health")
def health():
    ensure_monitor()
    return jsonify({
        "status":          "running",
        "thread_alive":    _thread.is_alive() if _thread else False,
        "pid":             os.getpid(),
        "pinnacle":        bool(ODDS_API_KEY),
        "profiles_loaded": len(wallet_profiles),
    })
