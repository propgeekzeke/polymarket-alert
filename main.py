"""PolyAlert v2 - pre-game sharp tailing: tailability check, consensus, CLV scoreboard."""
import os, time, threading, requests, statistics, json
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)

WALLETS = {
    "0x26437896ed9dfeb2f69765edcafe8fdceaab39ae": "#Latina",
    "0x709e8dcb133555794decc598e07f2c923b8366f5": "#0X70",
    "0xf0318c32136c2db7fec88b84869aee6a1106c80c": "#BTB",
    "0xfea31bc088000ff909be1dfd8d0e3f2c7ef2d227": "#0XFE",
    # Tier 1 from wallet scout 2026-07-09 (CLV-positive, profitable, $1k+ avg stakes)
    "0xa804390f80019699ab34a282c0df7528fba82a75": "#RiverSkew",     # CLV +0.76pp, 67% beat close, MLB+Soc
    "0x23c8a4c266d10ba5846837eac391fea89ed6f293": "#Netrol",        # CLV +0.58pp, 72% beat close, 6mo, Soc
    "0xbca08c1bc204a34f2fddbe47b438b9bd42ac9705": "#1WinStreak",    # $992k/75d, wtd CLV +0.81pp, MLB
    "0x2a69660046d7acc4ab204d7cc5ba78b0776cd2f7": "#UpTheBlues",    # $524k, CLV +0.44pp, high volume, Soc
    "0xa80e584e189865e8289403bd96ed52d67e816aa1": "#Allegiant",     # $274k/29d, CLV +0.09pp, Soc
    "0x448861155279dbf833d041b963e3ac854599e319": "#Flipadelphia",  # $561k/590d grinder, MLB+Soc
}

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
DISCORD_USER_ID = "221025359884320770"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
POLL_INTERVAL = 20
MIN_SIZE = 10000
CONSENSUS_MIN = 1000     # any tracked-wallet pre-game BUY >= this counts toward consensus
TAIL_OK_PP = 1.0         # current price within this of entry -> tailable
TAIL_MEH_PP = 2.5        # within this -> caution; beyond -> line gone
STATE_FILE = "/tmp/polyalert_state.json"

WALLET_MIN_SIZE = {
    # Tier 1 scouts: thresholds ~= their avg PRE-GAME position size
    "0xa804390f80019699ab34a282c0df7528fba82a75": 3000,   # #RiverSkew (avg $4.7k)
    "0x23c8a4c266d10ba5846837eac391fea89ed6f293": 4000,   # #Netrol (avg $6.4k)
    "0xbca08c1bc204a34f2fddbe47b438b9bd42ac9705": 2000,   # #1WinStreak (avg $2.8k)
    "0x2a69660046d7acc4ab204d7cc5ba78b0776cd2f7": 2500,   # #UpTheBlues (avg $2.4k pre-game; live noise now filtered)
    "0xa80e584e189865e8289403bd96ed52d67e816aa1": 5000,   # #Allegiant (avg $7.2k)
    "0x448861155279dbf833d041b963e3ac854599e319": 5000,   # #Flipadelphia (avg $9.2k)
}

# --- Runtime state -----------------------------------------------------------

seen_hashes = {}         # tx hash -> trade ts (pruned after 48h)
watermarks = {}          # wallet -> newest processed trade ts
position_totals = {}     # (wallet, eventSlug, outcome) -> running USDC total
wallet_profiles = {}     # wallet -> profile dict (cached at startup)
game_starts = {}         # eventSlug -> game start ts (None = no game time found)
consensus_book = {}      # (eventSlug, outcome) -> {wallet: total USDC}
consensus_alerted = set()  # (eventSlug, outcome) already alerted
alert_progress = {}      # (wallet, eventSlug, outcome, side) -> USDC accumulated since last alert
clv_log = []             # alerted BUYs pending/graded vs closing line

_thread = None
_thread_lock = threading.Lock()
_seeded = False

# Slug keyword -> The Odds API sport key
SPORT_MAP = [
    (["mlb-", "-mlb-"],                    "baseball_mlb"),
    (["nba-", "-nba-"],                    "basketball_nba"),
    (["nfl-", "-nfl-"],                    "americanfootball_nfl"),
    (["nhl-", "-nhl-"],                    "icehockey_nhl"),
    (["cfb-", "ncaaf"],                    "americanfootball_ncaaf"),
    (["cbb-", "ncaab"],                    "basketball_ncaab"),
    (["wnba-"],                            "basketball_wnba"),
    (["fifwc", "world-cup", "2026-fifa"],  "soccer_fifa_world_cup"),
    (["epl-"],                             "soccer_epl"),
    (["lal-", "la-liga", "laliga"],        "soccer_spain_la_liga"),
    (["ucl-", "champions-league"],         "soccer_uefa_champs_league"),
    (["uel-", "europa-league"],            "soccer_uefa_europa_league"),
    (["sea-", "serie-a"],                  "soccer_italy_serie_a"),
    (["bun-", "bundesliga"],               "soccer_germany_bundesliga"),
    (["li1-", "ligue-1", "ligue1"],        "soccer_france_ligue_one"),
    (["mls-"],                             "soccer_usa_mls"),
    (["ufc-", "-ufc-"],                    "mma_mixed_martial_arts"),
]

# --- Polymarket API ----------------------------------------------------------

def get_recent_trades(wallet):
    try:
        r = requests.get("https://data-api.polymarket.com/activity",
                         params={"user": wallet, "limit": 25}, timeout=10)
        if not r.ok:
            return []
        d = r.json()
        return d if isinstance(d, list) else []
    except Exception:
        return []


def get_game_start(event_slug):
    """Game start ts from gamma API (cached). None = no game time (not a game market)."""
    if event_slug in game_starts:
        return game_starts[event_slug]
    ts = None
    try:
        evs = requests.get("https://gamma-api.polymarket.com/events",
                           params={"slug": event_slug}, timeout=10).json()
        if evs:
            for m in evs[0].get("markets", []):
                g = m.get("gameStartTime")
                if g:
                    g = g.strip().replace(" ", "T")
                    if g.endswith("+00"):
                        g += ":00"
                    ts = int(datetime.fromisoformat(g).timestamp())
                    break
    except Exception:
        pass
    if len(game_starts) > 3000:
        game_starts.clear()
    game_starts[event_slug] = ts
    return ts


def get_current_ask(token_id):
    """Best price you'd pay to buy this outcome right now."""
    try:
        r = requests.get("https://clob.polymarket.com/price",
                         params={"token_id": token_id, "side": "buy"}, timeout=5)
        if r.ok:
            return float(r.json().get("price"))
    except Exception:
        pass
    return None


def get_closing_price(token_id, gs):
    """Last CLOB price at/before game start."""
    try:
        r = requests.get("https://clob.polymarket.com/prices-history",
                         params={"market": token_id, "startTs": gs - 6 * 3600,
                                 "endTs": gs + 300, "fidelity": 5}, timeout=10)
        if r.ok:
            pts = [x for x in r.json().get("history", []) if x["t"] <= gs]
            if pts:
                return pts[-1]["p"]
    except Exception:
        pass
    return None

# --- Wallet profiler ---------------------------------------------------------

def fetch_wallet_profile(wallet):
    """Paginate through ALL activity records for a wallet and compute summary stats."""
    try:
        raw = []
        offset = 0
        while offset < 6000:
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
        month_pnl = {}
        cats = {}
        net_pos = {}
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
                total_proceeds += usdc
                if mo:
                    month_pnl[mo] = month_pnl.get(mo, 0) + usdc

        all_trades = [t for t in raw if t.get("type") == "TRADE"
                      and t.get("side") in ("BUY", "SELL") and t.get("usdcSize", 0) > 0]
        if not all_trades:
            return {}

        position_sizes = [v for v in net_pos.values() if v > 0]
        if not position_sizes:
            position_sizes = [t["usdcSize"] for t in all_trades]

        top_cats = [c for c, _ in sorted(cats.items(), key=lambda x: -x[1])[:3]]
        total_pnl = total_proceeds - total_cost
        roi_pct   = round(total_pnl / total_cost * 100, 1) if total_cost > 0 else 0

        return {
            "total_trades":      len(all_trades),
            "num_positions":     len(position_sizes),
            "avg_stake":         round(statistics.mean(position_sizes)),
            "max_stake":         round(max(position_sizes)),
            "months_active":     len(months),
            "profitable_months": sum(1 for v in month_pnl.values() if v > 0),
            "total_months":      len(month_pnl),
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
    if any(k in slug for k in ["fifwc", "world-cup", "soccer", "copa", "champions",
                                "epl-", "lal-", "ucl-", "uel-", "sea-", "bun-",
                                "li1-", "mls-"]):      return "Soccer"
    if any(k in slug for k in ["ufc-", "-ufc"]):      return "UFC"
    return "Other"

# --- Market p90 (fixed: /activity now requires user param; use /trades) -------

def get_market_p90(condition_id, fill_size):
    """Return (p90_value, multiple) using recent trades in this market."""
    if not condition_id:
        return None, None
    try:
        r = requests.get("https://data-api.polymarket.com/trades",
                         params={"market": condition_id, "limit": 200}, timeout=10)
        if not r.ok:
            return None, None
        trades = r.json()
        if not isinstance(trades, list):
            return None, None
        sizes = sorted([t.get("size", 0) * t.get("price", 0) for t in trades
                        if t.get("size", 0) * t.get("price", 0) > 5])
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

        gap = round((pm_price - fair_prob) * 100, 2)
        agrees = pm_price <= fair_prob

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
    return any(kw in t for kw in FUTURES_TITLE_KW) or any(kw in s for kw in FUTURES_SLUG_KW)

# --- CLV scoreboard ----------------------------------------------------------

def grade_pending_clv(max_grades=5):
    now = time.time()
    graded = 0
    for e in clv_log:
        if e.get("clv") is not None or e.get("failed"):
            continue
        gs = e.get("gs")
        if not gs or now < gs + 600:
            continue
        close = get_closing_price(e["asset"], gs)
        if close is None or close <= 0.001 or close >= 0.999:
            if now > gs + 7200:
                e["failed"] = True
            continue
        # EV-style CLV: closing price treated as true prob; entry is what you paid.
        # (close / entry - 1) * 100  -> +% you beat the close in payout terms.
        e["clv"] = round((close / e["entry"] - 1) * 100, 2) if e["entry"] > 0 else 0.0
        e["close"] = close
        graded += 1
        if graded >= max_grades:
            break


def clv_stats(wallet=None):
    out = {}
    for e in clv_log:
        if e.get("clv") is None:
            continue
        if wallet and e["wallet"] != wallet:
            continue
        s = out.setdefault(e["wallet"], {"label": e["label"], "n": 0, "sum": 0.0, "beat": 0})
        s["n"] += 1
        s["sum"] += e["clv"]
        s["beat"] += 1 if e["clv"] > 0 else 0
    for s in out.values():
        s["avg_clv_pp"] = round(s["sum"] / s["n"], 2)
        s["beat_close_pct"] = round(s["beat"] / s["n"] * 100)
        del s["sum"], s["beat"]
    return out

# --- State persistence -------------------------------------------------------

def save_state():
    try:
        with open(STATE_FILE, "w") as fh:
            json.dump({
                "watermarks": watermarks,
                "consensus_alerted": ["|".join(k) for k in consensus_alerted],
                "clv_log": clv_log[-2000:],
            }, fh)
    except Exception:
        pass


def load_state():
    global clv_log
    try:
        with open(STATE_FILE) as fh:
            d = json.load(fh)
        watermarks.update(d.get("watermarks", {}))
        for k in d.get("consensus_alerted", []):
            parts = k.split("|")
            if len(parts) == 2:
                consensus_alerted.add((parts[0], parts[1]))
        clv_log = d.get("clv_log", [])
        print(f"State loaded: {len(watermarks)} watermarks, {len(clv_log)} CLV entries", flush=True)
    except Exception:
        pass

# --- Discord alerts ----------------------------------------------------------

def price_to_american(price):
    if price <= 0 or price >= 1: return "N/A"
    if price >= 0.5: return f"-{round(price / (1 - price) * 100)}"
    return f"+{round((1 - price) / price * 100)}"


def _post_discord(content):
    try:
        requests.post(DISCORD_WEBHOOK, json={
            "content": content,
            "allowed_mentions": {"users": [DISCORD_USER_ID]}
        }, timeout=5)
    except Exception:
        pass


def _countdown(gs):
    if not gs:
        return "no game time"
    mins = int((gs - time.time()) / 60)
    if mins < 0:
        return "started"
    return f"starts in {mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"starts in {mins}m"


def send_discord_alert(trade, label, wallet, gs, accumulated=None):
    side       = trade.get("side", "")
    event_slug = trade.get("eventSlug", "")
    outcome    = trade.get("outcome", "")
    title      = trade.get("title", "")
    fill_size  = trade.get("usdcSize", 0)
    price      = trade.get("price", 0)
    asset      = trade.get("asset", "")
    cid        = trade.get("conditionId", "")

    if accumulated is None:
        accumulated = fill_size
    total = position_totals.get((wallet, event_slug, outcome), fill_size)

    now_price   = get_current_ask(asset) if asset else None
    _, p90_mult = get_market_p90(cid, accumulated)
    pin         = get_pinnacle_devig(event_slug, title, outcome, now_price or price)
    profile     = wallet_profiles.get(wallet, {})
    my_clv      = clv_stats(wallet).get(wallet)

    emoji  = "\U0001f7e2" if side == "BUY" else "\U0001f534"
    action = "NEW BET" if side == "BUY" else "CLOSED POSITION"

    avg_stake = profile.get("avg_stake") if profile else None
    if avg_stake:
        mult = accumulated / avg_stake
        bet_line = f"Bet **${accumulated:,.0f}**  ({mult:.1f}x their avg bet of ${avg_stake:,})"
    else:
        bet_line = f"Bet **${accumulated:,.0f}**"
    if total and abs(total) > accumulated * 1.05:
        bet_line += f"  |  position **${total:,.0f}**"

    lines = [
        f"<@{DISCORD_USER_ID}>",
        f"{emoji} **{action} -- {label} (Sharp)** | PRE-GAME ({_countdown(gs)})",
        f"**{title}**",
        f"{side} **{outcome}** @ {round(price*100,1)}c  ({price_to_american(price)})",
        bet_line,
    ]

    meta = []
    if p90_mult:
        meta.append(f"\U0001f4ca **{p90_mult}x mkt p90**")
    if pin:
        ev_tag = "✅ **+EV vs Pinnacle**" if pin["agrees"] else "❌ **-EV vs Pinnacle**"
        meta.append(ev_tag)
    if meta:
        lines.append("  |  ".join(meta))

    if pin:
        ref = now_price if now_price is not None else price
        gap_str = f"{'+' if pin['gap'] > 0 else ''}{pin['gap']}pp"
        lines.append(
            f"\U0001f3af Pinnacle fair **{round(pin['fair']*100,1)}c** vs PM **{round(ref*100,1)}c**"
            f" -> **{gap_str}** - {pin['edge_label']}"
        )
        lines.append(f"_{pin['method']} - {pin['home']} vs {pin['away']}_")

    if profile:
        cats  = ", ".join(profile.get("top_cats", [])) or "-"
        pnl   = profile.get("total_pnl")
        roi   = profile.get("roi_pct")
        if pnl is not None:
            arrow = "▲" if pnl >= 0 else "▼"
            sign  = "+" if pnl >= 0 else ""
            lines.append(
                f"\U0001f4cb **{label}**  ·  {arrow} **${abs(pnl):,}** ({sign}{roi}% ROI)  ·  "
                f"{profile.get('num_positions', '?')} positions  ·  {cats}"
            )
    if my_clv:
        lines.append(f"\U0001f4c8 **CLV {my_clv['avg_clv_pp']:+.2f}%** avg  ·  "
                     f"beats close **{my_clv['beat_close_pct']}%**  ·  n={my_clv['n']}")

    lines.append(f"<https://polymarket.com/@{wallet}>")
    _post_discord("\n".join(lines))

    if side == "BUY" and asset and gs:
        clv_log.append({"wallet": wallet, "label": label, "slug": event_slug,
                        "asset": asset, "entry": price, "gs": gs,
                        "ts": trade.get("timestamp", 0), "clv": None})


def send_consensus_alert(event_slug, outcome, title, book, trade):
    gs = game_starts.get(event_slug)
    names = [f"{WALLETS.get(w, w[:8])} (${amt:,.0f})" for w, amt in
             sorted(book.items(), key=lambda x: -x[1])]
    price = trade.get("price", 0)
    asset = trade.get("asset", "")
    now_price = get_current_ask(asset) if asset else None
    lines = [
        f"<@{DISCORD_USER_ID}>",
        f"\U0001f6a8\U0001f6a8 **CONSENSUS -- {len(book)} sharps on the same side** | PRE-GAME ({_countdown(gs)})",
        f"**{title}**",
        f"BUY **{outcome}** -- " + ", ".join(names),
    ]
    if now_price is not None:
        lines.append(f"Current price: **{round(now_price*100,1)}c** ({price_to_american(now_price)})")
    else:
        lines.append(f"Last fill: **{round(price*100,1)}c**")
    _post_discord("\n".join(lines))

# --- Trade handling ----------------------------------------------------------

def handle_trade(trade, label, wallet):
    side = trade.get("side", "")
    if side not in ("BUY", "SELL"):
        return
    event_slug = trade.get("eventSlug", "")
    outcome    = trade.get("outcome", "")
    title      = trade.get("title", "")
    if not event_slug or not outcome:
        return
    if _is_futures(title, event_slug):
        return

    fill_size = trade.get("usdcSize", 0)
    ts        = trade.get("timestamp", 0)

    gs = get_game_start(event_slug)
    if gs and ts >= gs:
        return

    key = (wallet, event_slug, outcome)
    position_totals[key] = position_totals.get(key, 0) + (fill_size if side == "BUY" else -fill_size)

    if side == "BUY" and fill_size >= CONSENSUS_MIN:
        book = consensus_book.setdefault((event_slug, outcome), {})
        book[wallet] = book.get(wallet, 0) + fill_size
        if len(book) >= 2 and (event_slug, outcome) not in consensus_alerted:
            consensus_alerted.add((event_slug, outcome))
            send_consensus_alert(event_slug, outcome, title, book, trade)

    pkey = (wallet, event_slug, outcome, side)
    alert_progress[pkey] = alert_progress.get(pkey, 0) + fill_size
    if alert_progress[pkey] < WALLET_MIN_SIZE.get(wallet, MIN_SIZE):
        return
    accumulated = alert_progress.pop(pkey)
    send_discord_alert(trade, label, wallet, gs, accumulated)

# --- Monitor loop ------------------------------------------------------------

def monitor_loop():
    print(f"Monitor thread running in pid={os.getpid()}", flush=True)
    cycles = 0
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            for wallet, label in WALLETS.items():
                try:
                    for trade in reversed(get_recent_trades(wallet)):
                        tx = trade.get("transactionHash", "")
                        ts = trade.get("timestamp", 0)
                        if not tx or tx in seen_hashes:
                            continue
                        seen_hashes[tx] = ts
                        watermarks[wallet] = max(watermarks.get(wallet, 0), ts)
                        if trade.get("type") == "TRADE":
                            handle_trade(trade, label, wallet)
                except Exception as e:
                    print(f"Error polling {label}: {e}", flush=True)

            grade_pending_clv()

            cycles += 1
            if cycles % 15 == 0:
                save_state()
                cutoff = time.time() - 48 * 3600
                for tx in [t for t, ts in seen_hashes.items() if ts and ts < cutoff]:
                    del seen_hashes[tx]
                for k in list(consensus_book):
                    g = game_starts.get(k[0])
                    if g and time.time() > g:
                        del consensus_book[k]
                for k in list(alert_progress):
                    g = game_starts.get(k[1])
                    if g and time.time() > g:
                        del alert_progress[k]
        except Exception as e:
            print(f"Monitor loop error: {e}", flush=True)
            time.sleep(5)

# --- Startup -----------------------------------------------------------------

def ensure_monitor():
    """Load state, seed seen hashes + wallet profiles, start polling thread."""
    global _thread, _seeded
    with _thread_lock:
        if not _seeded:
            load_state()
            now = time.time()
            for wallet in WALLETS:
                wm = watermarks.get(wallet, 0)
                for t in get_recent_trades(wallet):
                    h  = t.get("transactionHash", "")
                    ts = t.get("timestamp", 0)
                    if not h:
                        continue
                    if wm and ts > wm and ts > now - 900:
                        continue
                    seen_hashes[h] = ts
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
        "clv_logged":      len(clv_log),
        "clv_graded":      sum(1 for e in clv_log if e.get("clv") is not None),
    })


@app.route("/clv")
def clv():
    ensure_monitor()
    stats = clv_stats()
    return jsonify({
        "wallets": {s["label"]: {k: v for k, v in s.items() if k != "label"}
                    for s in stats.values()},
        "pending": sum(1 for e in clv_log if e.get("clv") is None and not e.get("failed")),
        "note": "avg_clv_pp > 0 and beat_close_pct > 50 = wallet still sharp; consider demoting anyone negative over n>=30",
    })
