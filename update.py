#!/usr/bin/env python3
"""Emerald Race — ranked solo match collector for two NA accounts.

Riot supplies rank, match stats and timelines. DeepLoL is an unofficial,
optional enrichment source; unknown AI/fate values remain null.
"""
import json
import os
import random
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

PLAYERS = (
    {"label": "Jam", "gameName": "Jam", "tagLine": "0304", "platform": "na1", "regional": "americas", "direction": "up"},
    {"label": "Antoñio", "gameName": "Antoñio", "tagLine": "9401", "platform": "na1", "regional": "americas", "direction": "down"},
)
TRACKER_SCHEMA_VERSION = "emerald-race-v1"
START_UNIX = 1792307345  # October 18, 2026 07:09:05 UTC
QUEUE = 420  # Ranked Solo/Duo only
QUEUE_TYPE = "RANKED_SOLO_5x5"
OUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "data.json"
PAGE_SIZE = 100
SCAN_PAGES_PER_RUN = 4
MATCH_DETAILS_PER_PLAYER_RUN = 28
EXTERNAL_ENRICHMENTS_PER_PLAYER_RUN = 12
DEEPLOL_RETRY_SECONDS = 60 * 60 * 4
TIMELINE_RETRY_SECONDS = 60 * 60 * 4
FATES = ("Godlike", "Solid", "Balanced", "Messy", "Doomed")
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY", "UNKNOWN")
TIERS = ("IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER")
DIVISIONS = {"IV": 0, "III": 1, "II": 2, "I": 3}


def avg(values):
    values = [v for v in values if type(v) in (int, float)]
    return sum(values) / len(values) if values else None


def numeric(value):
    try:
        v = float(value)
        return v if v == v and abs(v) != float("inf") else None
    except (ValueError, TypeError):
        return None


def rank_value(rank):
    """Approximate LP-equivalent rank position; not literal LP to demote/promote."""
    if not rank or rank.get("tier") not in TIERS:
        return None
    tier = rank["tier"]
    lp = int(rank.get("lp") or 0)
    return TIERS.index(tier) * 400 + (DIVISIONS.get(rank.get("division"), 0) * 100 if TIERS.index(tier) < 7 else 0) + lp


# Fixed endpoints for the two halves of the race meter:
# Jam: Platinum IV 0 LP -> Emerald IV 0 LP (left to center).
# Antoñio: Diamond I 100 LP -> Emerald I 100 LP (right to center).
PLATINUM_FLOOR = TIERS.index("PLATINUM") * 400
EMERALD_FLOOR = TIERS.index("EMERALD") * 400
DIAMOND_FLOOR = TIERS.index("DIAMOND") * 400
DIAMOND_TOP = DIAMOND_FLOOR + 400
RACE_SPAN = 400


def race_position(current, direction):
    """Fixed-scale progress to Emerald, independent of the first tracking run.

    Progress is clamped to [0, 1]. A Platinum I 100 or Diamond IV 0
    boundary does not count as Emerald until Riot reports an Emerald rank.
    """
    current_v = rank_value(current)
    if current_v is None:
        return None
    tier_index = TIERS.index(current["tier"])
    if direction == "up":
        if tier_index >= TIERS.index("EMERALD"):
            return 1.0
        if tier_index < TIERS.index("PLATINUM"):
            return 0.0
        return max(0.0, min(0.9975, (current_v - PLATINUM_FLOOR) / RACE_SPAN))
    if direction == "down":
        if tier_index <= TIERS.index("EMERALD"):
            return 1.0
        if tier_index > TIERS.index("DIAMOND"):
            return 0.0
        return max(0.0, min(0.9975, (DIAMOND_TOP - current_v) / RACE_SPAN))
    raise ValueError(f"Unknown race direction: {direction}")


def distance_to_emerald(current, direction):
    """Approximate LP remaining to the Emerald edge approached by this player."""
    val = rank_value(current)
    if val is None:
        return None
    tier_index = TIERS.index(current["tier"])
    if direction == "up":
        if tier_index >= TIERS.index("EMERALD"):
            return 0
        return max(1, EMERALD_FLOOR - val)
    if direction == "down":
        if tier_index <= TIERS.index("EMERALD"):
            return 0
        return max(1, val - DIAMOND_FLOOR)
    raise ValueError(f"Unknown race direction: {direction}")


def fresh_player(player):
    return {
        "riotId": f'{player["gameName"]}#{player["tagLine"]}',
        "direction": player["direction"],
        "puuid": None,
        "current": None,
        "rankHistory": [],
        "matches": {},
        "pendingMatchIds": [],
        "scan": {"offset": 0, "complete": False},
        "stats": {},
    }


def fresh_state():
    return {
        "trackerSchemaVersion": TRACKER_SCHEMA_VERSION,
        "updatedAt": None,
        "split": {"queue": QUEUE, "queueType": QUEUE_TYPE, "startUnix": START_UNIX},
        "activePlayerLabels": [p["label"] for p in PLAYERS],
        "players": {p["label"]: fresh_player(p) for p in PLAYERS},
    }


def load_state():
    if not OUT_PATH.exists():
        return fresh_state()
    with OUT_PATH.open(encoding="utf-8") as fh:
        state = json.load(fh)
    # Never inherit pre-race LeBlanc matches, rank or statistics. Cutoff is strict.
    if state.get("trackerSchemaVersion") != TRACKER_SCHEMA_VERSION or state.get("split", {}).get("startUnix") != START_UNIX:
        print("[reset] Old tracker data detected; starting a clean race.")
        return fresh_state()
    for player in PLAYERS:
        state.setdefault("players", {}).setdefault(player["label"], fresh_player(player))
    state["activePlayerLabels"] = [p["label"] for p in PLAYERS]
    state["players"] = {p["label"]: state["players"][p["label"]] for p in PLAYERS}
    return state


def save_state(state):
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    tmp.replace(OUT_PATH)


def riot_get(url, params=None):
    headers = {"X-Riot-Token": os.environ["RIOT_API_KEY"].strip()}
    for attempt in range(5):
        res = requests.get(url, headers=headers, params=params, timeout=25)
        if res.status_code == 200:
            return res.json()
        if res.status_code == 429 or 500 <= res.status_code < 600:
            try:
                delay = min(60, max(0, float(res.headers.get("Retry-After", 2 ** attempt + random.random()))))
            except ValueError:
                delay = 2 ** attempt
            print(f"[Riot] {res.status_code} retry in {delay:.1f}s")
            time.sleep(delay)
            continue
        res.raise_for_status()
    raise RuntimeError(f"Riot API failed after retries: {res.status_code} {url}")


def get_puuid(player):
    name = urllib.parse.quote(player["gameName"], safe="")
    tag = urllib.parse.quote(player["tagLine"], safe="")
    url = f'https://{player["regional"]}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name}/{tag}'
    return riot_get(url)["puuid"]


def get_rank(puuid, player):
    url = f'https://{player["platform"]}.api.riotgames.com/lol/league/v4/entries/by-puuid/{urllib.parse.quote(puuid, safe="")}'
    entries = riot_get(url)
    solo = next((e for e in entries if e.get("queueType") == QUEUE_TYPE), None)
    if solo is None:
        return None
    return {
        "tier": solo.get("tier"),
        "division": solo.get("rank"),
        "lp": int(solo.get("leaguePoints", 0)),
        "wins": int(solo.get("wins", 0)),
        "losses": int(solo.get("losses", 0)),
    }


def get_match_ids(puuid, player, offset):
    url = f'https://{player["regional"]}.api.riotgames.com/lol/match/v5/matches/by-puuid/{urllib.parse.quote(puuid, safe="")}/ids'
    return riot_get(url, {"queue": QUEUE, "startTime": START_UNIX, "start": offset, "count": PAGE_SIZE})


def get_match(match_id, player, timeline=False):
    suffix = "/timeline" if timeline else ""
    url = f'https://{player["regional"]}.api.riotgames.com/lol/match/v5/matches/{urllib.parse.quote(match_id, safe="")}{suffix}'
    return riot_get(url)


def role_of(participant):
    for key in ("teamPosition", "individualPosition"):
        role = str(participant.get(key) or "").upper()
        if role in ROLES and role != "UNKNOWN":
            return role
    return "UNKNOWN"


def find_opponent(participants, me):
    """Require an unambiguous opposing lane matchup; don't invent gold deltas."""
    role = role_of(me)
    if role in ("UTILITY", "UNKNOWN"):
        return None
    rivals = [p for p in participants if p.get("teamId") != me.get("teamId") and role_of(p) == role]
    return rivals[0] if len(rivals) == 1 else None


def frame_gold_lead_15(timeline, own_id, rival_id):
    frames = timeline.get("info", {}).get("frames", [])
    target_ms = 15 * 60 * 1000
    matching = [f for f in frames if abs(int(f.get("timestamp", -1)) - target_ms) <= 30_000]
    if not matching:
        return None
    frame = min(matching, key=lambda f: abs(int(f.get("timestamp", 0)) - target_ms))
    players = frame.get("participantFrames", {})
    own = players.get(str(own_id)) or players.get(own_id)
    rival = players.get(str(rival_id)) or players.get(rival_id)
    if not own or not rival:
        return None
    a, b = numeric(own.get("totalGold")), numeric(rival.get("totalGold"))
    return int(a - b) if a is not None and b is not None else None


def fate_from_scores(players, target):
    """Existing tracker *estimated fate* formula, not a DeepLoL official fate label."""
    my_score = numeric(target.get("ai_score"))
    side = target.get("side")
    id_ = target.get("puu_id")
    if my_score is None or side is None or not id_:
        return None
    mates = [numeric(p.get("ai_score")) for p in players if p.get("side") == side and p.get("puu_id") != id_]
    avg_mates = avg(mates)
    if avg_mates is None:
        return None
    luck = max(0, min(100, .46 * (avg_mates - my_score + 50) + .54 * ((avg_mates + my_score) / 2)))
    return ("Godlike" if luck > 59 else "Solid" if luck > 52 else "Balanced" if luck > 49 else "Messy" if luck > 41 else "Doomed")


def deeplol_data(player, match_id, puuid):
    """Best-effort access to an undocumented third-party match cache."""
    platform = match_id.split("_")[0]
    response = requests.get(
        "https://b2c-api-cdn.deeplol.gg/match/match-cached",
        params={"match_id": match_id, "platform_id": platform},
        timeout=18,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    participants = response.json().get("participants_list", [])
    target = next((x for x in participants if
        x.get("puu_id") == puuid or (
            str(x.get("riot_id_name", "")).casefold() == player["gameName"].casefold()
            and str(x.get("riot_id_tag_line", "")).casefold() == player["tagLine"].casefold()
        )), None)
    if not target:
        return None
    return {"aiScore": numeric(target.get("ai_score")), "fate": fate_from_scores(participants, target)}


def parse_match(match, player_puuid):
    info = match.get("info", {})
    if info.get("queueId") != QUEUE:
        return None
    start = info.get("gameStartTimestamp")
    # Riot's startTime API filter should already enforce this. Recheck to avoid
    # counting a game already in progress at the requested cutoff.
    if not start or int(start) // 1000 < START_UNIX:
        return None
    participants = info.get("participants", [])
    me = next((x for x in participants if x.get("puuid") == player_puuid), None)
    if not me:
        return None
    duration = max(0, int(info.get("gameDuration") or 0))
    kills, deaths, assists = (int(me.get(k) or 0) for k in ("kills", "deaths", "assists"))
    cs = int(me.get("totalMinionsKilled") or 0) + int(me.get("neutralMinionsKilled") or 0)
    role = role_of(me)
    rival = find_opponent(participants, me)
    return {
        "win": bool(me.get("win")), "championId": str(me.get("championId")),
        "championName": me.get("championName") or None,
        "durationSeconds": duration, "startTs": int(start) // 1000,
        "endTs": int(info.get("gameEndTimestamp") or 0) // 1000 or None,
        "kills": kills, "deaths": deaths, "assists": assists,
        "kda": (kills + assists) / max(deaths, 1),
        "csPerMin": cs * 60 / duration if duration else None,
        "role": role, "participantId": me.get("participantId"),
        "opponentId": rival.get("participantId") if rival else None,
        "goldLead15": None, "aiScore": None, "fate": None,
        "deeplolLastAttempt": 0, "timelineLastAttempt": 0,
    }


def enrich_matches(player_state, player):
    matches = player_state["matches"]
    now = int(time.time())
    attempts = 0
    for mid, row in sorted(matches.items(), key=lambda p: p[1].get("startTs", 0), reverse=True):
        if attempts >= EXTERNAL_ENRICHMENTS_PER_PLAYER_RUN:
            break
        if (row.get("aiScore") is None or row.get("fate") is None) and now - row.get("deeplolLastAttempt", 0) >= DEEPLOL_RETRY_SECONDS:
            attempts += 1
            row["deeplolLastAttempt"] = now
            try:
                extra = deeplol_data(player, mid, player_state["puuid"])
                if extra:
                    if extra["aiScore"] is not None:
                        row["aiScore"] = extra["aiScore"]
                    if extra["fate"] is not None:
                        row["fate"] = extra["fate"]
            except (requests.RequestException, ValueError, KeyError) as exc:
                print(f"[DeepLoL] {mid}: {exc}")
        if attempts >= EXTERNAL_ENRICHMENTS_PER_PLAYER_RUN:
            break
        if (row.get("role") not in ("UTILITY", "UNKNOWN") and row.get("durationSeconds", 0) >= 900
            and row.get("opponentId") is not None and row.get("goldLead15") is None
            and now - row.get("timelineLastAttempt", 0) >= TIMELINE_RETRY_SECONDS):
            attempts += 1
            row["timelineLastAttempt"] = now
            try:
                tl = get_match(mid, player, timeline=True)
                row["goldLead15"] = frame_gold_lead_15(tl, row["participantId"], row["opponentId"])
            except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
                print(f"[timeline] {mid}: {exc}")


def calculate_stats(matches):
    rows = list(matches.values())
    games = len(rows)
    wins = sum(1 for r in rows if r["win"])
    champions = {}
    roles = {role: 0 for role in ROLES}
    fates = {fate: 0 for fate in FATES}
    fates["Unavailable"] = 0
    for row in rows:
        c = champions.setdefault(row["championId"], {"name": row.get("championName"), "games": 0, "wins": 0})
        c["games"] += 1
        c["wins"] += bool(row["win"])
        if not c["name"] and row.get("championName"):
            c["name"] = row["championName"]
        role = row.get("role", "UNKNOWN")
        roles[role if role in roles else "UNKNOWN"] += 1
        fate = row.get("fate")
        fates[fate if fate in FATES else "Unavailable"] += 1
    most = sorted(champions.items(), key=lambda x: (-x[1]["games"], -x[1]["wins"], x[0]))
    ai_win = [r.get("aiScore") for r in rows if r["win"]]
    ai_loss = [r.get("aiScore") for r in rows if not r["win"]]
    gold = [r.get("goldLead15") for r in rows if r.get("role") not in ("UTILITY", "UNKNOWN")]
    return {
        "games": games, "wins": wins, "losses": games - wins,
        "winrate": (wins / games) if games else None,
        "totalPlaytimeSeconds": sum(r["durationSeconds"] for r in rows),
        "mostPlayedChampion": ({"id": most[0][0], **most[0][1]} if most else None),
        "champions": champions,
        "averageKda": avg([r.get("kda") for r in rows]),
        "averageCsPerMin": avg([r.get("csPerMin") for r in rows]),
        "averageAiScoreWin": avg(ai_win),
        "averageAiScoreLoss": avg(ai_loss),
        "aiWinSampleCount": sum(1 for v in ai_win if numeric(v) is not None),
        "aiLossSampleCount": sum(1 for v in ai_loss if numeric(v) is not None),
        "averageGoldLead15NonSupport": avg(gold),
        "goldLead15SampleCount": sum(1 for v in gold if numeric(v) is not None),
        "fateCounts": fates,
        "roleCounts": roles,
    }


def update_player(player_state, player):
    if not player_state.get("puuid"):
        player_state["puuid"] = get_puuid(player)
    puuid = player_state["puuid"]

    current = get_rank(puuid, player)
    player_state["current"] = current
    player_state.pop("initialRank", None)  # Legacy, no longer used as a race baseline.
    if current:
        baseline = {k: current[k] for k in ("tier", "division", "lp")}
        past = player_state.setdefault("rankHistory", [])
        if not past or any(past[-1].get(k) != baseline[k] for k in baseline):
            past.append({"ts": int(time.time()), **baseline})
    player_state["distanceToEmeraldLpEquivalent"] = distance_to_emerald(current, player["direction"])
    player_state["raceProgress"] = race_position(current, player["direction"])

    matches = player_state.setdefault("matches", {})
    pending = player_state.setdefault("pendingMatchIds", [])
    pending_set = set(pending)
    scan = player_state.setdefault("scan", {"offset": 0, "complete": False})

    # Every run sees the latest page, even during a large initial backfill.
    latest = get_match_ids(puuid, player, 0)
    for mid in reversed(latest):
        if mid not in matches and mid not in pending_set:
            pending.append(mid)
            pending_set.add(mid)

    # Scan history from the cutoff, in bounded chunks. Resuming on later runs
    # is safe: match IDs are deduplicated in the canonical matches dictionary.
    if not scan.get("complete"):
        for _ in range(SCAN_PAGES_PER_RUN):
            offset = int(scan.get("offset", 0))
            ids = latest if offset == 0 else get_match_ids(puuid, player, offset)
            for mid in reversed(ids):
                if mid not in matches and mid not in pending_set:
                    pending.append(mid)
                    pending_set.add(mid)
            scan["offset"] = offset + len(ids)
            if len(ids) < PAGE_SIZE:
                scan["complete"] = True
                break

    processed = 0
    # Oldest pending first so initial setup fills all post-cutoff games.
    for _ in range(min(len(pending), MATCH_DETAILS_PER_PLAYER_RUN)):
        mid = pending.pop(0)
        pending_set.remove(mid)
        if mid in matches:
            continue
        try:
            match = get_match(mid, player)
            record = parse_match(match, puuid)
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
            print(f"[{player['label']}] match {mid} retry later: {exc}")
            pending.append(mid)
            pending_set.add(mid)
            continue
        if record:
            matches[mid] = record
            processed += 1
        # A match that fails the cutoff/queue filter is intentionally excluded.

    enrich_matches(player_state, player)
    player_state["stats"] = calculate_stats(matches)
    player_state["coverage"] = {"pendingGames": len(pending), "historyScanComplete": bool(scan.get("complete"))}
    print(f"[{player['label']}] rank={current} games={len(matches)} newly_fetched={processed} pending={len(pending)} scan_done={scan['complete']}")


def main():
    if not os.environ.get("RIOT_API_KEY", "").strip():
        raise SystemExit("Set RIOT_API_KEY in the CircleCI project environment (or your shell) before running.")
    state = load_state()
    failed = []
    for player in PLAYERS:
        try:
            update_player(state["players"][player["label"]], player)
        except Exception as exc:
            failed.append(f"{player['label']}: {exc}")
            print(f"[ERROR] {player['label']}: {exc}")
    now_ny = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    state["updatedAt"] = {"iso": now_ny.isoformat(), "display": now_ny.strftime("%b %d, %Y %I:%M %p ET")}
    save_state(state)
    if failed:
        raise SystemExit("; ".join(failed))


if __name__ == "__main__":
    main()
