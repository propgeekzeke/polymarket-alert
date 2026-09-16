"""PolyAlert v2 - pre-game sharp tailing: tailability check, consensus, CLV scoreboard."""
import os, re, time, threading, requests, statistics, json, fcntl, unicodedata
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)

WALLETS = {
    # Core: consistent CLV-beaters (curated 2026-07-15)
    "0xb90494d9a5d8f71f1930b2aa4b599f95c344c255": "#Airpods123",   # 76% beat, +1.24% CLV (n=83), $1.02M
    "0xa804390f80019699ab34a282c0df7528fba82a75": "#RiverSkew",    # 65% beat, +2.16% CLV (n=154), $389k
    "0xb61b2079b95f6b7476fd3203e0274ffb93308a06": "#Hot2Trot",     # 67% beat, +2.67% CLV (n=33), soccer whale $2.11M
    "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1": "#Sharp2a2C",    # 70% beat, +0.88% CLV (n=20), $4.31M
    "0x709e8dcb133555794decc598e07f2c923b8366f5": "#0X70",         # 67% beat, +0.91% CLV (n=15), big bettor
    "0xec8d7bf83a1db5f06b9535985e58ffd17708dd71": "#Gardiner",     # 60% beat, +0.76% CLV (n=166), $207k
    "0x076daa87c4fe1a85402a9b6b8e0a866224388d4c": "#Sharp076d",    # 71% beat, +1.82% CLV / +3.14% wtd (n=21), $3.76M, soccer +15.6% ROI
    # Whales: don't beat CLV (likely bet late to hide it) but hugely profitable - tail on selection
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563": "#Whale2c33",    # $6.52M all-time, in-line CLV
    "0x204f72f35326db932158cba6adff0b9a1da95e14": "#SwissTony",    # $19.34M all-time, in-line CLV
    # CFB specialists (prober 2026-09-12: 567 games / 9,112 markets scanned, full-history pre-game CLV)
    "0x16b29c50f2439faf627209b2ac0c7bbddaa8a881": "#SeriouslySirius",  # CFB 71% beat, +2.84% CLV (n=251), $3.65M
    "0xb889590a2fab0c810584a660518c4c020325a430": "#Ems123",           # CFB 84% beat, +0.92% CLV (n=25), $961k, avg $46k
    "0x5268527977f700f9bf9b6d5cd843859e4e70135d": "#HomeRunHazard",    # CFB 74% beat, +2.63% CLV (n=27), $2.44M
    "0xf68a281980f8c13828e84e147e3822381d6e5b1b": "#Nooserac",         # CFB 11/11 beat close, +1.44% CLV, $717k (+$98k 30d) - TRIAL, thin n
    "0x29b15e8557b2cb5b5ba1ec8b5cbba479ee26f737": "#Elenes",           # Soccer +51% ROI (+$526k), 84% hit pre-game n=102 but 32% beat close - TRIAL, soccer only
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
    # Conviction thresholds ~= 80% of each wallet's average bet - only real plays ping
    "0xb90494d9a5d8f71f1930b2aa4b599f95c344c255": 65000,  # #Airpods123 (avg $84k)
    "0xa804390f80019699ab34a282c0df7528fba82a75": 5000,   # #RiverSkew (avg $6.4k)
    "0xb61b2079b95f6b7476fd3203e0274ffb93308a06": 85000,  # #Hot2Trot (avg $108k)
    "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1": 30000,  # #Sharp2a2C (avg $38k)
    "0x709e8dcb133555794decc598e07f2c923b8366f5": 150000, # #0X70 (avg $185k)
    "0xec8d7bf83a1db5f06b9535985e58ffd17708dd71": 5500,   # #Gardiner (avg $7k)
    "0x076daa87c4fe1a85402a9b6b8e0a866224388d4c": 6500,   # #Sharp076d (avg $8.1k)
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563": 75000,  # #Whale2c33 (conviction floor; skips market-making)
    "0x204f72f35326db932158cba6adff0b9a1da95e14": 50000,  # #SwissTony (conviction floor; skips market-making)
    "0x16b29c50f2439faf627209b2ac0c7bbddaa8a881": 10000,  # #SeriouslySirius (elite in 5 sports; NBA/NHL/soccer avg $22-33k, NFL $144k)
    "0xb889590a2fab0c810584a660518c4c020325a430": 37000,  # #Ems123 (avg $46k)
    "0x5268527977f700f9bf9b6d5cd843859e4e70135d": 3200,   # #HomeRunHazard (avg $4k)
    "0xf68a281980f8c13828e84e147e3822381d6e5b1b": 2800,   # #Nooserac (avg $3.5k)
    "0x29b15e8557b2cb5b5ba1ec8b5cbba479ee26f737": 8000,   # #Elenes (pre-game avg $10.2k)
}

# Per-wallet sport block-list (event-slug prefixes). Sharp076d: soccer edge only - his tennis is
# in-play grinding with 52% beat-close pre-game (n=96) and esports is noise; never ping those.
_TENNIS  = ("atp-", "wta-")
_ESPORTS = ("lol-", "cs-", "cs2-", "val-", "dota-", "esports-", "r6-", "rl-")
WALLET_BLOCK = {
    # per-sport test 2026-09-14 (per_sport_report_2026-09-14.md): block sports with no pre-game CLV edge
    "0x076daa87c4fe1a85402a9b6b8e0a866224388d4c": _TENNIS + _ESPORTS,            # #Sharp076d: soccer only (tennis 50% beat, 56% live)
    "0xa804390f80019699ab34a282c0df7528fba82a75": _TENNIS + _ESPORTS,            # #RiverSkew: tennis -0.16% CLV (n=64), esports 43% beat
    "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1": _TENNIS + _ESPORTS + ("nhl-",),# #Sharp2a2C: edge is CBB+soccer; NHL/esports in-line, tennis -$1.69M
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563": ("nfl-", "cfb-"),               # #Whale2c33: NFL 29% beat / -2.59% CLV, CFB 36% beat
    "0x5268527977f700f9bf9b6d5cd843859e4e70135d": _TENNIS + ("mlb-",),           # #HomeRunHazard: MLB grinder (52% beat, 66% live), tennis 94% live
    "0x709e8dcb133555794decc598e07f2c923b8366f5": ("ufc-", "mlb-", "nhl-"),       # #0X70: -$515k combined, tiny samples
    "0x29b15e8557b2cb5b5ba1ec8b5cbba479ee26f737": _TENNIS + _ESPORTS + ("nhl-", "nfl-", "cfb-", "nba-", "wnba-", "cbb-", "mlb-", "ufc-"),  # #Elenes: soccer only (NHL -43% ROI)
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
wallet_cards = {}        # wallet -> {avg_bet, all_pnl, all_roi, soc_pnl, soc_roi} for alerts
sport_stats  = {}        # wallet -> {sport: {positions, cost, pnl, roi, n, avg_clv_pp, beat_close_pct}} (background-loaded)
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
        total = sum(raw_probs.values())
        fair = {k: v / total for k, v in raw_probs.items()}

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
            for name, prob in fair.items():
                if name == "draw":
                    continue
                score = len(title_words & (set(name.replace("-", " ").split()) - STOP))
                if score > best_team_score:
                    best_team_score, best_team_prob = score, prob
            if best_team_prob is not None and best_team_score > 0:
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

        method = f"{'3way' if is_3way else '2way'}-proportional-devig(pinnacle {market_type}" + (f" {line:g}" if line is not None else "") + ")"
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
        # 1) match fixture by team-name overlap
        tw = _words(title_lower)
        best, best_s = None, 0
        for f in _optic_fixtures(sport, gs):
            names = _words((f.get("home_team_display") or "") + " " + (f.get("away_team_display") or ""))
            s = len(tw & names)
            if s > best_s:
                best_s, best = s, f
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
        if len(sel) < 2:
            print(f"Optic: {'line %g ' % line if line is not None else ''}{want[0]} not offered for {best.get('home_team_display')} v {best.get('away_team_display')}", flush=True)
            return None

        def am_to_prob(p):
            p = float(p)
            return 100 / (p + 100) if p > 0 else abs(p) / (abs(p) + 100)
        raw = {o["name"].lower(): am_to_prob(o["price"]) for o in sel}
        tot = sum(raw.values())
        fair = {k: v / tot for k, v in raw.items()}
        is_3way = len(fair) == 3

        fair_prob = None
        if outcome_lower in ("over", "under"):
            for k, v in fair.items():
                if k.startswith(outcome_lower):
                    fair_prob = v
        elif outcome_lower in ("yes", "no"):
            # "Will X win?" -> X's outcome by title overlap
            bn, bs = None, 0
            for k in fair:
                if k == "draw":
                    continue
                s = len(tw & _words(k))
                if s > bs:
                    bs, bn = s, k
            if bn is not None and bs > 0:
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
        method = f"{'3way' if is_3way else '2way'}-proportional-devig(pinnacle/optic {sel[0]['market']}" + (f" {line:g}" if line is not None else "") + ")"
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


def compute_sport_stats(wallet, per_sport=40, max_positions=8000):
    """Per-sport realized P&L/ROI over closed positions + pre-game CLV on that sport's biggest $1k+ bets."""
    closed, seen, offset = [], set(), 0
    while offset < max_positions:
        try:
            b = requests.get("https://data-api.polymarket.com/closed-positions",
                             params={"user": wallet, "limit": 50, "offset": offset,
                                     "sortBy": "TIMESTAMP", "sortDirection": "DESC"}, timeout=15).json()
        except Exception:
            break
        if not isinstance(b, list) or not b:
            break
        new = 0
        for p in b:
            k = (p.get("asset"), p.get("conditionId"))
            if k not in seen:
                seen.add(k); closed.append(p); new += 1
        if not new or len(b) < 50:
            break
        offset += 50
    by = {}
    for p in closed:
        sp = _sport_of(p.get("eventSlug") or p.get("slug"))
        cost = (p.get("totalBought") or 0) * (p.get("avgPrice") or 0)
        d = by.setdefault(sp, {"cost": 0.0, "pnl": 0.0, "n": 0, "big": []})
        d["cost"] += cost; d["pnl"] += (p.get("realizedPnl") or 0); d["n"] += 1
        if cost >= 1000:
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
            try:
                tr = requests.get("https://data-api.polymarket.com/trades",
                                  params={"user": wallet, "market": p.get("conditionId"), "limit": 300},
                                  timeout=15).json()
            except Exception:
                continue
            if not isinstance(tr, list):
                continue
            pre = [t for t in tr if isinstance(t, dict) and t.get("side") == "BUY"
                   and t.get("asset") == p.get("asset") and (t.get("timestamp") or 0) < gs]
            stake = sum((t.get("size") or 0) * (t.get("price") or 0) for t in pre)
            if stake < 1000:
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
        try:
            sport_stats[wallet] = compute_sport_stats(wallet)
            summ = ", ".join(f"{k}: {v['beat_close_pct']}%/n{v['n']}" for k, v in sport_stats[wallet].items() if v.get("n"))
            print(f"Sport stats {label}: {summ}", flush=True)
        except Exception as e:
            print(f"Sport stats error {label}: {e}", flush=True)


def compute_wallet_card(wallet):
    """Startup stats for alerts: avg bet, all-time + soccer P&L/ROI (mark-to-market)."""
    SOC = ("fifwc", "epl-", "lal-", "ucl-", "sea-", "bun-", "li1-", "mls-", "uel-",
           "world-cup", "copa", "champions")
    raw, offset = [], 0
    while offset < 6000:
        try:
            b = requests.get("https://data-api.polymarket.com/activity",
                             params={"user": wallet, "limit": 500, "offset": offset}, timeout=15).json()
        except Exception:
            break
        if not isinstance(b, list) or not b:
            break
        raw.extend(b)
        if len(b) < 500:
            break
        offset += 500
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
        p = requests.get("https://data-api.polymarket.com/positions",
                         params={"user": wallet, "limit": 500}, timeout=15).json()
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
        wallet_cards.update(d.get("wallet_cards", {}))
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
    pin         = get_optic_devig(event_slug, title, outcome, now_price or price, gs) or get_pinnacle_devig(event_slug, title, outcome, now_price or price)
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

    card  = wallet_cards.get(wallet, {})
    sport = _sport_of(event_slug)
    ss    = (sport_stats.get(wallet) or {}).get(sport) or {}
    if ss.get("n"):
        lines.append(f"\U0001f4c8 **{sport} CLV {ss['avg_clv_pp']:+.2f}%** avg  ·  "
                     f"beats close **{ss['beat_close_pct']}%**  ·  n={ss['n']}")
    elif my_clv:
        lines.append(f"\U0001f4c8 **CLV {my_clv['avg_clv_pp']:+.2f}%** avg (all sports)  ·  "
                     f"beats close **{my_clv['beat_close_pct']}%**  ·  n={my_clv['n']}")
    stat = []
    allp = card.get("all_pnl"); allr = card.get("all_roi")
    if allp is not None:
        a = "▲" if allp >= 0 else "▼"
        stat.append(f"All-time {a} **${abs(allp):,}**" + (f" ({allr:+g}% ROI)" if allr is not None else ""))
    if ss.get("pnl") is not None:
        a = "▲" if ss["pnl"] >= 0 else "▼"
        stat.append(f"{sport} {a} **${abs(ss['pnl']):,}**"
                    + (f" ({ss['roi']:+g}% ROI, {ss['positions']} bets)" if ss.get("roi") is not None else ""))
    elif sport == "Soccer" and card.get("soc_pnl") is not None:
        socp = card["soc_pnl"]; socr = card.get("soc_roi")
        a = "▲" if socp >= 0 else "▼"
        stat.append(f"Soccer {a} **${abs(socp):,}**" + (f" ({socr:+g}% ROI)" if socr is not None else ""))
    if stat:
        lines.append("\U0001f4b0 " + "  ·  ".join(stat))

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
    if any(event_slug.startswith(p) for p in WALLET_BLOCK.get(wallet, ())):
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
        "pinnacle":        bool(ODDS_API_KEY or OPTIC_API_KEY),
        "optic":           bool(OPTIC_API_KEY),
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
        "pinnacle":        bool(ODDS_API_KEY or OPTIC_API_KEY),
        "optic":           bool(OPTIC_API_KEY),
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
        "pinnacle":        bool(ODDS_API_KEY or OPTIC_API_KEY),
        "optic":           bool(OPTIC_API_KEY),
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
