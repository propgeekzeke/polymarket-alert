"""PolyAlert v2 - pre-game sharp tailing: tailability check, consensus, CLV scoreboard."""
import os, time, threading, requests, statistics, json, fcntl
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
    "0x448861155279dbf833d041b963e3ac854599e319": "#Flipadelphia",  # $561k/590d grinder, MLB+Soc
    # Added 2026-07-11 (friend-flagged whale; elite P&L, in-line CLV on big bets - validate live)
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563": "#Whale2c33",     # $6.2M/277d, +0.62% CLV avg (whale bets in-line), Soc+MLB
    # Added 2026-07-11 (corrected-P&L re-run: beat close + real realized P&L)
    "0x6d3c5bd13984b2de47c3a88ddc455309aab3d294": "#VeryLucky888",  # CLV +1.58%, 71% beat, $412k, 89%mo-up, Soc/UFC/MLB
    "0xb90494d9a5d8f71f1930b2aa4b599f95c344c255": "#Airpods123",     # CLV +0.98%, 71% beat, $1.02M whale, Soc/NBA
    "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1": "#Sharp2a2C",      # CLV +0.93%, 71% beat, $4.18M, Soc/NBA/NHL
    "0xec8d7bf83a1db5f06b9535985e58ffd17708dd71": "#Gardiner",       # CLV +0.71%, 59% beat, $185k, Soc/Tennis/MLB
    "0x32ed517a571c01b6e9adecf61ba81ca48ff2f960": "#SportMaster",    # CLV +0.74% (tail small bets), $1.44M, MLB/Soc
    "0xf1528f12e645462c344799b62b1b421a6a4c64aa": "#PhoneSculptor",  # CLV +0.58%, $772k, MLB/Soc, avg $18.6k
    "0x204f72f35326db932158cba6adff0b9a1da95e14": "#SwissTony",      # $15.86M/+$6.4M-30d, CLV in-line - validate live
    # Added 2026-07-15 (found via England-Argentina advance-market scan)
    "0xb61b2079b95f6b7476fd3203e0274ffb93308a06": "#Hot2Trot",       # CLV +4.62%, beats close 82%, pure soccer, $2.42M/24d
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
MAX_TRADE_AGE = 12 * 3600  # never alert on trades older than this (prevents re-pinging stale bets on restart)

WALLET_MIN_SIZE = {
    # Tier 1 scouts: thresholds ~= their avg PRE-GAME position size
    "0xa804390f80019699ab34a282c0df7528fba82a75": 3000,   # #RiverSkew (avg $4.7k)
    "0x23c8a4c266d10ba5846837eac391fea89ed6f293": 4000,   # #Netrol (avg $6.4k)
    "0xbca08c1bc204a34f2fddbe47b438b9bd42ac9705": 2000,   # #1WinStreak (avg $2.8k)
    "0x2a69660046d7acc4ab204d7cc5ba78b0776cd2f7": 2500,   # #UpTheBlues (avg $2.4k pre-game; live noise now filtered)
    "0x448861155279dbf833d041b963e3ac854599e319": 5000,   # #Flipadelphia (avg $9.2k)
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563": 5000,   # #Whale2c33 (huge sizing; $5k gate)
    "0x6d3c5bd13984b2de47c3a88ddc455309aab3d294": 2500,   # #VeryLucky888 (avg $2.7k)
    "0xb90494d9a5d8f71f1930b2aa4b599f95c344c255": 10000,  # #Airpods123 (whale, avg $53.8k)
    "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1": 10000,  # #Sharp2a2C (whale, avg $37k)
    "0xec8d7bf83a1db5f06b9535985e58ffd17708dd71": 3000,   # #Gardiner (avg $3.2k)
    "0x32ed517a571c01b6e9adecf61ba81ca48ff2f960": 2000,   # #SportMaster (tail small bets; avg $2.1k)
    "0xf1528f12e645462c344799b62b1b421a6a4c64aa": 8000,   # #PhoneSculptor (avg $18.6k)
    "0x204f72f35326db932158cba6adff0b9a1da95e14": 10000,  # #SwissTony (whale, avg $33.9k)
    "0xb61b2079b95f6b7476fd3203e0274ffb93308a06": 25000,  # #Hot2Trot (soccer whale, avg $81k)
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
clv_baseline = {}        # wallet -> historical CLV stats (computed at startup, EV-style %)
alerted_positions = set()  # (wallet, eventSlug, outcome, side) already pinged - prevents double pings

_thread = None
_thread_lock = threading.Lock()
_seeded = False
_monitor_owner = None    # None=undecided, True=this process runs the monitor, False=another does

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

# Prop-market noise to skip (exact-score props generate tons of low-signal pings)
PROP_SLUG_KW = ["exact-score", "correct-score"]
PROP_TITLE_KW = ["exact score", "correct score"]

def _is_prop_noise(title, slug):
    t = (title or "").lower()
    s = (slug or "").lower()
    return any(kw in s for kw in PROP_SLUG_KW) or any(kw in t for kw in PROP_TITLE_KW)

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


def compute_historical_clv(wallet, max_events=40):
    """One-time baseline: EV-style CLV over a wallet's recent pre-game $1k+ sport bets."""
    raw, offset = [], 0
    while offset < 4000:
        try:
            r = requests.get("https://data-api.polymarket.com/activity",
                             params={"user": wallet, "limit": 500, "offset": offset,
                                     "type": "TRADE"}, timeout=15)
            if not r.ok:
                break
            b = r.json()
        except Exception:
            break
        if not isinstance(b, list) or not b:
            break
        raw.extend(b)
        if len(b) < 500:
            break
        offset += 500
    samples = {}
    for t in raw:
        if t.get("side") != "BUY" or t.get("usdcSize", 0) < 1000:
            continue
        slug = (t.get("eventSlug") or "").lower()
        if not (slug.startswith("mlb-") or any(k in slug for k in
                ("fifwc", "epl-", "lal-", "ucl-", "uel-", "sea-", "bun-", "li1-", "mls-"))):
            continue
        k = (slug, t.get("asset", ""))
        s = samples.setdefault(k, {"stake": 0.0, "pxnum": 0.0, "first": t.get("timestamp", 0)})
        s["stake"] += t["usdcSize"]
        s["pxnum"] += t["usdcSize"] * t.get("price", 0)
        s["first"] = min(s["first"], t.get("timestamp", 0))
    items = sorted(samples.items(), key=lambda kv: -kv[1]["first"])[:max_events]
    clvs = []
    for (slug, asset), s in items:
        gs = get_game_start(slug)
        if not gs or not asset or s["first"] >= gs:
            continue
        close = get_closing_price(asset, gs)
        if close is None or close <= 0.001 or close >= 0.999:
            continue
        vwap = s["pxnum"] / s["stake"]
        if vwap <= 0:
            continue
        clvs.append((close / vwap - 1) * 100)
    if not clvs:
        return None
    return {"avg_clv_pp": round(sum(clvs) / len(clvs), 2),
            "beat_close_pct": round(sum(1 for c in clvs if c > 0) / len(clvs) * 100),
            "n": len(clvs)}

# --- State persistence -------------------------------------------------------

def save_state():
    try:
        with open(STATE_FILE, "w") as fh:
            json.dump({
                "watermarks": watermarks,
                "consensus_alerted": ["|".join(k) for k in consensus_alerted],
                "clv_log": clv_log[-2000:],
                "clv_baseline": clv_baseline,
                "alerted_positions": ["|".join(k) for k in alerted_positions],
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
        clv_baseline.update(d.get("clv_baseline", {}))
        for k in d.get("alerted_positions", []):
            parts = k.split("|")
            if len(parts) == 4:
                alerted_positions.add(tuple(parts))
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
    my_clv      = clv_baseline.get(wallet) or clv_stats(wallet).get(wallet)

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

    off_pnl = get_official_pnl(wallet)
    if off_pnl.get("all") is not None:
        allp = off_pnl["all"]
        d30  = off_pnl.get("d30", 0)
        arrow = "▲" if allp >= 0 else "▼"
        d30s  = f"{'+' if d30 >= 0 else '-'}${abs(d30):,} 30d"
        cats  = ", ".join(profile.get("top_cats", [])) if profile else ""
        line = f"\U0001f4cb **{label}**  ·  {arrow} **${abs(allp):,}** lifetime  ·  {d30s}"
        if cats:
            line += f"  ·  {cats}"
        lines.append(line)
    elif profile and profile.get("total_pnl") is not None:
        pnl = profile.get("total_pnl"); roi = profile.get("roi_pct")
        arrow = "▲" if pnl >= 0 else "▼"
        sign = "+" if pnl >= 0 else ""
        cats = ", ".join(profile.get("top_cats", [])) or "-"
        lines.append(f"\U0001f4cb **{label}**  ·  {arrow} **${abs(pnl):,}** ({sign}{roi}% ROI)  ·  {cats}")
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
    if _is_prop_noise(title, event_slug):
        return

    fill_size = trade.get("usdcSize", 0)
    ts        = trade.get("timestamp", 0)

    # Recency guard: never alert on stale trades (fixes re-pinging days-old bets after a restart,
    # esp. spread/derivative markets where game-start time is unknown so the pre-game filter is bypassed)
    if ts and ts < time.time() - MAX_TRADE_AGE:
        return

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
    if pkey in alerted_positions:   # already pinged this position -> suppress multi-fill/add double pings
        return
    alert_progress[pkey] = alert_progress.get(pkey, 0) + fill_size
    if alert_progress[pkey] < WALLET_MIN_SIZE.get(wallet, MIN_SIZE):
        return
    accumulated = alert_progress.pop(pkey)
    alerted_positions.add(pkey)
    send_discord_alert(trade, label, wallet, gs, accumulated)

# --- Monitor loop ------------------------------------------------------------

def monitor_loop():
    print(f"Monitor thread running in pid={os.getpid()}", flush=True)
    for wallet, label in WALLETS.items():
        if wallet in clv_baseline:
            continue
        try:
            base = compute_historical_clv(wallet)
            if base:
                clv_baseline[wallet] = base
                print(f"CLV baseline {label}: {base['avg_clv_pp']:+.2f}% avg, "
                      f"beats {base['beat_close_pct']}% (n={base['n']})", flush=True)
        except Exception as e:
            print(f"CLV baseline error {label}: {e}", flush=True)
    save_state()
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
                for k in list(alerted_positions):
                    g = game_starts.get(k[1])
                    if g and time.time() > g:
                        alerted_positions.discard(k)
        except Exception as e:
            print(f"Monitor loop error: {e}", flush=True)
            time.sleep(5)

# --- Startup -----------------------------------------------------------------

def ensure_monitor():
    """Load state, seed seen hashes + wallet profiles, start polling thread."""
    global _thread, _seeded, _monitor_owner
    with _thread_lock:
        if _monitor_owner is None:
            # Singleton monitor: only the process that grabs this file lock polls + alerts,
            # so duplicate gunicorn workers can't each fire their own identical alerts.
            try:
                fh = open("/tmp/polyalert_monitor.lock", "w")
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                globals()["_monitor_lock_fh"] = fh   # keep ref so the lock is held for process life
                _monitor_owner = True
                print(f"Monitor lock acquired by pid={os.getpid()}", flush=True)
            except (OSError, IOError):
                _monitor_owner = False
                print(f"Monitor lock held elsewhere; pid={os.getpid()} serves HTTP only", flush=True)
        if not _monitor_owner:
            return
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



BOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sharp Wallet Tracker</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--line:#21262d;--fg:#e6edf3;--mut:#8b949e;--grn:#3fb950;--red:#f85149;--acc:#58a6ff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
  header{padding:18px 20px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  h1{font-size:18px;margin:0}
  .sub{color:var(--mut);font-size:12px}
  .wrap{max-width:1100px;margin:0 auto;padding:16px 20px 60px}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:right;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.03em;cursor:default}
  .pos{color:var(--grn)}.neg{color:var(--red)}.mut{color:var(--mut)}
  tr.w{cursor:pointer}
  tr.w:hover{background:#1b222b}
  .name{font-weight:600}
  .chip{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;background:#21262d;color:var(--mut)}
  .bets{background:#0b0f14}
  .bets td{padding:0}
  .bets .inner{padding:6px 10px 12px 24px}
  .bt{width:100%;border-collapse:collapse}
  .bt th,.bt td{border-bottom:1px solid #1c2129;padding:6px 8px;font-size:12.5px}
  .empty{color:var(--mut);padding:8px 24px;font-size:12.5px}
  a{color:var(--acc);text-decoration:none}
  .rk{color:var(--mut);width:22px;display:inline-block}
  #err{color:var(--red);padding:20px}
  .reload{margin-left:auto;background:#21262d;border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px}
  .reload:hover{border-color:var(--acc)}
</style>
</head>
<body>
<header>
  <h1>Sharp Wallet Tracker</h1>
  <span class="sub" id="meta">loading…</span>
  <button class="reload" onclick="load()">Reload</button>
</header>
<div class="wrap">
  <div id="err"></div>
  <table id="tbl">
    <thead><tr>
      <th>Wallet</th><th>Lifetime P&amp;L</th><th>30d</th><th>ROI</th>
      <th>CLV %</th><th>Beat close</th><th>Active bets</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
</div>
<script>
// If hosting this file yourself (not served by the bot), set DATA_URL to your bot URL, e.g.
// const DATA_URL = "https://your-bot.onrender.com/dashboard.json";
const DATA_URL = (location.pathname.endsWith("/board") ? "/dashboard.json" : "/dashboard.json");
function money(n){ if(n===null||n===undefined) return '<span class="mut">—</span>';
  const s=n<0?'neg':'pos'; return '<span class="'+s+'">'+(n<0?'-':'')+'
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
+Math.abs(n).toLocaleString()+'</span>'; }
function pct(n,suf){ if(n===null||n===undefined) return '<span class="mut">—</span>';
  const s=n<0?'neg':(n>0?'pos':'mut'); return '<span class="'+s+'">'+(n>0?'+':'')+n+(suf||'')+'</span>'; }
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function fmtDate(d){ if(!d) return ''; try{ return new Date(d).toLocaleDateString(undefined,{month:'short',day:'numeric'}); }catch(e){ return ''; } }

async function load(){
  document.getElementById('err').textContent='';
  document.getElementById('meta').textContent='loading…';
  let data;
  try{ const r=await fetch(DATA_URL,{cache:'no-store'}); data=await r.json(); }
  catch(e){ document.getElementById('err').textContent='Could not reach '+DATA_URL+' — is the bot running? '+e; document.getElementById('meta').textContent=''; return; }
  const rows=document.getElementById('rows'); rows.innerHTML='';
  data.wallets.forEach((w,i)=>{
    const tr=document.createElement('tr'); tr.className='w';
    const n=(w.active||[]).length;
    tr.innerHTML=
      '<td><span class="rk">'+(i+1)+'</span><span class="name">'+esc(w.label)+'</span></td>'+
      '<td>'+money(w.lifetime_pnl)+'</td>'+
      '<td>'+money(w.pnl_30d)+'</td>'+
      '<td>'+pct(w.roi_pct,'%')+'</td>'+
      '<td>'+pct(w.clv_pct,'%')+' <span class="mut">'+(w.clv_n?('n='+w.clv_n):'')+'</span></td>'+
      '<td>'+(w.beat_pct!=null?w.beat_pct+'%':'<span class="mut">—</span>')+'</td>'+
      '<td><span class="chip">'+n+' open</span></td>';
    const det=document.createElement('tr'); det.className='bets'; det.style.display='none';
    let inner='<div class="inner">';
    if(n){
      inner+='<table class="bt"><thead><tr><th style="text-align:left">Market</th><th style="text-align:left">Side</th><th>Entry</th><th>Now</th><th>Value</th><th>P&amp;L</th><th>Ends</th></tr></thead><tbody>';
      w.active.forEach(b=>{
        inner+='<tr><td style="text-align:left"><a href="https://polymarket.com/event/'+esc(b.slug)+'" target="_blank">'+esc(b.title)+'</a></td>'+
          '<td style="text-align:left">'+esc(b.outcome)+'</td>'+
          '<td>'+b.avg+'c</td><td>'+b.cur+'c</td>'+
          '<td>
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
+ (b.value||0).toLocaleString()+'</td>'+
          '<td>'+money(b.pnl)+' '+pct(b.pnl_pct,'%')+'</td>'+
          '<td class="mut">'+fmtDate(b.end)+'</td></tr>';
      });
      inner+='</tbody></table>';
    } else { inner+='<div class="empty">No active (non-futures) positions.</div>'; }
    inner+='</div>';
    det.innerHTML='<td colspan="7">'+inner+'</td>';
    tr.onclick=()=>{ det.style.display = det.style.display==='none'?'':'none'; };
    rows.appendChild(tr); rows.appendChild(det);
  });
  const dt=new Date(data.updated);
  document.getElementById('meta').textContent=data.wallets.length+' wallets · updated '+dt.toLocaleString()+' · click a row for active bets';
}
load();
</script>
</body>
</html>
"""

# --- Dashboard (JSON API + served HTML page) ---------------------------------

_official_pnl_cache = {}   # wallet -> (ts, {all, d30})
_dash_cache = {"ts": 0, "data": None}

def get_official_pnl(wallet):
    now = time.time()
    c = _official_pnl_cache.get(wallet)
    if c and now - c[0] < 3600:
        return c[1]
    out = {}
    try:
        d = requests.get("https://user-pnl-api.polymarket.com/user-pnl",
                         params={"user_address": wallet, "interval": "all", "fidelity": "1d"},
                         timeout=10).json()
        if isinstance(d, list) and d:
            out = {"all": round(d[-1]["p"]),
                   "d30": round(d[-1]["p"] - d[-31]["p"]) if len(d) > 31 else round(d[-1]["p"])}
    except Exception:
        pass
    _official_pnl_cache[wallet] = (now, out)
    return out


def get_open_positions(wallet):
    try:
        d = requests.get("https://data-api.polymarket.com/positions",
                         params={"user": wallet, "sortBy": "CURRENT",
                                 "sortDirection": "DESC", "limit": 500}, timeout=15).json()
    except Exception:
        return []
    out = []
    for p in d if isinstance(d, list) else []:
        if p.get("redeemable") or (p.get("currentValue", 0) or 0) < 50:
            continue
        title = p.get("title", "")
        slug = p.get("eventSlug", "") or p.get("slug", "")
        if _is_futures(title, slug) or _is_prop_noise(title, slug):
            continue
        out.append({
            "title": title, "outcome": p.get("outcome", ""),
            "avg": round((p.get("avgPrice", 0) or 0) * 100, 1),
            "cur": round((p.get("curPrice", 0) or 0) * 100, 1),
            "value": round(p.get("currentValue", 0) or 0),
            "pnl": round(p.get("cashPnl", 0) or 0),
            "pnl_pct": round(p.get("percentPnl", 0) or 0, 1),
            "end": p.get("endDate", ""), "slug": slug,
        })
    return out


def build_dashboard():
    now = time.time()
    if _dash_cache["data"] and now - _dash_cache["ts"] < 120:
        return _dash_cache["data"]
    rows = []
    for wallet, label in WALLETS.items():
        prof = wallet_profiles.get(wallet, {})
        clv = clv_baseline.get(wallet) or clv_stats(wallet).get(wallet) or {}
        o = get_official_pnl(wallet)
        rows.append({
            "label": label, "address": wallet,
            "lifetime_pnl": o.get("all"), "pnl_30d": o.get("d30"),
            "roi_pct": prof.get("roi_pct"),
            "clv_pct": clv.get("avg_clv_pp"), "beat_pct": clv.get("beat_close_pct"),
            "clv_n": clv.get("n"),
            "active": get_open_positions(wallet),
        })
    rows.sort(key=lambda r: (r["clv_pct"] if r["clv_pct"] is not None else -99), reverse=True)
    data = {"updated": datetime.now(timezone.utc).isoformat(), "wallets": rows}
    _dash_cache["ts"] = now
    _dash_cache["data"] = data
    return data


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/dashboard.json")
def dashboard_json():
    ensure_monitor()
    return jsonify(build_dashboard())


@app.route("/board")
def board():
    return BOARD_HTML


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
