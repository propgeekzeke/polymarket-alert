"""PolyAlert v2 - pre-game sharp tailing: tailability check, consensus, CLV scoreboard."""
import os, re, time, threading, requests, statistics, json, fcntl, unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)

WALLETS = {
    # Core: consistent CLV-beaters (curated 2026-07-15)
    "0x076daa87c4fe1a85402a9b6b8e0a866224388d4c": "#Sharp076d",    # 71% beat, +1.82% CLV / +3.14% wtd (n=21), $3.76M, soccer +15.6% ROI
    # Whales: don't beat CLV (likely bet late to hide it) but hugely profitable - tail on selection
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563": "#Whale2c33",    # $6.52M all-time, in-line CLV
    # CFB specialists (prober 2026-09-12: 567 games / 9,112 markets scanned, full-history pre-game CLV)
    # Prober 2026-10-09: 4,794 markets / 3,420 wallets scanned, full-history trade-based CLV
    "0x924379a79c64b77ad5816ad362122a5f6228658e": "#KchTemp",          # 68% beat, +1.13% CLV (n=236, 0% live); NFL 78%/n55; $2.51M, avg $47k
    "0xf0318c32136c2db7fec88b84869aee6a1106c80c": "#BreakTheBank",     # 71% beat, +1.91% CLV (n=119); soccer +4.18%/71%, NFL 69%; $5.08M, avg $28k
    # Prober 2026-11-08 (Data API v2): 650 sports-leaderboard wallets -> 45d settled ROI -> pre-game CLV. TRIAL, regrade ~Dec 8
    "0x54ce7dd357766fc3255645e55ef41edd51cc9f80": "#mde4life1986",     # CFB 91% beat / +7.8% CLV (n=57), NFL 69%; +20.9% ROI; bets ~6h pre-KO
    "0x5e6e2c3f06686f2607b86c90e35f536e81a1be00": "#drainedandpoor",   # NBA 70% beat / +1.9% CLV (n=80), +20% ROI; esports weak (blocked)
    # Discovery scan 2026-11-09 (first weekly run): only wallet of 357 to clear the bar
    "0x885ffdf360e5b0b6e6acb963fb3f8ba2344c8ed9": "#forecastication4", # NFL 82% beat / +2.5% med CLV / +24.6% ROI (n=73, since Jul 20); avg $2.6k; NFL only
}

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
DISCORD_USER_ID = "221025359884320770"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
POLL_INTERVAL = 5
EARLY_ALERT_FRAC = 0.5   # ping once a position reaches 50% of the wallet threshold, then live-edit the message as fills land
EDIT_MIN_GAP = 8         # seconds between Discord message edits per position
# Your sizing (backtest Oct 9-Nov 9: skip <1x avg, then 1u per 1x avg, cap 2u = +6.0% ROI vs +3.2% flat)
UNIT       = 1000        # 1u = to-win $1k on favorites (>50c), risk $1k on dogs
SIZE_CAP   = 2.0         # max units
# Watch-only: still graded in the weekly self-grade, but never pinged (incl. consensus)
WATCH_WALLETS = {"0x076daa87c4fe1a85402a9b6b8e0a866224388d4c"}   # #Sharp076d: watch until ~Dec 9 (30d CLV/ROI slump)
MIN_SIZE = 10000
CONSENSUS_MIN = 1000     # any tracked-wallet pre-game BUY >= this counts toward consensus
TAIL_OK_PP = 1.0         # current price within this of entry -> tailable
TAIL_MEH_PP = 2.5        # within this -> caution; beyond -> line gone
STATE_FILE = "/tmp/polyalert_state.json"
MAX_TRADE_AGE = 12 * 3600  # never alert on trades older than this (prevents re-pinging stale bets on restart)

WALLET_MIN_SIZE = {
    # Conviction thresholds ~= 80% of each wallet's average bet - only real plays ping
    "0x076daa87c4fe1a85402a9b6b8e0a866224388d4c": 6500,   # #Sharp076d (avg $8.1k)
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563": 75000,  # #Whale2c33 (conviction floor; skips market-making)
    "0x924379a79c64b77ad5816ad362122a5f6228658e": 38000,  # #KchTemp (avg $47k; his >=80% bets beat close 74%)
    "0xf0318c32136c2db7fec88b84869aee6a1106c80c": 22000,  # #BreakTheBank (avg $28k)
    "0x54ce7dd357766fc3255645e55ef41edd51cc9f80": 6300,   # #mde4life1986 (CFB/NFL avg $7.9k)
    "0x5e6e2c3f06686f2607b86c90e35f536e81a1be00": 11000,  # #drainedandpoor (NBA avg $13.7k)
    "0x885ffdf360e5b0b6e6acb963fb3f8ba2344c8ed9": 2050,   # #forecastication4 (NFL avg $2,560 -> 1u at $2,560, 2u at $5,125)
}

# Per-wallet sport block-list (event-slug prefixes). Sharp076d: soccer edge only - his tennis is
# in-play grinding with 52% beat-close pre-game (n=96) and esports is noise; never ping those.
_TENNIS  = ("atp-", "wta-")
_ESPORTS = ("lol-", "cs-", "cs2-", "val-", "dota-", "esports-", "r6-", "rl-")
# Sport-name blocks (for sports without a fixed slug prefix, e.g. soccer)
WALLET_BLOCK_SPORTS = {
    "0x924379a79c64b77ad5816ad362122a5f6228658e": ("Soccer",),   # #KchTemp: soccer -3.2% ROI on 112 bets (full on-chain history); NFL +32% / CFB +16% kept
}

# Allow-lists: if a wallet is listed here, ONLY these sports (_sport_of buckets) ever ping.
WALLET_ALLOW_SPORTS = {
    "0x54ce7dd357766fc3255645e55ef41edd51cc9f80": ("CFB", "NFL"),   # #mde4life1986: NBA 38% beat
    "0x5e6e2c3f06686f2607b86c90e35f536e81a1be00": ("NBA",),          # #drainedandpoor: esports 57% beat
    "0x885ffdf360e5b0b6e6acb963fb3f8ba2344c8ed9": ("NFL",),          # #forecastication4: soccer 56% / NBA 58% beat, CFB flat CLV
}

WALLET_BLOCK = {
    # per-sport test 2026-09-14 (per_sport_report_2026-09-14.md): block sports with no pre-game CLV edge
    "0x076daa87c4fe1a85402a9b6b8e0a866224388d4c": _TENNIS + _ESPORTS,            # #Sharp076d: soccer only (tennis 50% beat, 56% live)
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563": ("nfl-", "cfb-"),               # #Whale2c33: NFL 29% beat / -2.59% CLV, CFB 36% beat
    "0x924379a79c64b77ad5816ad362122a5f6228658e": _TENNIS + _ESPORTS + ("ufc-",),                     # #KchTemp: tennis -2.8% ROI; soccer blocked via WALLET_BLOCK_SPORTS (-3.2% ROI, n=112)
    "0xf0318c32136c2db7fec88b84869aee6a1106c80c": _TENNIS + _ESPORTS + ("nba-", "wnba-", "mlb-"),     # #BreakTheBank: NBA -10% ROI, MLB -41% ROI (n=41), tennis -11%; NFL +27%, soccer kept (+2.9%)
}

# --- Runtime state -----------------------------------------------------------

seen_hashes = {}         # tx hash -> trade ts (pruned after 48h)
watermarks = {}          # wallet -> newest processed trade ts
position_totals = {}     # (wallet, eventSlug, outcome) -> running USDC total
wallet_profiles = {}     # wallet -> profile dict (cached at startup)
game_starts = {}         # eventSlug -> game start ts (None = no game time found)
event_titles = {}        # eventSlug -> event title (e.g. "Iowa vs. Michigan") for fixture matching
consensus_book = {}      # (eventSlug, outcome) -> {wallet: total USDC}
consensus_alerted = set()  # (conditionId, outcome) already alerted
consensus_px = {}        # (conditionId, outcome) -> {wallet: sum(USDC * price)} for each wallet's avg entry
consensus_msgs = {}      # (conditionId, outcome) -> {"id", "pin", "n", "ev", "title", "outcome", "trade"}: edit as more wallets join
alert_progress = {}      # (wallet, eventSlug, outcome, side) -> USDC accumulated since last alert
alert_pxsum = {}         # same key -> sum(USDC * price) for entry VWAP
alert_msgs = {}          # same key -> {"id": discord msg id, "pin": devig dict, "last_edit": ts, "trade": first trade}
clv_log = []             # alerted BUYs pending/graded vs closing line
clv_baseline = {}        # wallet -> historical CLV stats (computed at startup, EV-style %)
wallet_cards = {}        # wallet -> {avg_bet, all_pnl, all_roi, soc_pnl, soc_roi} for alerts
sport_stats  = {}        # wallet -> {sport: {positions, cost, pnl, roi, n, avg_clv_pp, beat_close_pct}} (background-loaded)
alerted_positions = set()
alerted_markets   = {}       # (wallet, conditionId) -> {outcome, title, size}: markets we pinged (for hedge detection)
hedge_progress    = {}       # (wallet, conditionId, outcome) -> USDC bought on the opposite side pre-game
hedge_alerted     = set()    # (wallet, conditionId, outcome) already flagged  # (wallet, eventSlug, outcome, side) already pinged - prevents double pings

_thread = None
_thread_lock = threading.Lock()
_seeded = False
_monitor_owner = None    # None=undecided, True=this process runs the monitor, False=another does
_lock_pid = None         # pid that made the lock decision (fork detection)

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

# Data API v1 was retired 2026-10-24. v2 wraps rows in {"data": ..., "pagination": {...}}, pages by cursor
# and uses snake_case. _dapi() calls v2 and hands back v1-shaped (camelCase) rows so the rest of the code is unchanged.
DAPI = "https://data-api.polymarket.com/v2/"
_V1_KEYS = {"proxy_wallet": "proxyWallet", "condition_id": "conditionId", "usdc_size": "usdcSize",
            "transaction_hash": "transactionHash", "token_id": "asset", "outcome_index": "outcomeIndex",
            "event_slug": "eventSlug", "avg_price": "avgPrice", "current_price": "curPrice",
            "current_value": "currentValue", "realized_pnl": "realizedPnl", "total_pnl": "cashPnl",
            "percent_pnl": "percentPnl", "end_date": "endDate", "entry_cost_usdc": "initialValue",
            "total_size": "totalBought", "profile_image": "profileImage"}


def _v1row(x):
    if not isinstance(x, dict):
        return x
    d = dict(x)
    for k, v in _V1_KEYS.items():
        if k in x and v not in d:
            d[v] = x[k]
    return d


def _dapi(path, params, max_rows=None, timeout=15):
    """GET a Data API v2 route, follow cursors up to max_rows, return a list of v1-shaped rows ([] on failure)."""
    p0 = {k: v for k, v in params.items() if k != "offset" and v is not None}
    if "market" in p0:
        p0["condition"] = p0.pop("market")
    out, cursor, tries = [], None, 0
    while True:
        p = dict(p0)
        if cursor:
            p["cursor"] = cursor
        try:
            r = requests.get(DAPI + path, params=p, timeout=timeout)
        except Exception:
            break
        if r.status_code == 429 and tries < 3:
            tries += 1
            time.sleep(min(float(r.headers.get("Retry-After", 2) or 2), 10))
            continue
        if not r.ok:
            print(f"Data API v2 {path} -> HTTP {r.status_code}: {r.text[:120]}", flush=True)
            break
        j = r.json() or {}
        rows = j.get("data") or []
        if isinstance(rows, dict):
            return rows
        out.extend(_v1row(x) for x in rows)
        cursor = (j.get("pagination") or {}).get("next_cursor")
        if not cursor or not rows or (max_rows and len(out) >= max_rows):
            break
    return out[:max_rows] if max_rows else out


def get_recent_trades(wallet):
    return _dapi("activity", {"user": wallet, "limit": 100}, max_rows=100, timeout=8)


def get_game_start(event_slug):
    """Game start ts from gamma API (cached). None = no game time (not a game market)."""
    if event_slug in game_starts:
        return game_starts[event_slug]
    ts = None
    try:
        evs = requests.get("https://gamma-api.polymarket.com/events",
                           params={"slug": event_slug}, timeout=10).json()
        if evs:
            if evs[0].get("title"):
                event_titles[event_slug] = evs[0]["title"]
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
        raw = _dapi("activity", {"user": wallet, "limit": 1000}, max_rows=6000)
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
    if slug.startswith("cfb-"):                        return "CFB"
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
        trades = _dapi("trades", {"condition": condition_id, "limit": 200}, max_rows=200, timeout=10)
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


def _power_devig(raw):
    """Power devig: find k so that sum(p_i ** k) == 1, fair_i = p_i ** k. raw = {name: implied prob}."""
    lo, hi = 1.0, 20.0
    for _ in range(60):
        k = (lo + hi) / 2
        tot = sum(p ** k for p in raw.values())
        if tot > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return {n: p ** k for n, p in raw.items()}


def get_pinnacle_devig(event_slug, title, outcome, pm_price):
    """Fetch Pinnacle odds, devig to fair probability, compare to pm_price.
    Handles 1X2/h2h, spreads (line must match) and game totals (line must match)."""
    if not ODDS_API_KEY:
        return None
    sport_key = _detect_sport_key(event_slug)
    if not sport_key:
        return None

    title_lower = (title or "").lower()
    outcome_lower = (outcome or "").lower()
    # props Pinnacle/Odds-API don't carry -> no fair value
    if any(x in title_lower for x in ["corner", "card", "1st half", "1h ", "2nd half", "2h ", "quarter",
                                        "touchdown", "yards", "team total", "1st inning", "first inning",
                                        "player", "anytime", "to score"]):
        return None
    is_spread = title_lower.startswith("spread:")
    is_totals = (not is_spread) and any(x in title_lower for x in ["o/u", "over/under", " over ", " under "])
    market_type = "spreads" if is_spread else ("totals" if is_totals else "h2h")
    line = None
    if is_spread or is_totals:
        m = re.search(r"\(?([+-]?\d+(?:\.\d+)?)\)?\s*$", title_lower)
        if not m:
            return None
        line = abs(float(m.group(1)))

    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
            params={
                "apiKey":     ODDS_API_KEY,
                "bookmakers": "pinnacle",
                "markets":    market_type,
                "regions":    "eu",
                "oddsFormat": "american",
            }, timeout=10)
        if not r.ok:
            print(f"Odds API {r.status_code}: {r.text[:120]}", flush=True)
            return None
        games = r.json()
        if not isinstance(games, list) or not games:
            print(f"Pinnacle: no games for {sport_key}", flush=True)
            return None

        STOP = {"will", "win", "on", "vs", "vs.", "fc", "cf", "sc", "ac", "the", "and", "spread:", "o/u", "de", "1907", "1913"}
        title_words = set(title_lower.replace("-", " ").replace("?", "").split()) - STOP
        best_game, best_score = None, 0
        for game in games:
            home = (game.get("home_team") or "").lower().replace("-", " ")
            away = (game.get("away_team") or "").lower().replace("-", " ")
            score = len(title_words & (set((home + " " + away).split()) - STOP))
            if score > best_score:
                best_score, best_game = score, game
        if not best_game or best_score < 1:
            print(f"Pinnacle: no game match for '{title}' in {sport_key}", flush=True)
            return None

        pinnacle = next((b for b in best_game.get("bookmakers", []) if b["key"] == "pinnacle"), None)
        if not pinnacle:
            return None
        mkt = next((m for m in pinnacle.get("markets", []) if m["key"] == market_type), None)
        if not mkt:
            print(f"Pinnacle: no {market_type} market for {best_game.get('home_team')} v {best_game.get('away_team')}", flush=True)
            return None

        outcomes = mkt.get("outcomes", [])
        if line is not None:
            outcomes = [o for o in outcomes if o.get("point") is not None and abs(abs(float(o["point"])) - line) < 0.01]
            if len(outcomes) != 2:
                print(f"Pinnacle: line {line} not offered ({market_type}) for {best_game.get('home_team')} v {best_game.get('away_team')}", flush=True)
                return None
        if not outcomes:
            return None
        is_3way = len(outcomes) == 3

        def am_to_prob(p):
            p = float(p)
            return 100 / (p + 100) if p > 0 else abs(p) / (abs(p) + 100)

        raw_probs = {o["name"].lower(): am_to_prob(o["price"]) for o in outcomes}
        fair = _power_devig(raw_probs)

        def team_match(name):
            return len((set(name.replace("-", " ").split()) - STOP) & (set(outcome_lower.replace("-", " ").split()) - STOP))

        fair_prob = fair.get(outcome_lower)
        if fair_prob is None and outcome_lower in ("over", "under"):
            fair_prob = fair.get(outcome_lower)
        if fair_prob is None and outcome_lower not in ("yes", "no"):
            # team name on a spread / ML: best word-overlap
            best_n, best_s = None, 0
            for name, prob in fair.items():
                s = team_match(name)
                if s > best_s:
                    best_s, best_n = s, name
            if best_n is not None:
                fair_prob = fair[best_n]
        if fair_prob is None and outcome_lower in ("yes", "no"):
            # "Will X win?" -> find X among outcomes by title overlap
            best_team_prob, best_team_score = None, 0
            if "draw" in title_lower and "draw" in fair:
                fair_prob = fair["draw"] if outcome_lower == "yes" else 1.0 - fair["draw"]
            for name, prob in (fair.items() if fair_prob is None else []):
                if name == "draw":
                    continue
                score = len(title_words & (set(name.replace("-", " ").split()) - STOP))
                if score > best_team_score:
                    best_team_score, best_team_prob = score, prob
            if fair_prob is None and best_team_prob is not None and best_team_score > 0:
                fair_prob = best_team_prob if outcome_lower == "yes" else 1.0 - best_team_prob

        if fair_prob is None:
            print(f"Pinnacle: outcome '{outcome}' not matched in {list(fair)}", flush=True)
            return None

        gap = round((pm_price - fair_prob) * 100, 2)
        agrees = pm_price <= fair_prob

        if abs(gap) <= 1.5:
            edge_label = "IN-LINE"
        elif gap < -1.5:
            edge_label = f"EDGE +{abs(gap):.1f}pp below fair"
        else:
            edge_label = f"STALE {gap:+.1f}pp above fair"

        method = f"{'3way' if is_3way else '2way'}-power-devig(pinnacle {market_type}" + (f" {line:g}" if line is not None else "") + ")"
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

# --- OpticOdds (preferred) -----------------------------------------------------
# Optic gives Pinnacle 1X2 / Asian handicap / totals with exact lines across far more leagues
# than The Odds API. If OPTIC_API_KEY is set we use it; otherwise fall back to the Odds API path.

OPTIC_API_KEY = os.environ.get("OPTIC_API_KEY", "")
_optic_fx_cache = {}     # (sport, day) -> (ts, fixtures)

_OPTIC_SPORT = {"Soccer": "soccer", "MLB": "baseball", "NFL": "football", "CFB": "football",
                "NBA": "basketball", "CBB": "basketball", "NHL": "hockey", "MMA": "mma", "Tennis": "tennis"}
_OPTIC_ML     = ("Moneyline",)
_OPTIC_TOTAL  = ("Total Goals", "Total Points", "Total Runs", "Total Rounds", "Total Games", "Total Sets", "Total")
_OPTIC_SPREAD = ("Asian Handicap", "Point Spread", "Run Line", "Puck Line", "Spread", "Game Spread", "Set Spread")
_NAME_STOP = {"will", "win", "on", "vs", "vs.", "fc", "cf", "sc", "ac", "afc", "the", "and", "spread:", "o/u",
              "de", "1907", "1913", "1901", "club", "sk", "ssa", "sl", "fk", "united", "city"}

def _words(s):
    """Team-name tokens: accent-stripped, punctuation/digits removed, filler dropped."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z\s]", " ", s)
    return {w for w in s.split() if len(w) >= 3} - _NAME_STOP

def _optic_fixtures(sport, gs):
    """Fixtures for a sport within +/-3h of game start (cached 10 min)."""
    key = (sport, int(gs // 3600))
    hit = _optic_fx_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    fx = []
    try:
        for page in (1, 2, 3, 4, 5):
            r = requests.get("https://api.opticodds.com/api/v3/fixtures",
                             params={"key": OPTIC_API_KEY, "sport": sport, "page": page,
                                     "start_date_after":  datetime.fromtimestamp(gs - 3*3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     "start_date_before": datetime.fromtimestamp(gs + 3*3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                             timeout=15)
            if not r.ok:
                print(f"Optic fixtures {r.status_code}: {r.text[:120]}", flush=True)
                break
            d = r.json()
            fx.extend(d.get("data", []))
            if not d.get("has_more"):
                break
    except Exception as e:
        print(f"Optic fixtures error: {e}", flush=True)
    _optic_fx_cache[key] = (time.time(), fx)
    return fx

def get_optic_devig(event_slug, title, outcome, pm_price, gs):
    """Pinnacle fair value via OpticOdds. Same return shape as get_pinnacle_devig."""
    if not OPTIC_API_KEY or not gs:
        return None
    sport_name = _sport_of(event_slug)
    sport = _OPTIC_SPORT.get(sport_name)
    if not sport:
        return None
    title_lower = (title or "").lower()
    outcome_lower = (outcome or "").lower()
    if any(x in title_lower for x in ["corner", "card", "1st half", "1h ", "2nd half", "2h ", "quarter", "1q ", "2q ", "3q ", "4q ",
                                        "touchdown", "yards", "team total", "inning", "player", "anytime", "to score", "odd/even"]):
        return None
    is_spread = title_lower.startswith("spread:")
    is_totals = (not is_spread) and any(x in title_lower for x in ["o/u", "over/under"])
    line = None
    if is_spread or is_totals:
        m = re.search(r"\(?([+-]?\d+(?:\.\d+)?)\)?\s*$", title_lower)
        if not m:
            return None
        line = abs(float(m.group(1)))

    try:
        # 1) match fixture by team-name overlap. Use the EVENT title ("Iowa vs. Michigan") when we have it -
        #    spread/total titles only name one team, and "Michigan" alone matches four Saturday fixtures.
        tw = _words(event_titles.get(event_slug) or title_lower) | _words(title_lower)
        best, best_s, best_dt = None, 0, 1e12
        for f in _optic_fixtures(sport, gs):
            names = _words((f.get("home_team_display") or "") + " " + (f.get("away_team_display") or ""))
            s = len(tw & names)
            try:
                dt = abs(datetime.fromisoformat((f.get("start_date") or "").replace("Z", "+00:00")).timestamp() - gs)
            except Exception:
                dt = 1e12
            if s > best_s or (s == best_s and s > 0 and dt < best_dt):
                best_s, best, best_dt = s, f, dt
        if not best or best_s < 1:
            print(f"Optic: no fixture match for '{title}' ({sport})", flush=True)
            return None
        # 2) Pinnacle odds for that fixture
        r = requests.get("https://api.opticodds.com/api/v3/fixtures/odds",
                         params={"key": OPTIC_API_KEY, "sportsbook": "Pinnacle", "fixture_id": best["id"]}, timeout=15)
        if not r.ok:
            print(f"Optic odds {r.status_code}: {r.text[:120]}", flush=True)
            return None
        data = r.json().get("data") or []
        odds = data[0].get("odds", []) if data else []
        if not odds:
            print(f"Optic: no Pinnacle odds for {best.get('home_team_display')} v {best.get('away_team_display')}", flush=True)
            return None
        want = _OPTIC_SPREAD if is_spread else (_OPTIC_TOTAL if is_totals else _OPTIC_ML)
        sel = [o for o in odds if o.get("market") in want]
        if line is not None:
            sel = [o for o in sel if o.get("points") is not None and abs(abs(float(o["points"])) - line) < 0.01]
        # prefer a single market name if several match (e.g. both "Asian Handicap" and "Spread")
        if sel:
            mname = sel[0]["market"]
            sel = [o for o in sel if o["market"] == mname]
        if is_spread and sel:
            # Optic lists both -L/+L pairs; keep the pair where the title's favourite is the negative side
            fav = _words(title_lower.split("spread:", 1)[1].split("(")[0])
            neg = [o for o in sel if float(o["points"]) < 0 and len(_words(o["name"]) & fav) > 0]
            pos = [o for o in sel if float(o["points"]) > 0 and len(_words(o["name"]) & fav) == 0]
            sel = neg[:1] + pos[:1]
        if len(sel) < 2:
            print(f"Optic: {'line %g ' % line if line is not None else ''}{want[0]} not offered for {best.get('home_team_display')} v {best.get('away_team_display')}", flush=True)
            return None

        def am_to_prob(p):
            p = float(p)
            return 100 / (p + 100) if p > 0 else abs(p) / (abs(p) + 100)
        raw = {o["name"].lower(): am_to_prob(o["price"]) for o in sel}
        fair = _power_devig(raw)
        is_3way = len(fair) == 3

        fair_prob = None
        if outcome_lower in ("over", "under"):
            for k, v in fair.items():
                if k.startswith(outcome_lower):
                    fair_prob = v
        elif outcome_lower in ("yes", "no"):
            # "Will X win?" -> X's outcome. Match on the MARKET question only (the event title names both
            # teams and caused ties), and credit acronyms ("Sporting CP" -> Sporting Clube de Portugal).
            mw = _words(title_lower)
            acr = [a.lower() for a in re.findall(r"\b[A-Z]{2,4}\b", title or "")]
            bn, bs = None, 0
            if "draw" in title_lower and "draw" in fair:
                # "Will X vs Y end in a draw?" -> Yes = draw prob, No = 1 - draw
                fair_prob = fair["draw"] if outcome_lower == "yes" else 1.0 - fair["draw"]
            for k in (fair if fair_prob is None else []):
                if k == "draw":
                    continue
                kw = _words(k)
                sc = len(mw & kw)
                initials = "".join(t[0] for t in unicodedata.normalize("NFKD", k).encode("ascii", "ignore").decode().lower().split() if t not in _NAME_STOP)
                if any(a in initials for a in acr):
                    sc += 1
                if sc > bs:
                    bs, bn = sc, k
            if fair_prob is None and bn is not None and bs > 0:
                fair_prob = fair[bn] if outcome_lower == "yes" else 1.0 - fair[bn]
        else:
            ow = _words(outcome_lower)
            bn, bs = None, 0
            for k in fair:
                s = len(ow & _words(k))
                if s > bs:
                    bs, bn = s, k
            if bn is not None:
                fair_prob = fair[bn]
        if fair_prob is None:
            print(f"Optic: outcome '{outcome}' not matched in {list(fair)}", flush=True)
            return None

        gap = round((pm_price - fair_prob) * 100, 2)
        agrees = pm_price <= fair_prob
        if abs(gap) <= 1.5:
            edge_label = "IN-LINE"
        elif gap < -1.5:
            edge_label = f"EDGE +{abs(gap):.1f}pp below fair"
        else:
            edge_label = f"STALE {gap:+.1f}pp above fair"
        method = f"{'3way' if is_3way else '2way'}-power-devig(pinnacle/optic {sel[0]['market']}" + (f" {line:g}" if line is not None else "") + ")"
        return {"fair": round(fair_prob, 4), "gap": gap, "agrees": agrees, "edge_label": edge_label,
                "method": method, "home": best.get("home_team_display"), "away": best.get("away_team_display")}
    except Exception as e:
        print(f"Optic devig error: {e}", flush=True)
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
    raw = _dapi("activity", {"user": wallet, "limit": 1000, "type": "TRADE"}, max_rows=4000)
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


def _sport_of(slug):
    """Coarse sport bucket from an event slug (used for per-sport CLV / P&L on alerts)."""
    s = (slug or "").lower()
    if s.startswith("mlb-"):                                  return "MLB"
    if s.startswith(("atp-", "wta-")):                        return "Tennis"
    if s.startswith("cfb-"):                                  return "CFB"
    if s.startswith("nfl-"):                                  return "NFL"
    if s.startswith(("nba-", "wnba-")):                       return "NBA"
    if s.startswith(("cbb-", "ncaab-")):                      return "CBB"
    if s.startswith("nhl-"):                                  return "NHL"
    if s.startswith(("ufc-", "mma-", "bkfc-", "pfl-")) or "noche" in s:  return "MMA"
    if s.startswith(("lol-", "cs-", "cs2-", "val-", "dota-", "r6-", "rl-")) or "esport" in s:  return "Esports"
    if re.search(r"-\d{4}-\d{2}-\d{2}", s) or "-vs-" in s or "win-on" in s:  return "Soccer"
    return "Other"


def compute_sport_stats(wallet, per_sport=400, max_positions=9000):
    """Per-sport realized P&L/ROI over closed positions + pre-game CLV on that sport's biggest $1k+ bets."""
    closed, seen = [], set()
    for status in ("CLOSED", "REDEEMABLE"):   # v2 also exposes unredeemed winners AND losers -> no survivorship bias
        for p in _dapi("positions", {"user": wallet, "status": status, "limit": 1000,
                                     "sort_by": "TIMESTAMP", "sort_direction": "DESC"}, max_rows=max_positions):
            k = (p.get("asset"), p.get("conditionId"))
            if k not in seen:
                seen.add(k); closed.append(p)
    by = {}
    for p in closed:
        sp = _sport_of(p.get("eventSlug") or p.get("slug"))
        cost = p.get("total_cost_usdc") or (p.get("totalBought") or 0) * (p.get("avgPrice") or 0)
        pnl = p.get("total_pnl") if p.get("total_pnl") is not None else (p.get("realizedPnl") or 0)
        d = by.setdefault(sp, {"cost": 0.0, "pnl": 0.0, "n": 0, "big": []})
        d["cost"] += cost; d["pnl"] += pnl; d["n"] += 1
        if cost >= 500:
            d["big"].append((cost, p))
    out = {}
    for sp, d in by.items():
        if sp == "Other":
            continue
        clvs = []
        for cost, p in sorted(d["big"], key=lambda x: -x[0])[:per_sport]:
            slug = (p.get("eventSlug") or "").lower()
            gs = get_game_start(slug)
            if not gs:
                continue
            tr = _dapi("trades", {"user": wallet, "condition": p.get("conditionId"), "limit": 300,
                                  "taker_only": "false"}, max_rows=300)
            if not isinstance(tr, list):
                continue
            pre = [t for t in tr if isinstance(t, dict) and t.get("side") == "BUY"
                   and t.get("asset") == p.get("asset") and (t.get("timestamp") or 0) < gs]
            stake = sum((t.get("size") or 0) * (t.get("price") or 0) for t in pre)
            if stake < 500:
                continue
            vwap = sum((t.get("size") or 0) * (t.get("price") or 0) * (t.get("price") or 0) for t in pre) / stake
            if vwap <= 0.02 or vwap >= 0.98:
                continue
            close = get_closing_price(p.get("asset"), gs)
            if close is None or close <= 0.001 or close >= 0.999:
                continue
            clvs.append((close / vwap - 1) * 100)
        out[sp] = {"positions": d["n"], "cost": round(d["cost"]), "pnl": round(d["pnl"]),
                   "roi": round(d["pnl"] / d["cost"] * 100, 1) if d["cost"] > 0 else None,
                   "n": len(clvs),
                   "avg_clv_pp": round(sum(clvs) / len(clvs), 2) if clvs else None,
                   "beat_close_pct": round(sum(1 for c in clvs if c > 0) / len(clvs) * 100) if clvs else None}
    return out


def _load_sport_stats():
    """Background: per-sport stats are slow (closed-positions paging) so they fill in after the monitor starts."""
    for wallet, label in WALLETS.items():
        if wallet in sport_stats:
            continue
        try:
            sport_stats[wallet] = compute_sport_stats(wallet)
            save_state()
            summ = ", ".join(f"{k}: {v['beat_close_pct']}%/n{v['n']}" for k, v in sport_stats[wallet].items() if v.get("n"))
            print(f"Sport stats {label}: {summ}", flush=True)
        except Exception as e:
            print(f"Sport stats error {label}: {e}", flush=True)


def compute_wallet_card(wallet):
    """Startup stats for alerts: avg bet, all-time + soccer P&L/ROI (mark-to-market)."""
    SOC = ("fifwc", "epl-", "lal-", "ucl-", "sea-", "bun-", "li1-", "mls-", "uel-",
           "world-cup", "copa", "champions")
    raw = _dapi("activity", {"user": wallet, "limit": 1000}, max_rows=6000)
    cost = soc_cost = soc_proc = 0.0
    stakes = {}
    for t in raw:
        u = t.get("usdcSize", 0) or 0
        if u <= 0:
            continue
        slug = (t.get("eventSlug") or "").lower()
        typ, side = t.get("type", ""), t.get("side", "")
        soc = any(k in slug for k in SOC)
        if typ == "TRADE" and side == "BUY":
            cost += u
            if soc:
                soc_cost += u
            if u >= 1000 and (soc or slug.startswith("mlb-")):
                k = (slug, t.get("asset", "")); stakes[k] = stakes.get(k, 0) + u
        elif typ == "REDEEM" or (typ == "TRADE" and side == "SELL"):
            if soc:
                soc_proc += u
    openval = 0.0
    try:
        p = _dapi("positions", {"user": wallet, "limit": 500}, max_rows=500)
        for x in p if isinstance(p, list) else []:
            s = (x.get("eventSlug", "") or x.get("slug", "")).lower()
            if any(k in s for k in SOC):
                openval += x.get("currentValue", 0) or 0
    except Exception:
        pass
    o = get_official_pnl(wallet)
    stk = [v for v in stakes.values() if v > 0]
    soc_pnl = round(soc_proc - soc_cost + openval)
    return {
        "avg_bet": round(statistics.mean(stk)) if stk else 0,
        "all_pnl": o.get("all"),
        "all_roi": round(o["all"] / cost * 100, 1) if (o.get("all") is not None and cost > 0) else None,
        "soc_pnl": soc_pnl,
        "soc_roi": round(soc_pnl / soc_cost * 100, 1) if soc_cost > 0 else None,
    }

# --- Tail tracker / weekly self-grade / weekly discovery ----------------------
# Everything here is recomputed from the Data API (v2), so nothing is lost when Render redeploys and wipes /tmp.
REPORT_WEEKDAY  = 0      # Monday
REPORT_HOUR_UTC = 13     # ~8-9am ET: self-grade at 13:00 UTC, discovery at 14:00 UTC
TAIL_STAKE      = 1000   # to-win $1k on favorites, risk $1k on dogs
GRADE_FLAG_BEAT = 55     # flag a wallet if pre-game beat-close % falls below this (n >= GRADE_FLAG_N)
GRADE_FLAG_N    = 20
reports = {"selfgrade": None, "discovery": None, "running": set()}
report_state = {}        # {"selfgrade_day": "YYYY-MM-DD", "discovery_day": ...}


def _post_long(text):
    """Discord caps messages at 2000 chars: split on lines."""
    chunk = ""
    for ln in text.split("\n"):
        if len(chunk) + len(ln) + 1 > 1900:
            _post_discord(chunk)
            chunk = ""
        chunk += ln + "\n"
    if chunk.strip():
        _post_discord(chunk)


def _gamma_event_starts(slugs, workers=6):
    """event slug -> {"gs": first gameStartTime (what the bot uses), "m": {conditionId: gameStartTime}}"""
    def one(e):
        try:
            d = requests.get("https://gamma-api.polymarket.com/events", params={"slug": e}, timeout=15).json()
            out = {"gs": None, "m": {}}
            for mk in (d[0].get("markets", []) if d else []):
                g = mk.get("gameStartTime")
                if not g:
                    continue
                t = int(datetime.strptime(g[:19].replace(" ", "T"), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
                out["m"][mk.get("conditionId")] = t
                if out["gs"] is None:
                    out["gs"] = t
            return e, out
        except Exception:
            return e, None
    res = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for e, v in ex.map(one, list(slugs)):
            if v is not None:
                res[e] = v
    return res


def _settlements(conds, workers=4):
    """conditionId list -> {token_id: final_price (0..1) or None if unresolved}"""
    conds = sorted(set(c for c in conds if c))
    def one(cs):
        try:
            return requests.get(DAPI + "tokens", params={"condition": ",".join(cs)}, timeout=30).json().get("data") or []
        except Exception:
            return []
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(one, [conds[i:i + 10] for i in range(0, len(conds), 10)]):
            for r in rows:
                fp = r.get("final_price")
                out[r["token_id"]] = float(fp) if (r.get("resolved") and fp is not None) else None
    return out


def _closes(pairs, workers=6):
    """[(token, game_start)] -> {token: last CLOB price at/before kickoff}"""
    def one(a):
        return a[0], get_closing_price(a[0], a[1])
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, c in ex.map(one, list(pairs)):
            if c is not None:
                out[t] = c
    return out


def _tail_bet(price, final):
    """Your sizing: risk $1k on dogs (<=50c), risk-to-win $1k on favorites. Returns (risk, pnl or None)."""
    risk = TAIL_STAKE if price <= 0.5 else TAIL_STAKE * price / (1 - price)
    if final is None:
        return risk, None
    return risk, risk * (final / price - 1)


def _passes_filters(wallet, ev, title):
    if _is_futures(title, ev) or _is_prop_noise(title, ev):
        return False
    if any(ev.startswith(p) for p in WALLET_BLOCK.get(wallet, ())):
        return False
    sp = _sport_of(ev)
    if wallet in WALLET_ALLOW_SPORTS and sp not in WALLET_ALLOW_SPORTS[wallet]:
        return False
    if sp in WALLET_BLOCK_SPORTS.get(wallet, ()):
        return False
    return True


def replay_pings(days=30):
    """Re-run every roster wallet's BUY fills through the live ping rules (filters, thresholds, EARLY_ALERT_FRAC,
    consensus) and grade each ping at the price it fired: CLV vs close + settled result at TAIL_STAKE sizing."""
    start = int(time.time()) - days * 86400
    fills = {}
    for w in WALLETS:
        rows = _dapi("activity", {"user": w, "type": "TRADE", "side": "BUY", "start": start, "limit": 1000},
                     max_rows=80000, timeout=40)
        fills[w] = [t for t in rows if t.get("side") == "BUY" and _passes_filters(w, t.get("eventSlug") or "", t.get("title") or "")]
    # only events where some key could reach a ping need a game time
    need, tot = set(), {}
    for w, rows in fills.items():
        for t in rows:
            k = (w, t["eventSlug"], t.get("outcome"))
            tot[k] = tot.get(k, 0) + (t.get("usdcSize") or 0)
    for (w, ev, o), u in tot.items():
        if u >= min(EARLY_ALERT_FRAC * WALLET_MIN_SIZE.get(w, MIN_SIZE), max(CONSENSUS_MIN, 0.25 * WALLET_MIN_SIZE.get(w, MIN_SIZE))):
            need.add(ev)
    G = _gamma_event_starts(need)
    pings, cbook, cdone, events = [], {}, set(), []
    for w, rows in fills.items():
        for t in rows:
            events.append((t.get("timestamp") or 0, w, t))
    events.sort(key=lambda x: x[0])
    prog, done, pidx = {}, set(), {}
    for ts, w, t in events:
        ev = t["eventSlug"]; g = G.get(ev)
        if g is None:
            continue
        if g["gs"] and ts >= g["gs"]:
            continue
        out, usd, px = t.get("outcome"), t.get("usdcSize") or 0, t.get("price") or 0
        gs = g["m"].get(t.get("conditionId")) or g["gs"]
        base = {"ts": ts, "ev": ev, "out": out, "title": t.get("title"), "tok": t.get("asset"),
                "cond": t.get("conditionId"), "px": px, "gs": gs, "sport": _sport_of(ev)}
        k = (w, ev, f"{out}@{t.get('conditionId') or ''}")
        prog[k] = prog.get(k, 0) + usd
        if k not in done:
            if prog[k] >= EARLY_ALERT_FRAC * WALLET_MIN_SIZE.get(w, MIN_SIZE):
                done.add(k)
                pings.append(dict(base, kind="watch" if w in WATCH_WALLETS else "wallet", wallet=w,
                                  label=WALLETS[w], legs=[], placed=0.0, nextr=0.0))
                pidx[k] = pings[-1]
        if k in pidx and t.get("asset") == pidx[k]["tok"]:
            # your scale-in: re-size each time he crosses another 0.5x of his average bet
            pg = pidx[k]; r = prog[k] / avg_bet(w)
            if r + 1e-9 >= pg["nextr"]:
                tgt = units_for(w, prog[k])
                if tgt > pg["placed"] + 1e-9:
                    pg["legs"].append({"px": px, "u": tgt - pg["placed"]}); pg["placed"] = tgt
                pg["nextr"] = int(r / 0.5) * 0.5 + 0.5
        if w in WATCH_WALLETS:
            continue
        ck = (t.get("conditionId"), out)
        b = cbook.setdefault(ck, {})
        b[w] = b.get(w, 0) + usd
        q = [x for x, a in b.items() if a >= max(CONSENSUS_MIN, 0.25 * WALLET_MIN_SIZE.get(x, MIN_SIZE))]
        if len(q) >= 2 and ck not in cdone:
            cdone.add(ck)
            pings.append(dict(base, kind="consensus", wallet=",".join(q), label=" + ".join(WALLETS[x] for x in q)))
    now = time.time()
    fin = _settlements([p["cond"] for p in pings])
    cl = _closes({(p["tok"], p["gs"]) for p in pings if p["gs"] and p["gs"] < now})
    for p in pings:
        e = p["px"]
        p["close"] = cl.get(p["tok"])
        p["clv"] = (p["close"] / e - 1) * 100 if (p["close"] and 0.02 < e < 0.98) else None
        p["risk"], p["pnl"] = _tail_bet(e, fin.get(p["tok"])) if 0.02 < e < 0.98 else (0, None)
        # sized (your scale): sum of legs, each at the price when he crossed that size
        f = fin.get(p["tok"]); rs = ps = 0.0
        for lg in p.pop("legs", None) or []:
            if 0.02 < lg["px"] < 0.98:
                rk, _ = bet_dollars(lg["u"], lg["px"])
                rs += rk
                if f is not None:
                    ps += rk * (f / lg["px"] - 1)
        p["units"] = p.pop("placed", 0); p.pop("nextr", None)
        p["risk_s"], p["pnl_s"] = rs, (ps if f is not None else None)
    # consensus pings: you bet the size of the biggest wallet on it (same scale-in legs)
    by_tok = {}
    for p in pings:
        if p["kind"] == "wallet":
            by_tok.setdefault((p["tok"], p["wallet"]), p)
    for p in pings:
        if p["kind"] == "consensus":
            cands = [by_tok.get((p["tok"], w)) for w in p["wallet"].split(",")]
            cands = [c for c in cands if c and c.get("risk_s")]
            best = max(cands, key=lambda c: c["risk_s"]) if cands else None
            p["risk_s"], p["pnl_s"] = (best["risk_s"], best["pnl_s"]) if best else (0.0, None)
    return pings


def _tail_summary(rows):
    z = [x for x in rows if x.get("pnl_s") is not None and x.get("risk_s")]
    rz = sum(x["risk_s"] for x in z); pz = sum(x["pnl_s"] for x in z)
    sized = {"bets": len(z), "risked": round(rz), "pnl": round(pz), "roi": round(100 * pz / rz, 1) if rz else None}
    s = [x for x in rows if x.get("pnl") is not None]
    c = [x["clv"] for x in rows if x.get("clv") is not None]
    risk = sum(x["risk"] for x in s); pnl = sum(x["pnl"] for x in s)
    return {"pings": len(rows), "graded": len(s), "risked": round(risk), "pnl": round(pnl),
            "roi": round(100 * pnl / risk, 1) if risk else None,
            "clv": round(sum(c) / len(c), 2) if c else None,
            "clv_med": round(statistics.median(c), 2) if c else None,
            "beat": round(100 * sum(1 for y in c if y > 0) / len(c)) if c else None, "sized": sized}


def _fmt_tail(lab, s, width=16):
    if not s["graded"]:
        return f"`{lab:<{width}}` {s['pings']} pings, none settled yet"
    z = s.get("sized") or {}
    sz = (f"your size: {z['bets']} bets **${z['pnl']:+,}** ROI **{z['roi']:+.1f}%**" if z.get("roi") is not None
          else "your size: no bets (never reached avg)")
    flat = f"flat 1u: {s['graded']} · ${s['pnl']:+,} · {s['roi']:+.1f}%" if s["roi"] is not None else ""
    clv = f"CLV med {s['clv_med']:+.2f}% · beat {s['beat']}%" if s["clv"] is not None else ""
    return f"`{lab:<{width}}` {sz} \u00b7 _{flat}_ \u00b7 {clv}"


def run_selfgrade(post=True, days=30):
    """Weekly: every roster wallet, last `days` days, only the sports the bot pings for."""
    if "selfgrade" in reports["running"]:
        return reports["selfgrade"]
    reports["running"].add("selfgrade")
    try:
        t0 = time.time()
        pings = replay_pings(days)
        wp = [p for p in pings if p["kind"] == "wallet"]
        wl = [p for p in pings if p["kind"] == "watch"]
        cp = [p for p in pings if p["kind"] == "consensus"]
        per = []
        for w, label in WALLETS.items():
            mine = [p for p in (wl if w in WATCH_WALLETS else wp) if p["wallet"] == w]
            s = _tail_summary(mine)
            o = get_official_pnl(w)
            flag = (s["clv"] is not None and s["graded"] >= GRADE_FLAG_N and (s["beat"] < GRADE_FLAG_BEAT or s["clv_med"] < 0)) \
                   or (o.get("all") is not None and o["all"] < 0)
            per.append({"label": label + (" (watch)" if w in WATCH_WALLETS else ""), "wallet": w, "tail": s, "lifetime": o.get("all"), "d30": o.get("d30"), "flag": flag,
                        "sports": {sp: _tail_summary([p for p in mine if p["sport"] == sp])
                                   for sp in sorted({p["sport"] for p in mine})}})
        per.sort(key=lambda r: -(r["tail"]["pnl"] or 0))
        res = {"updated": datetime.now(timezone.utc).isoformat(), "days": days, "secs": round(time.time() - t0),
               "all": _tail_summary(wp), "consensus": _tail_summary(cp), "wallets": per,
               "pings": [{k: p.get(k) for k in ("kind", "label", "title", "out", "px", "close", "clv", "risk", "pnl", "gs", "ts", "sport")}
                         for p in sorted(pings, key=lambda p: -p["ts"])]}
        reports["selfgrade"] = res
        if post:
            L = [f"<@{DISCORD_USER_ID}>",
                 f"# \U0001f4cb Weekly self-grade — last {days} days",
                 f"_Every ping (replayed from the wallets' fills). **Your size** = skip until he's at his avg bet, then 1u per 1x avg (cap {SIZE_CAP:g}u), each add at the price when he got there; 1u = to-win ${UNIT:,} on favs / risk ${UNIT:,} on dogs. Flat = 1u at the ping price. Watch wallets aren't pinged or counted in ALL._",
                 _fmt_tail("ALL PINGS", res["all"]),
                 _fmt_tail("\U0001f6a8 CONSENSUS", res["consensus"]),
                 ""]
            for r in per:
                lt = f"lifetime ${r['lifetime']:+,} · 30d ${r['d30']:+,}" if r.get("lifetime") is not None else ""
                L.append(("⚠️ " if r["flag"] else "") + _fmt_tail(r["label"], r["tail"]) + (f" · _{lt}_" if lt else ""))
                for sp, s in r["sports"].items():
                    if s["graded"] >= 5 and len(r["sports"]) > 1 and sp != "Other":
                        c = f" · CLV med {s['clv_med']:+.2f}% · beat {s['beat']}%" if s["clv"] is not None else ""
                        z = s.get("sized") or {}
                        zs = f"your size {z['bets']} · ROI {z['roi']:+.1f}%" if z.get("roi") is not None else "your size: no bets"
                        L.append(f"   ↳ {sp}: {zs} · _flat {s['graded']} · {s['roi']:+.1f}%_{c}")
            fl = [r["label"] for r in per if r["flag"]]
            L.append("")
            L.append(f"⚠️ **Review:** {', '.join(fl)} (beat close < {GRADE_FLAG_BEAT}% or median CLV < 0 over {GRADE_FLAG_N}+ pings, or lifetime P&L < 0)" if fl
                     else "✅ No wallet below the beat-close / CLV bar this week.")
            _post_long("\n".join(L))
        return res
    except Exception as e:
        print(f"Self-grade error: {e}", flush=True)
    finally:
        reports["running"].discard("selfgrade")


def run_discovery(post=True, days=45):
    """Weekly: sports leaderboards -> settled ROI on $1k+ bets -> pre-game CLV on the survivors -> post top 5."""
    if "discovery" in reports["running"]:
        return reports["discovery"]
    reports["running"].add("discovery")
    try:
        t0 = time.time()
        roster = {w.lower() for w in WALLETS}
        cands = {}
        for tp in ("month", "week"):
            for sb in ("PNL", "VOLUME"):
                for r in _dapi("leaderboard", {"time_period": tp, "category": "sports", "sort_by": sb, "limit": 100}, max_rows=300):
                    w = (r.get("user_id") or "").lower()
                    if w and w not in roster:
                        c = cands.setdefault(w, {"name": r.get("user_name"), "pnl": 0, "vol": 0})
                        c["pnl"] = max(c["pnl"], r.get("pnl") or 0); c["vol"] = max(c["vol"], r.get("volume") or 0)
        cands = {w: c for w, c in cands.items() if c["pnl"] > 0 and c["vol"] >= 300000}
        start = int(time.time()) - days * 86400

        def pull(w):
            rows = _dapi("activity", {"user": w, "type": "TRADE", "start": start, "limit": 1000}, max_rows=5000, timeout=30)
            agg, sells, buys = {}, 0.0, 0.0
            for t in rows:
                u = t.get("usdcSize") or 0
                if t.get("side") != "BUY":
                    sells += u; continue
                buys += u
                a = agg.setdefault(t.get("asset"), {"usd": 0, "sh": 0, "last": 0, "ev": t.get("eventSlug") or "", "cond": t.get("conditionId")})
                a["usd"] += u; a["sh"] += t.get("size") or 0; a["last"] = max(a["last"], t.get("timestamp") or 0)
            return w, {"pos": {k: v for k, v in agg.items() if v["usd"] >= 1000 and v["sh"] > 0 and _sport_of(v["ev"]) != "Other"},
                       "sell_ratio": sells / buys if buys else 0}
        data = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            for w, v in ex.map(pull, list(cands)):
                data[w] = v
        fin = _settlements([p["cond"] for v in data.values() for p in v["pos"].values()])
        stage1 = []
        for w, v in data.items():
            st = [(t, p) for t, p in v["pos"].items() if fin.get(t) is not None]
            if len(st) < 30 or v["sell_ratio"] >= 0.25:
                continue
            cost = sum(p["usd"] for _, p in st); pnl = sum(p["sh"] * fin[t] - p["usd"] for t, p in st)
            hi = sum(1 for _, p in st if p["usd"] / p["sh"] >= 0.85) / len(st)
            if cost / len(st) < 2500 or pnl / cost < 0.03 or hi >= 0.25:
                continue
            stage1.append((pnl / cost * min(1, len(st) / 100), w, len(st), cost, pnl))
        stage1 = sorted(stage1, reverse=True)[:20]
        top = [w for _, w, *_ in stage1]
        G = _gamma_event_starts({p["ev"] for w in top for p in data[w]["pos"].values()})
        pairs = set()
        for w in top:
            for t, p in data[w]["pos"].items():
                gs = G.get(p["ev"], {}).get("m", {}).get(p["cond"])
                p["gs"] = gs
                if gs and p["last"] < gs < time.time():
                    pairs.add((t, gs))
        cl = _closes(pairs)
        out = []
        for _, w, n_set, cost, pnl in stage1:
            pre, by = [], {}
            for t, p in data[w]["pos"].items():
                e = p["usd"] / p["sh"]
                if not p.get("gs") or p["last"] >= p["gs"] or t not in cl or not 0.03 < e < 0.97:
                    continue
                f = fin.get(t)
                x = {"clv": (cl[t] / e - 1) * 100, "usd": p["usd"], "pnl": (p["sh"] * f - p["usd"]) if f is not None else None,
                     "mins": (p["gs"] - p["last"]) / 60, "sp": _sport_of(p["ev"])}
                pre.append(x); by.setdefault(x["sp"], []).append(x)
            if len(pre) < 30:
                continue
            beat = 100 * sum(1 for x in pre if x["clv"] > 0) / len(pre)
            clv = sum(x["clv"] for x in pre) / len(pre)
            if beat < 62 or clv <= 0:
                continue
            # lifetime check (2026-11-10): must be profitable all-time and >= 90 days old; flag if the last 45d carries the account
            try:
                pts = (requests.get(DAPI + "user-pnl", params={"user": w, "interval": "all", "fidelity": "1d"}, timeout=20).json().get("data") or {}).get("points") or []
            except Exception:
                pts = []
            if not pts:
                continue
            life = pts[-1].get("position_pnl") or 0
            age = (time.time() - pts[0]["timestamp"]) / 86400
            before = [p.get("position_pnl") or 0 for p in pts if p["timestamp"] <= time.time() - 45 * 86400]
            prior = before[-1] if before else 0
            if life <= 0 or age < 90:
                continue
            hot = prior < 0 or (life - prior) > 0.75 * life
            sports = []
            for sp, xs in sorted(by.items(), key=lambda kv: -len(kv[1])):
                if len(xs) >= 10:
                    sb = 100 * sum(1 for x in xs if x["clv"] > 0) / len(xs)
                    sv = [x for x in xs if x["pnl"] is not None]
                    sr = 100 * sum(x["pnl"] for x in sv) / max(1, sum(x["usd"] for x in sv))
                    sports.append(f"{sp} {len(xs)}: {sb:.0f}% beat / {sum(x['clv'] for x in xs)/len(xs):+.1f}% CLV / {sr:+.0f}% ROI")
            out.append({"wallet": w, "name": cands[w]["name"], "n": len(pre), "beat": round(beat), "clv": round(clv, 2),
                        "life": round(life), "prior45": round(prior), "age": round(age), "hot": hot,
                        "roi": round(100 * pnl / cost, 1), "avg": round(cost / n_set),
                        "mins": round(statistics.median(x["mins"] for x in pre)), "sports": sports})
        out.sort(key=lambda r: (-r["beat"], -r["clv"]))
        res = {"updated": datetime.now(timezone.utc).isoformat(), "scanned": len(cands), "stage1": len(stage1),
               "secs": round(time.time() - t0), "candidates": out[:10]}
        reports["discovery"] = res
        if post:
            L = [f"<@{DISCORD_USER_ID}>",
                 f"# \U0001f50e Weekly discovery scan",
                 f"_{len(cands)} profitable sports-leaderboard wallets → {len(stage1)} passed settled ROI ({days}d) → graded on pre-game CLV. Bar: 62%+ beat close, CLV > 0, 30+ pre-game bets, profitable lifetime, account 90+ days old._"]
            if not out:
                L.append("No new wallet cleared the bar this week.")
            for r in out[:5]:
                L.append(f"\n**{r['name'] or r['wallet'][:10]}** — <https://polymarket.com/profile/{r['wallet']}>")
                L.append(f"{r['n']} pre-game bets · beat close **{r['beat']}%** · CLV **{r['clv']:+.2f}%** · ROI **{r['roi']:+.1f}%** · avg ${r['avg']:,} · bets ~{r['mins']} min pre-KO")
                L.append(f"   lifetime P&L **${r['life']:+,}** (was ${r['prior45']:+,} 45 days ago) · account {r['age']} days old"
                         + ("  ⚠️ **mostly made in the last 45 days — hot-streak risk, check per-sport lifetime before adding**" if r["hot"] else ""))
                for s in r["sports"][:4]:
                    L.append(f"   ↳ {s}")
            L.append("\n_Reply in chat with who to add (and which sports) and it gets deployed._")
            _post_long("\n".join(L))
        return res
    except Exception as e:
        print(f"Discovery error: {e}", flush=True)
    finally:
        reports["running"].discard("discovery")


def _report_scheduler():
    """Mondays: self-grade at REPORT_HOUR_UTC, discovery an hour later. Checked every 10 min."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            day = now.strftime("%Y-%m-%d")
            if now.weekday() == REPORT_WEEKDAY:
                if now.hour >= REPORT_HOUR_UTC and report_state.get("selfgrade_day") != day:
                    report_state["selfgrade_day"] = day; save_state()
                    run_selfgrade(post=True)
                if now.hour >= REPORT_HOUR_UTC + 1 and report_state.get("discovery_day") != day:
                    report_state["discovery_day"] = day; save_state()
                    run_discovery(post=True)
        except Exception as e:
            print(f"Report scheduler error: {e}", flush=True)
        time.sleep(600)


# --- State persistence -------------------------------------------------------

def save_state():
    try:
        with open(STATE_FILE, "w") as fh:
            json.dump({
                "watermarks": watermarks,
                "consensus_alerted": ["|".join(k) for k in consensus_alerted],
                "clv_log": clv_log[-2000:],
                "clv_baseline": clv_baseline,
                "wallet_cards": wallet_cards,
                "sport_stats": sport_stats,
                "alerted_positions": ["|".join(k) for k in alerted_positions],
                "report_state": report_state,
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
        wallet_cards.update(d.get("wallet_cards", {}))
        sport_stats.update(d.get("sport_stats", {}))
        report_state.update(d.get("report_state", {}))
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
    """Post to the webhook; returns the Discord message id (so we can edit it later) or None."""
    try:
        r = requests.post(DISCORD_WEBHOOK, params={"wait": "true"}, json={
            "content": content,
            "allowed_mentions": {"users": [DISCORD_USER_ID]}
        }, timeout=5)
        if r.ok:
            return (r.json() or {}).get("id")
    except Exception:
        pass
    return None


def _edit_discord(msg_id, content):
    try:
        requests.patch(f"{DISCORD_WEBHOOK}/messages/{msg_id}", json={
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


def build_alert_content(trade, label, wallet, gs, accumulated, price, pin, threshold=None):
    side       = trade.get("side", "")
    event_slug = trade.get("eventSlug", "")
    outcome    = trade.get("outcome", "")
    title      = trade.get("title", "")
    total = position_totals.get((wallet, event_slug, f"{outcome}@{trade.get('conditionId') or ''}"), accumulated)
    profile = wallet_profiles.get(wallet, {})
    my_clv  = clv_baseline.get(wallet) or clv_stats(wallet).get(wallet)

    emoji  = "\U0001f7e2" if side == "BUY" else "\U0001f534"
    action = "NEW BET" if side == "BUY" else "CLOSED POSITION"
    filling = "  \u23f3 *still filling*" if (threshold and accumulated < threshold) else ""

    bet_line = f"He bet **${accumulated:,.0f}**  ({accumulated / avg_bet(wallet):.1f}x his avg bet of ${avg_bet(wallet):,.0f}){filling}"
    if total and abs(total) > accumulated * 1.05:
        bet_line += f"  |  position **${total:,.0f}**"

    lines = [
        f"<@{DISCORD_USER_ID}>",
        f"{emoji} **{action} -- {label}** | PRE-GAME ({_countdown(gs)})",
        f"**{title}**",
        f"{side} **{outcome}** @ **{round(price*100,1)}c**  ({price_to_american(price)})",
        bet_line,
    ]
    if side == "BUY":
        lines.append(sizing_line(wallet, accumulated, price))

    if pin:
        plus_ev = price <= pin["fair"]
        lines.append(f"\U0001f3af Pinnacle fair **{round(pin['fair']*100,1)}c**  ·  paid **{round(price*100,1)}c**  ·  "
                     f"+EV: **{'Yes' if plus_ev else 'No'}** ({(pin['fair'] - price)*100:+.1f}c)")
    else:
        lines.append("\U0001f3af Pinnacle fair: n/a")

    card  = wallet_cards.get(wallet, {})
    sport = _sport_of(event_slug)
    ss    = (sport_stats.get(wallet) or {}).get(sport) or {}
    if ss.get("n"):
        lines.append(f"\U0001f4c8 **{sport} EV {ss['avg_clv_pp']:+.2f}%** avg  ·  beats close **{ss['beat_close_pct']}%**  ·  n={ss['n']}")
    elif my_clv:
        lines.append(f"\U0001f4c8 **EV {my_clv['avg_clv_pp']:+.2f}%** avg (all sports)  ·  beats close **{my_clv['beat_close_pct']}%**  ·  n={my_clv['n']}")

    stat = []
    allp = card.get("all_pnl"); allr = card.get("all_roi")
    if allp is not None:
        stat.append(f"All-time {'▲' if allp >= 0 else '▼'} **${abs(allp):,}**" + (f" ({allr:+g}% ROI)" if allr is not None else ""))
    if ss.get("pnl") is not None:
        stat.append(f"{sport} {'▲' if ss['pnl'] >= 0 else '▼'} **${abs(ss['pnl']):,}**" + (f" ({ss['roi']:+g}% ROI)" if ss.get("roi") is not None else ""))
    elif sport == "Soccer" and card.get("soc_pnl") is not None:
        socp = card["soc_pnl"]; socr = card.get("soc_roi")
        stat.append(f"Soccer {'▲' if socp >= 0 else '▼'} **${abs(socp):,}**" + (f" ({socr:+g}% ROI)" if socr is not None else ""))
    if stat:
        lines.append("\U0001f4b0 " + "  ·  ".join(stat))

    lines.append(f"<https://polymarket.com/@{wallet}>")
    return "\n".join(lines)


def send_discord_alert(trade, label, wallet, gs, accumulated=None, entry_price=None, pkey=None, threshold=None):
    side       = trade.get("side", "")
    event_slug = trade.get("eventSlug", "")
    outcome    = trade.get("outcome", "")
    title      = trade.get("title", "")
    fill_size  = trade.get("usdcSize", 0)
    price      = entry_price or trade.get("price", 0)      # what THEY paid (VWAP of the fills so far)
    asset      = trade.get("asset", "")
    if accumulated is None:
        accumulated = fill_size

    # Fair value vs the price they paid (power devig of Pinnacle via Optic, Odds API fallback)
    pin = get_optic_devig(event_slug, title, outcome, price, gs) or get_pinnacle_devig(event_slug, title, outcome, price)

    msg_id = _post_discord(build_alert_content(trade, label, wallet, gs, accumulated, price, pin, threshold))
    if pkey is not None:
        alert_msgs[pkey] = {"id": msg_id, "pin": pin, "last_edit": time.time(), "trade": trade}

    if side == "BUY" and asset and gs:
        clv_log.append({"wallet": wallet, "label": label, "slug": event_slug,
                        "asset": asset, "entry": price, "gs": gs,
                        "ts": trade.get("timestamp", 0), "clv": None})


def send_hedge_alert(label, wallet, title, orig, new_outcome, new_size, price, gs):
    """Follow-up when a pinged wallet buys the other side of the same market pre-game."""
    osz = orig.get("size") or 0
    net = (osz - new_size) / osz * 100 if osz else 0
    if net > 50:
        verdict = f"still leaning **{orig['outcome']}** (net {net:.0f}% of original)"
    elif net > 0:
        verdict = f"mostly hedged (net {net:.0f}% left on {orig['outcome']})"
    else:
        verdict = f"**flipped** - now bigger on {new_outcome}"
    lines = [
        f"<@{DISCORD_USER_ID}>",
        f"\u26a0\ufe0f **HEDGE -- {label}** | PRE-GAME ({_countdown(gs)})",
        f"**{title}**",
        f"Pinged **{orig['outcome']}** ${osz:,.0f} -> now bought **{new_outcome}** ${new_size:,.0f} @ {round(price*100,1)}c",
        verdict,
        f"<https://polymarket.com/@{wallet}>",
    ]
    _post_discord("\n".join(lines))


def build_consensus_content(event_slug, outcome, title, book, ckey, pin):
    gs = game_starts.get(event_slug)
    pxb = consensus_px.get(ckey, {})
    asset = (consensus_msgs.get(ckey) or {}).get("asset")
    now_price = get_current_ask(asset) if asset else None
    sport = _sport_of(event_slug)
    total = sum(book.values())
    names = " + ".join(WALLETS.get(w, w[:8]) for w in sorted(book, key=lambda x: -book[x]))
    lines = [
        f"<@{DISCORD_USER_ID}>",
        f"# \U0001f6a8 CONSENSUS — {len(book)} SHARPS ON IT",
        f"**{names}**",
        f"**{title}**  ·  PRE-GAME ({_countdown(gs)})",
        f"## BUY {outcome}",
    ]
    for w in sorted(book, key=lambda x: -book[x]):
        avg = pxb.get(w, 0) / book[w] if book[w] else 0
        ss = (sport_stats.get(w) or {}).get(sport) or {}
        rec = (f"  ·  {sport} {ss['beat_close_pct']}% beat / {ss['avg_clv_pp']:+.2f}% CLV (n={ss['n']})"
               if ss.get("n") and ss.get("beat_close_pct") is not None else "")
        lines.append(f"• **{WALLETS.get(w, w[:8])}** ${book[w]:,.0f} @ {avg * 100:.1f}c{rec}")
    lines.append(f"Combined **${total:,.0f}**")
    lead = max(book, key=lambda w: units_for(w, book[w]))
    bp = now_price if now_price is not None else ((pxb.get(lead, 0) / book[lead]) if book.get(lead) else 0)
    if units_for(lead, book[lead]) > 0 and bp:
        lines.append(sizing_line(lead, book[lead], bp).replace("he's at", f"{WALLETS.get(lead, lead[:8])} is at"))
    else:
        lines.append("\U0001f4b5 **Your bet: WAIT** — no wallet is at its average bet yet; you'll get a BET NOW ping from whoever gets there first.")
    ref = now_price
    if pin:
        if ref is None:
            ref = (sum(pxb.values()) / sum(book[w] for w in pxb)) if pxb else None
        ev_txt = ""
        if ref is not None:
            ev_txt = f"  ·  now **{ref * 100:.1f}c** ({price_to_american(ref)})  ·  +EV: **{'Yes' if ref <= pin['fair'] else 'No'}** ({(pin['fair'] - ref) * 100:+.1f}c)"
        lines.append(f"\U0001f3af Pinnacle fair **{pin['fair'] * 100:.1f}c** (power devig){ev_txt}")
    else:
        lines.append("\U0001f3af Pinnacle fair: n/a" + (f"  ·  now **{ref * 100:.1f}c** ({price_to_american(ref)})" if ref is not None else ""))
    return "\n".join(lines)


def send_consensus_alert(event_slug, outcome, title, book, trade, ckey=None):
    ckey = ckey or (trade.get("conditionId") or event_slug, outcome)
    gs = game_starts.get(event_slug)
    asset = trade.get("asset", "")
    consensus_msgs[ckey] = {"asset": asset, "n": len(book), "last_edit": time.time()}
    now_price = get_current_ask(asset) if asset else None
    ref = now_price if now_price is not None else trade.get("price", 0)
    pin = get_optic_devig(event_slug, title, outcome, ref, gs) or get_pinnacle_devig(event_slug, title, outcome, ref)
    consensus_msgs[ckey]["pin"] = pin
    consensus_msgs[ckey]["id"] = _post_discord(build_consensus_content(event_slug, outcome, title, book, ckey, pin))

# --- Trade handling ----------------------------------------------------------

def avg_bet(wallet):
    """Average conviction bet: thresholds are set at 80% of each wallet's average bet."""
    return WALLET_MIN_SIZE.get(wallet, MIN_SIZE) / 0.8


def units_for(wallet, size):
    """Skip until he's at his average bet, then 1u per 1x average, capped at SIZE_CAP (rounded to 0.1u)."""
    r = size / avg_bet(wallet)
    return 0.0 if r < 1 else round(min(r, SIZE_CAP), 1)


def bet_dollars(units, price):
    """(risk, to_win) for `units` at `price`: favorites -> to-win units*UNIT; dogs -> risk units*UNIT."""
    if not 0 < price < 1 or units <= 0:
        return 0, 0
    if price > 0.5:
        win = units * UNIT
        return win * price / (1 - price), win
    risk = units * UNIT
    return risk, risk * (1 - price) / price


def sizing_line(wallet, size, price):
    a = avg_bet(wallet); r = size / a; u = units_for(wallet, size)
    if u == 0:
        return (f"\U0001f4b5 **Your bet: WAIT** — he's at {r:.1f}x his avg (${size:,.0f} of ${a:,.0f}). "
                f"You'll get a BET NOW ping when he hits ${a:,.0f}.")
    risk, win = bet_dollars(u, price)
    tail = "  ·  **max size**" if u >= SIZE_CAP else f"  ·  goes to {SIZE_CAP:g}u if he reaches ${SIZE_CAP * a:,.0f}"
    return (f"\U0001f4b5 **Your bet: {u:.1f}u → risk ${risk:,.0f} to win ${win:,.0f}** @ {price * 100:.1f}c"
            f"  ·  he's at {r:.1f}x his ${a:,.0f} avg{tail}")


alert_tier = {}          # pkey -> 0 (wait) / 1 (betting) / 2 (max): BET NOW / MAX pings fire when this goes up
alert_units = {}         # pkey -> units you were told to bet so far (so ADD pings say how much MORE)


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
    if any(event_slug.startswith(p) for p in WALLET_BLOCK.get(wallet, ())):
        return
    if wallet in WALLET_ALLOW_SPORTS and _sport_of(event_slug) not in WALLET_ALLOW_SPORTS[wallet]:
        return
    if _sport_of(event_slug) in WALLET_BLOCK_SPORTS.get(wallet, ()):
        return
    if wallet in WATCH_WALLETS:
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

    # Positions are tracked per MARKET: "Suns" on the moneyline and "Suns +8.5" are different bets (bug fix 2026-11-09:
    # they used to be summed under one event-level key and shown under whichever market's title came first).
    mk = f"{outcome}@{trade.get('conditionId') or ''}"
    key = (wallet, event_slug, mk)
    position_totals[key] = position_totals.get(key, 0) + (fill_size if side == "BUY" else -fill_size)

    # Consensus: keyed by MARKET (conditionId), not event - "Eagles" on the ML and "Eagles" on a spread are
    # different bets. A wallet only counts once it has >= 25% of its own conviction threshold on that market.
    ckey = (trade.get("conditionId") or event_slug, outcome)
    if side == "BUY":
        book = consensus_book.setdefault(ckey, {})
        book[wallet] = book.get(wallet, 0) + fill_size
        pxb = consensus_px.setdefault(ckey, {})
        pxb[wallet] = pxb.get(wallet, 0) + fill_size * (trade.get("price") or 0)
        qualified = {w: amt for w, amt in book.items()
                     if amt >= max(CONSENSUS_MIN, 0.25 * WALLET_MIN_SIZE.get(w, MIN_SIZE))}
        if len(qualified) >= 2 and ckey not in consensus_alerted:
            consensus_alerted.add(ckey)
            send_consensus_alert(event_slug, outcome, title, qualified, trade, ckey)
        elif len(qualified) >= 2 and ckey in consensus_msgs:
            m = consensus_msgs[ckey]
            if len(qualified) > m["n"] or time.time() - m.get("last_edit", 0) >= 60:
                m["n"] = len(qualified); m["last_edit"] = time.time()
                if m.get("id"):
                    _edit_discord(m["id"], build_consensus_content(event_slug, outcome, title, qualified, ckey, m.get("pin")))

    # Hedge detection: wallet buys the OPPOSITE side of a market we already pinged -> follow-up warning
    cid = trade.get("conditionId", "")
    orig = alerted_markets.get((wallet, cid))
    if side == "BUY" and orig and orig["outcome"] != outcome:
        hk = (wallet, cid, outcome)
        if hk not in hedge_alerted:
            hedge_progress[hk] = hedge_progress.get(hk, 0) + fill_size
            if hedge_progress[hk] >= max(1000, 0.25 * WALLET_MIN_SIZE.get(wallet, MIN_SIZE)):
                hedge_alerted.add(hk)
                send_hedge_alert(label, wallet, title, orig, outcome, hedge_progress[hk], trade.get("price", 0), gs)
        return

    pkey = (wallet, event_slug, mk, side)
    alert_progress[pkey] = alert_progress.get(pkey, 0) + fill_size
    alert_pxsum[pkey] = alert_pxsum.get(pkey, 0) + fill_size * (trade.get("price") or 0)
    accumulated = alert_progress[pkey]
    entry_vwap = alert_pxsum[pkey] / accumulated if accumulated else trade.get("price", 0)
    threshold = WALLET_MIN_SIZE.get(wallet, MIN_SIZE)

    if pkey in alerted_positions:
        # Already pinged: keep the ORIGINAL message current (size / VWAP / +EV) instead of pinging again,
        # except a short BET NOW / MAX SIZE ping when he crosses 1x / 2x his average bet.
        m = alert_msgs.get(pkey)
        u = units_for(wallet, accumulated) if side == "BUY" else 0
        tier = 0 if u == 0 else (2 if u >= SIZE_CAP else 1)
        if tier > alert_tier.get(pkey, 0):
            alert_tier[pkey] = tier
            px_now = trade.get("price") or entry_vwap
            prev = alert_units.get(pkey, 0.0)
            alert_units[pkey] = u
            if prev > 0:
                add = round(u - prev, 1); rk, wn = bet_dollars(add, px_now)
                body = (f"\U0001f4b5 **Add {add:.1f}u → risk ${rk:,.0f} to win ${wn:,.0f}** @ {px_now * 100:.1f}c"
                        f"  ·  total now {u:.1f}u (max)  ·  he's at {accumulated / avg_bet(wallet):.1f}x his ${avg_bet(wallet):,.0f} avg")
            else:
                body = sizing_line(wallet, accumulated, px_now)
            head = "\U0001f7e2 **BET NOW**" if prev == 0 else "\u2b06\ufe0f **ADD TO MAX**"
            _post_discord(f"<@{DISCORD_USER_ID}> {head} — {label} · **{outcome}** · {title}\n" + body)
            if m:
                m["last_edit"] = 0
        if m and m.get("id") and time.time() - m.get("last_edit", 0) >= EDIT_MIN_GAP:
            m["last_edit"] = time.time()
            if side == "BUY" and cid:
                alerted_markets[(wallet, cid)] = {"outcome": outcome, "title": title, "size": accumulated}
            _edit_discord(m["id"], build_alert_content(m["trade"], label, wallet, gs, accumulated, entry_vwap, m.get("pin"), threshold))
        return
    if accumulated < EARLY_ALERT_FRAC * threshold:
        return
    alerted_positions.add(pkey)
    u0 = units_for(wallet, accumulated) if side == "BUY" else 0
    alert_tier[pkey] = 0 if u0 == 0 else (2 if u0 >= SIZE_CAP else 1)
    alert_units[pkey] = u0
    if side == "BUY" and cid:
        alerted_markets[(wallet, cid)] = {"outcome": outcome, "title": title, "size": accumulated}
    send_discord_alert(trade, label, wallet, gs, accumulated, entry_vwap, pkey=pkey, threshold=threshold)

# --- Monitor loop ------------------------------------------------------------

def seed_open_positions():
    """Rebuild in-memory position / consensus state from each wallet's OPEN positions so a restart or redeploy doesn't
    forget who is already on what (bug fix 2026-11-09: a 17:07Z redeploy wiped KchTemp's Over 44.5, so Bc43's later bet
    on the same market never triggered consensus). Positions already past the ping size are marked as pinged (silent)."""
    now = time.time(); n_pos = n_pinged = 0
    cands = []

    def pull(w):
        return w, _dapi("positions", {"user": w, "limit": 500}, max_rows=1500)
    with ThreadPoolExecutor(max_workers=6) as ex:
        pulled = list(ex.map(pull, [w for w in WALLETS if w not in WATCH_WALLETS]))
    for w, rows in pulled:
        floor = min(1000, 0.25 * WALLET_MIN_SIZE.get(w, MIN_SIZE))
        for p in rows:
            ev, title, out = p.get("eventSlug") or "", p.get("title") or "", p.get("outcome") or ""
            cost = (p.get("totalBought") or 0) * (p.get("avgPrice") or 0)
            if ev and out and cost >= floor and _passes_filters(w, ev, title):
                cands.append((w, p, cost))
    need = {p.get("eventSlug") for _, p, _ in cands if p.get("eventSlug") not in game_starts}
    for ev, g in _gamma_event_starts(need, workers=8).items():
        game_starts[ev] = g["gs"]
    for w, p, cost in cands:
        thr = WALLET_MIN_SIZE.get(w, MIN_SIZE)
        ev, title, out, cid = p.get("eventSlug") or "", p.get("title") or "", p.get("outcome") or "", p.get("conditionId") or ""
        gs = game_starts.get(ev)
        if not gs or gs <= now:
            continue
        avg = p.get("avgPrice") or 0
        mk = f"{out}@{cid}"
        position_totals[(w, ev, mk)] = cost
        ck = (cid or ev, out)
        consensus_book.setdefault(ck, {})[w] = cost
        consensus_px.setdefault(ck, {})[w] = cost * avg
        pkey = (w, ev, mk, "BUY")
        alert_progress[pkey] = cost; alert_pxsum[pkey] = cost * avg
        n_pos += 1
        if cost >= EARLY_ALERT_FRAC * thr:
            alerted_positions.add(pkey); n_pinged += 1
            u = units_for(w, cost)
            alert_tier[pkey] = 0 if u == 0 else (2 if u >= SIZE_CAP else 1)
            alert_units[pkey] = u
            if cid:
                alerted_markets[(w, cid)] = {"outcome": out, "title": title, "size": cost}
    for ck, book in consensus_book.items():
        q = [w for w, a in book.items() if a >= max(CONSENSUS_MIN, 0.25 * WALLET_MIN_SIZE.get(w, MIN_SIZE))]
        if len(q) >= 2:
            consensus_alerted.add(ck)   # already pinged before the restart
    print(f"Seeded {n_pos} open pre-game positions ({n_pinged} already past ping size) across {len(WALLETS)} wallets", flush=True)


def monitor_loop():
    print(f"Monitor thread running in pid={os.getpid()}", flush=True)
    try:
        seed_open_positions()
    except Exception as e:
        print(f"Seed open positions error: {e}", flush=True)
    for wallet, label in WALLETS.items():
        try:
            if wallet not in clv_baseline:
                base = compute_historical_clv(wallet)
                if base:
                    clv_baseline[wallet] = base
                    print(f"CLV baseline {label}: {base['avg_clv_pp']:+.2f}% avg, "
                          f"beats {base['beat_close_pct']}% (n={base['n']})", flush=True)
            if wallet not in wallet_cards:
                wallet_cards[wallet] = compute_wallet_card(wallet)
                c = wallet_cards[wallet]
                print(f"Card {label}: avg ${c['avg_bet']:,}, all ${c['all_pnl']}, soc ${c['soc_pnl']}", flush=True)
        except Exception as e:
            print(f"Card/CLV error {label}: {e}", flush=True)
    save_state()
    threading.Thread(target=_load_sport_stats, daemon=True).start()
    threading.Thread(target=_report_scheduler, daemon=True).start()
    cycles = 0
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            # Fetch all wallets in parallel (one round-trip instead of 14 sequential ones), then process in order.
            with ThreadPoolExecutor(max_workers=len(WALLETS) or 1) as ex:
                fetched = list(ex.map(get_recent_trades, list(WALLETS.keys())))
            for (wallet, label), trades in zip(WALLETS.items(), fetched):
                try:
                    for trade in sorted(trades, key=lambda t: t.get("timestamp", 0)):
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
                if len(consensus_book) > 3000:
                    consensus_book.clear()
                for k in list(alert_progress):
                    g = game_starts.get(k[1])
                    if g and time.time() > g:
                        del alert_progress[k]
                        alert_pxsum.pop(k, None)
                        alert_msgs.pop(k, None)
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
    global _thread, _seeded, _monitor_owner, _lock_pid
    with _thread_lock:
        # Fork safety: gunicorn forks workers from the master AFTER import, so a child inherits
        # _monitor_owner=True without actually holding its own lock. Re-decide in every new pid.
        if _monitor_owner is not None and _lock_pid != os.getpid():
            _monitor_owner, _thread = None, None
        if _monitor_owner is None:
            _lock_pid = os.getpid()
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


# NOTE: do NOT call ensure_monitor() at import time - under gunicorn the master imports this module and
# would start a monitor thread of its own alongside the worker's (double alerts). Routes call it instead.



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
  const s=n<0?'neg':'pos'; return '<span class="'+s+'">'+(n<0?'-':'')+'$'+Math.abs(n).toLocaleString()+'</span>'; }
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
          '<td>$'+ (b.value||0).toLocaleString()+'</td>'+
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
        r = requests.get(DAPI + "user-pnl", params={"user": wallet, "interval": "all", "fidelity": "1d"}, timeout=10)
        pts = (((r.json() or {}).get("data") or {}).get("points") or []) if r.ok else []
        v = [x.get("position_pnl") if x.get("position_pnl") is not None else (x.get("realized_pnl") or 0) + (x.get("unrealized_pnl") or 0)
             for x in pts]
        if v:
            out = {"all": round(v[-1]), "d30": round(v[-1] - v[-31]) if len(v) > 31 else round(v[-1])}
    except Exception:
        pass
    _official_pnl_cache[wallet] = (now, out)
    return out


def get_open_positions(wallet):
    try:
        d = _dapi("positions", {"user": wallet, "sort_by": "CURRENT_VALUE", "sort_direction": "DESC", "limit": 500},
                  max_rows=500)
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
            "sports": sport_stats.get(wallet),
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
        "instance":        os.environ.get("RENDER_INSTANCE_ID", ""),
        "monitor_owner":   _monitor_owner,
        "pinnacle":        bool(ODDS_API_KEY or OPTIC_API_KEY),
        "optic":           bool(OPTIC_API_KEY),
        "profiles_loaded": len(wallet_profiles),
        "clv_logged":      len(clv_log),
        "clv_graded":      sum(1 for e in clv_log if e.get("clv") is not None),
    })


@app.route("/devig")
def devig_test():
    """Debug: /devig?slug=nfl-det-buf-2026-09-18&title=Spread: Bills (-3.5)&outcome=Bills&price=0.56"""
    from flask import request
    slug = request.args.get("slug", ""); title = request.args.get("title", ""); outcome = request.args.get("outcome", "")
    try:
        price = float(request.args.get("price", "0.5"))
    except ValueError:
        price = 0.5
    gs = get_game_start(slug)
    return jsonify({"slug": slug, "title": title, "outcome": outcome, "price": price, "game_start": gs,
                    "optic_key_set": bool(OPTIC_API_KEY), "odds_key_set": bool(ODDS_API_KEY),
                    "optic": get_optic_devig(slug, title, outcome, price, gs),
                    "odds_api": get_pinnacle_devig(slug, title, outcome, price)})


@app.route("/report/selfgrade")
def report_selfgrade():
    """Last weekly self-grade. ?run=1 starts a fresh one in the background (?post=1 also posts it to Discord)."""
    from flask import request
    ensure_monitor()
    if request.args.get("run") == "1" and "selfgrade" not in reports["running"]:
        threading.Thread(target=run_selfgrade, kwargs={"post": request.args.get("post") == "1"}, daemon=True).start()
    r = reports["selfgrade"]
    return jsonify({"running": "selfgrade" in reports["running"],
                    "report": {k: v for k, v in r.items() if k != "pings"} if r else None})


@app.route("/report/discovery")
def report_discovery():
    from flask import request
    ensure_monitor()
    if request.args.get("run") == "1" and "discovery" not in reports["running"]:
        threading.Thread(target=run_discovery, kwargs={"post": request.args.get("post") == "1"}, daemon=True).start()
    return jsonify({"running": "discovery" in reports["running"], "report": reports["discovery"]})


@app.route("/tail")
def tail():
    """Live tail tracker: every ping in the last self-grade window, graded at the ping price."""
    ensure_monitor()
    r = reports["selfgrade"]
    if not r:
        return jsonify({"note": "no self-grade yet - hit /report/selfgrade?run=1", "running": "selfgrade" in reports["running"]})
    return jsonify({"updated": r["updated"], "all": r["all"], "consensus": r["consensus"],
                    "by_wallet": {w["label"]: w["tail"] for w in r["wallets"]}, "pings": r["pings"][:300]})


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
