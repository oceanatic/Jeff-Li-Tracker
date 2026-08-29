#!/usr/bin/env python3
# LEBLANC ADVANCED TRACKER V4 — THIS FILE MUST LIVE AT scripts/update.py
# Writes KDA, CS/min, DeepLoL AI/fate, gold@15, and LeBlanc LP attribution to docs/data.json.
import os
import json
import time
import urllib.parse
import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

RIOT_API_KEY = os.environ["RIOT_API_KEY"].strip()

PLAYERS = [
    {
        "label": "Jeff Li: The Journey to Emerald",
        "gameName": "Buy Again",
        "tagLine": "NA1",
        "platform": "na1",
        "regional": "americas",
    },
]

QUEUE_RANKED_SOLO = 420
QUEUE_TYPE_SOLO = "RANKED_SOLO_5x5"
LEBLANC_CHAMPION_ID = "7"
TRACKER_SCHEMA_VERSION = "leblanc-advanced-v4"

OUT_PATH = "docs/data.json"

# Split tracking start: August 28, 2026 07:00 UTC
SPLIT_START_UNIX = 1787901411

# Match-v5 paging / rate safety
MATCH_PAGE_SIZE = 100
MAX_MATCH_DETAILS_PER_RUN = 40
RATE_LIMIT_HIT_LIMIT = 2
BACKFILL_PAGES_PER_RUN = 7
STOP_BACKFILL_ON_FIRST_SEEN = True

# Advanced LeBlanc enrichment. Existing matchesSeen are backfilled gradually.
MAX_ADVANCED_MATCH_BACKFILL_PER_RUN = 10
MAX_DEEPLOL_RETRIES_PER_RUN = 10
MAX_GOLD15_RETRIES_PER_RUN = 10

FATE_NAMES = ("Godlike", "Solid", "Balanced", "Messy", "Doomed")


def riot_get(url, params=None, max_retries=6):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    last = None

    for attempt in range(max_retries):
        r = requests.get(url, headers=headers, params=params, timeout=30)
        last = r

        if r.status_code == 200:
            return r.json()

        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            if retry_after is not None:
                sleep_s = float(retry_after)
            else:
                sleep_s = min(60.0, (2 ** attempt) + random.random())
            print(f"[riot_get] 429; sleeping {sleep_s:.1f}s -> {url}")
            time.sleep(sleep_s)
            continue

        if r.status_code in (500, 502, 503, 504):
            sleep_s = min(30.0, (2 ** attempt) + random.random())
            print(f"[riot_get] {r.status_code}; sleeping {sleep_s:.1f}s -> {url}")
            time.sleep(sleep_s)
            continue

        try:
            body_preview = (r.text or "")[:300]
        except Exception:
            body_preview = "<unreadable body>"

        print(f"[riot_get] {r.status_code} {url} params={params} body={body_preview}")
        r.raise_for_status()

    if last is not None:
        raise requests.HTTPError(
            f"Failed after retries ({max_retries}) for {url}: "
            f"{last.status_code} {last.text[:200]}"
        )

    raise requests.HTTPError(f"Failed after retries ({max_retries}) for {url}")


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def average(values):
    values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return (sum(values) / len(values)) if values else None


def rank_value(tier, division, lp):
    """LP-equivalent rank value: 100 LP/division, 400 LP/tier."""
    tier_order = {
        "IRON": 0,
        "BRONZE": 1,
        "SILVER": 2,
        "GOLD": 3,
        "PLATINUM": 4,
        "EMERALD": 5,
        "DIAMOND": 6,
        "MASTER": 7,
        "GRANDMASTER": 8,
        "CHALLENGER": 9,
    }
    div_order = {"IV": 0, "III": 1, "II": 2, "I": 3}
    return tier_order.get(tier, -1) * 400 + div_order.get(division, 0) * 100 + int(lp)


def seconds_to_hms(total):
    total = int(total)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:d}:{m:02d}:{s:02d}"


def load_state():
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    return {
        "updatedAt": None,
        "split": {
            "queue": QUEUE_RANKED_SOLO,
            "queueType": QUEUE_TYPE_SOLO,
            "startUnix": SPLIT_START_UNIX,
        },
        "players": {},
    }


def save_state(state):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_puuid(game_name, tag_line, regional):
    gn = urllib.parse.quote(game_name)
    tl = urllib.parse.quote(tag_line)
    url = (
        f"https://{regional}.api.riotgames.com/riot/account/v1/accounts/"
        f"by-riot-id/{gn}/{tl}"
    )
    data = riot_get(url)
    if "puuid" not in data:
        raise RuntimeError(f"Account lookup failed: {data}")
    return data["puuid"]


def get_league_entries_by_puuid(puuid, platform):
    url = f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
    return riot_get(url)


def get_ranked_solo_entry(entries):
    for entry in entries:
        if entry.get("queueType") == QUEUE_TYPE_SOLO:
            return entry
    return None


def get_match_ids(puuid, regional, start=0, count=20, start_time=None):
    url = f"https://{regional}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
    params = {
        "queue": QUEUE_RANKED_SOLO,
        "start": start,
        "count": count,
    }
    if start_time is not None:
        params["startTime"] = int(start_time)
    return riot_get(url, params=params)


def get_match(match_id, regional):
    url = f"https://{regional}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    return riot_get(url)


def get_match_timeline(match_id, regional):
    url = f"https://{regional}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
    return riot_get(url)


def calculate_teammate_average_ai_score(participants, target_player):
    target_side = target_player.get("side")
    target_puuid = target_player.get("puu_id")

    if not target_side or not target_puuid:
        return None

    teammate_scores = []
    for player in participants:
        same_team = player.get("side") == target_side
        not_target = player.get("puu_id") != target_puuid
        if same_team and not_target:
            score = safe_float(player.get("ai_score"))
            if score is not None:
                teammate_scores.append(score)

    return average(teammate_scores)


def calculate_team_luck_value(avg_ai_score, my_ai_score):
    avg_score = safe_float(avg_ai_score)
    my_score = safe_float(my_ai_score)

    if avg_score is None or my_score is None:
        return None

    value = 0.46 * (avg_score - my_score + 50) + 0.54 * ((avg_score + my_score) / 2)
    return round(max(0, min(100, value)), 1)


def get_fate_from_team_luck_value(team_luck_value):
    value = safe_float(team_luck_value)
    if value is None:
        return None
    if value > 59:
        return "Godlike"
    if value > 52:
        return "Solid"
    if value > 49:
        return "Balanced"
    if value > 41:
        return "Messy"
    return "Doomed"


def get_deeplol_stats(game_name, tag_line, match_id):
    """Uses the same DeepLoL cached-match method from the user's older tracker."""
    try:
        platform_id = match_id.split("_")[0]
        url = (
            "https://b2c-api-cdn.deeplol.gg/match/match-cached"
            f"?match_id={urllib.parse.quote(match_id)}"
            f"&platform_id={urllib.parse.quote(platform_id)}"
        )

        response = requests.get(url, timeout=20)

        if response.status_code == 404:
            print(f"[DeepLoL] not cached yet: {match_id}")
            return {
                "ai_score": None,
                "fate": None,
                "team_luck_value": None,
                "teammate_average_ai_score": None,
            }

        response.raise_for_status()
        data = response.json()
        participants = data.get("participants_list", [])

        for player in participants:
            player_name = str(player.get("riot_id_name", "")).lower()
            player_tag = str(player.get("riot_id_tag_line", "")).lower()

            if player_name == game_name.lower() and player_tag == tag_line.lower():
                ai_score = safe_float(player.get("ai_score"))
                teammate_avg = calculate_teammate_average_ai_score(participants, player)
                team_luck_value = calculate_team_luck_value(teammate_avg, ai_score)
                fate = get_fate_from_team_luck_value(team_luck_value)

                print(
                    f"[DeepLoL] {match_id}: ai={ai_score} teammate_avg={teammate_avg} "
                    f"luck={team_luck_value} fate={fate}"
                )

                return {
                    "ai_score": ai_score,
                    "fate": fate,
                    "team_luck_value": team_luck_value,
                    "teammate_average_ai_score": teammate_avg,
                }

        print(f"[DeepLoL] player not found in participants: {match_id}")

    except Exception as error:
        print(f"[DeepLoL] failed for {match_id}: {error}")

    return {
        "ai_score": None,
        "fate": None,
        "team_luck_value": None,
        "teammate_average_ai_score": None,
    }


def game_end_unix(info):
    end_ms = info.get("gameEndTimestamp")
    if isinstance(end_ms, (int, float)) and end_ms > 0:
        return int(end_ms // 1000)

    start_ms = info.get("gameStartTimestamp")
    duration = int(info.get("gameDuration", 0) or 0)
    if isinstance(start_ms, (int, float)) and start_ms > 0:
        return int(start_ms // 1000) + duration

    return None


def find_lane_opponent(participants, me):
    my_team = me.get("teamId")
    my_team_position = str(me.get("teamPosition") or "").upper()
    my_individual_position = str(me.get("individualPosition") or "").upper()

    enemies = [p for p in participants if p.get("teamId") != my_team]

    if my_team_position:
        same = [p for p in enemies if str(p.get("teamPosition") or "").upper() == my_team_position]
        if len(same) == 1:
            return same[0]

    if my_individual_position:
        same = [p for p in enemies if str(p.get("individualPosition") or "").upper() == my_individual_position]
        if len(same) == 1:
            return same[0]

    # LeBlanc is overwhelmingly a mid-lane champion. Use MIDDLE only as a fallback.
    middle = [
        p for p in enemies
        if str(p.get("teamPosition") or p.get("individualPosition") or "").upper() == "MIDDLE"
    ]
    if len(middle) == 1:
        return middle[0]

    return None


def gold_lead_at_15(timeline, my_participant_id, opponent_participant_id):
    if not timeline or my_participant_id is None or opponent_participant_id is None:
        return None

    frames = timeline.get("info", {}).get("frames", [])
    if not frames:
        return None

    target_ms = 15 * 60 * 1000
    frame = min(frames, key=lambda f: abs(int(f.get("timestamp", 0)) - target_ms))

    # Do not pretend a much earlier frame is "15 minutes".
    if abs(int(frame.get("timestamp", 0)) - target_ms) > 70_000:
        return None

    participant_frames = frame.get("participantFrames", {})
    mine = participant_frames.get(str(my_participant_id)) or participant_frames.get(my_participant_id)
    opp = participant_frames.get(str(opponent_participant_id)) or participant_frames.get(opponent_participant_id)

    if not mine or not opp:
        return None

    my_gold = mine.get("totalGold")
    opp_gold = opp.get("totalGold")
    if not isinstance(my_gold, (int, float)) or not isinstance(opp_gold, (int, float)):
        return None

    return int(my_gold - opp_gold)


def ensure_player_state(players, p):
    label = p["label"]
    st = players.setdefault(label, {})

    st.setdefault("riotId", f'{p["gameName"]}#{p["tagLine"]}')
    st.setdefault("puuid", None)
    st.setdefault("current", None)
    st.setdefault("leagueRecord", None)
    st.setdefault("peak", None)
    st.setdefault("lpHistory", [])
    st.setdefault("matchesSeen", [])
    st.setdefault("pendingMatchIds", [])
    st.setdefault("backfill", {"nextStart": 0, "done": False})
    st.setdefault("matchMeta", {})

    stats = st.setdefault("stats", {})
    defaults = {
        "splitMatchIdsFound": None,
        "splitMatchesProcessed": 0,
        "splitBackfillRemaining": None,
        "totalPlaytimeSeconds": 0,
        "totalPlaytimeHMS": "0:00:00",
        "games": 0,
        "wins": 0,
        "losses": 0,
        "leblancGames": 0,
        "leblancWins": 0,
        "leblancLosses": 0,
        "leblancWinrate": None,
        "gamesOffLeblanc": 0,
        "champions": {},
        "mostPlayedChampionId": None,
        "highestWinrateChampionId": None,
        "lowestWinrateChampionId": None,
        "highestWinrateChampionWR": None,
        "lowestWinrateChampionWR": None,
        "avgLpGainPerWin": None,
        "avgLpLossPerLoss": None,
        "lpDelta": {"wins": [], "losses": []},
        # Advanced LeBlanc fields expected by index.html
        "leblancMatchStats": {},
        "leblancAverageAiScore": None,
        "leblancAverageKda": None,
        "leblancAverageKills": None,
        "leblancAverageDeaths": None,
        "leblancAverageAssists": None,
        "leblancAverageGoldLead15": None,
        "leblancAverageCsPerMin": None,
        "leblancFateCounts": {name: 0 for name in FATE_NAMES},
        "leblancLpAttributions": {},
        "leblancLpNet": 0,
        "leblancLpTrackedGames": 0,
    }

    for key, value in defaults.items():
        stats.setdefault(key, value)

    for name in FATE_NAMES:
        stats["leblancFateCounts"].setdefault(name, 0)

    return st, stats


def write_basic_match_meta(st, mid, info, me):
    meta = st["matchMeta"].setdefault(mid, {})
    meta["championId"] = str(me.get("championId"))
    meta["win"] = bool(me.get("win"))
    meta["gameEndTs"] = game_end_unix(info)
    meta["participantId"] = me.get("participantId")
    return meta


def build_or_update_leblanc_record(st, stats, p, mid, match, regional, fetch_external=True):
    info = match.get("info", {})
    participants = info.get("participants", [])
    me = next((x for x in participants if x.get("puuid") == st["puuid"]), None)
    if not me:
        return False

    meta = write_basic_match_meta(st, mid, info, me)
    if str(me.get("championId")) != LEBLANC_CHAMPION_ID:
        return False

    duration = int(info.get("gameDuration", 0) or 0)
    kills = int(me.get("kills", 0) or 0)
    deaths = int(me.get("deaths", 0) or 0)
    assists = int(me.get("assists", 0) or 0)
    cs = int(me.get("totalMinionsKilled", 0) or 0) + int(me.get("neutralMinionsKilled", 0) or 0)
    minutes = duration / 60 if duration > 0 else 0

    records = stats["leblancMatchStats"]
    record = records.setdefault(mid, {})
    record.update({
        "matchId": mid,
        "win": bool(me.get("win")),
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "kda": (kills + assists) / max(1, deaths),
        "cs": cs,
        "csPerMin": (cs / minutes) if minutes > 0 else None,
        "durationSeconds": duration,
        "gameEndTs": meta.get("gameEndTs"),
        "participantId": me.get("participantId"),
        "teamPosition": me.get("teamPosition"),
    })

    opponent = find_lane_opponent(participants, me)
    if opponent:
        record["opponentParticipantId"] = opponent.get("participantId")
        record["opponentChampionId"] = str(opponent.get("championId"))

    # External enrichment is intentionally failure-tolerant.
    if fetch_external:
        if record.get("aiScore") is None:
            deeplol = get_deeplol_stats(p["gameName"], p["tagLine"], mid)
            if deeplol.get("ai_score") is not None:
                record["aiScore"] = deeplol["ai_score"]
            if deeplol.get("fate") is not None:
                record["fate"] = deeplol["fate"]
            if deeplol.get("team_luck_value") is not None:
                record["teamLuckValue"] = deeplol["team_luck_value"]
            if deeplol.get("teammate_average_ai_score") is not None:
                record["teammateAverageAiScore"] = deeplol["teammate_average_ai_score"]

        if duration >= 15 * 60 and record.get("goldLead15") is None and opponent:
            try:
                timeline = get_match_timeline(mid, regional)
                lead = gold_lead_at_15(
                    timeline,
                    me.get("participantId"),
                    opponent.get("participantId"),
                )
                if lead is not None:
                    record["goldLead15"] = lead
            except Exception as error:
                print(f"[timeline] failed for {mid}: {error}")

    return True


def derive_leblanc_advanced_stats(stats):
    records = list(stats.get("leblancMatchStats", {}).values())

    stats["leblancAverageAiScore"] = average([
        safe_float(r.get("aiScore")) for r in records
        if safe_float(r.get("aiScore")) is not None
    ])

    stats["leblancAverageKda"] = average([
        safe_float(r.get("kda")) for r in records
        if safe_float(r.get("kda")) is not None
    ])

    stats["leblancAverageKills"] = average([
        safe_float(r.get("kills")) for r in records
        if safe_float(r.get("kills")) is not None
    ])
    stats["leblancAverageDeaths"] = average([
        safe_float(r.get("deaths")) for r in records
        if safe_float(r.get("deaths")) is not None
    ])
    stats["leblancAverageAssists"] = average([
        safe_float(r.get("assists")) for r in records
        if safe_float(r.get("assists")) is not None
    ])

    stats["leblancAverageGoldLead15"] = average([
        safe_float(r.get("goldLead15")) for r in records
        if safe_float(r.get("goldLead15")) is not None
    ])

    stats["leblancAverageCsPerMin"] = average([
        safe_float(r.get("csPerMin")) for r in records
        if safe_float(r.get("csPerMin")) is not None
    ])

    fate_counts = {name: 0 for name in FATE_NAMES}
    for record in records:
        fate = record.get("fate")
        if fate in fate_counts:
            fate_counts[fate] += 1
    stats["leblancFateCounts"] = fate_counts


def derive_basic_champion_stats(stats):
    champs = stats.get("champions", {})
    leblanc = champs.get(LEBLANC_CHAMPION_ID, {})

    leblanc_games = int(leblanc.get("games", 0) or 0)
    leblanc_wins = int(leblanc.get("wins", 0) or 0)
    leblanc_losses = int(leblanc.get("losses", 0) or 0)

    stats["leblancGames"] = leblanc_games
    stats["leblancWins"] = leblanc_wins
    stats["leblancLosses"] = leblanc_losses
    stats["leblancWinrate"] = (leblanc_wins / leblanc_games) if leblanc_games else None
    stats["gamesOffLeblanc"] = max(0, int(stats.get("games", 0) or 0) - leblanc_games)

    champ_rows = []
    for cid, d in champs.items():
        g = int(d.get("games", 0) or 0)
        w = int(d.get("wins", 0) or 0)
        wr = (w / g) if g else None
        champ_rows.append((cid, g, w, wr))
    champ_rows.sort(key=lambda x: x[1], reverse=True)

    stats["mostPlayedChampionId"] = champ_rows[0][0] if champ_rows else None

    eligible = [r for r in champ_rows if r[1] >= 5 and r[3] is not None]
    if len(eligible) >= 2:
        best = max(eligible, key=lambda r: (r[3], r[1]))
        worst = min(eligible, key=lambda r: (r[3], -r[1]))
        stats["highestWinrateChampionId"] = best[0]
        stats["lowestWinrateChampionId"] = worst[0]
        stats["highestWinrateChampionWR"] = best[3]
        stats["lowestWinrateChampionWR"] = worst[3]
    elif len(eligible) == 1:
        only = eligible[0]
        stats["highestWinrateChampionId"] = only[0]
        stats["lowestWinrateChampionId"] = only[0]
        stats["highestWinrateChampionWR"] = only[3]
        stats["lowestWinrateChampionWR"] = only[3]
    else:
        stats["highestWinrateChampionId"] = None
        stats["lowestWinrateChampionId"] = None
        stats["highestWinrateChampionWR"] = None
        stats["lowestWinrateChampionWR"] = None


def backfill_advanced_match_data(st, stats, p, regional):
    """Backfill KDA/CS/timestamps/AI/fate/gold15 for already-seen matches."""
    checked = 0
    enriched = 0

    # First, fill missing base records/meta from Riot match details.
    for mid in st.get("matchesSeen", []):
        if checked >= MAX_ADVANCED_MATCH_BACKFILL_PER_RUN:
            break

        meta = st.get("matchMeta", {}).get(mid)
        record = stats.get("leblancMatchStats", {}).get(mid)

        needs_basic = meta is None
        if meta and str(meta.get("championId")) == LEBLANC_CHAMPION_ID:
            needs_basic = record is None or any(
                record.get(k) is None
                for k in ("kills", "deaths", "assists", "csPerMin", "gameEndTs")
            )

        if not needs_basic:
            continue

        checked += 1
        try:
            match = get_match(mid, regional)
            if build_or_update_leblanc_record(st, stats, p, mid, match, regional, fetch_external=True):
                enriched += 1
        except Exception as error:
            print(f"[advanced backfill] failed {mid}: {error}")

    # Retry DeepLoL for LeBlanc records that were created but still have no AI score.
    deeplol_retries = 0
    for mid, record in stats.get("leblancMatchStats", {}).items():
        if deeplol_retries >= MAX_DEEPLOL_RETRIES_PER_RUN:
            break
        if record.get("aiScore") is not None:
            continue

        deeplol_retries += 1
        deeplol = get_deeplol_stats(p["gameName"], p["tagLine"], mid)
        if deeplol.get("ai_score") is not None:
            record["aiScore"] = deeplol["ai_score"]
        if deeplol.get("fate") is not None:
            record["fate"] = deeplol["fate"]
        if deeplol.get("team_luck_value") is not None:
            record["teamLuckValue"] = deeplol["team_luck_value"]
        if deeplol.get("teammate_average_ai_score") is not None:
            record["teammateAverageAiScore"] = deeplol["teammate_average_ai_score"]

    # Retry timeline only when the record has an opponent participant ID and no gold lead yet.
    gold_retries = 0
    for mid, record in stats.get("leblancMatchStats", {}).items():
        if gold_retries >= MAX_GOLD15_RETRIES_PER_RUN:
            break
        if record.get("goldLead15") is not None:
            continue
        if int(record.get("durationSeconds", 0) or 0) < 15 * 60:
            continue

        my_pid = record.get("participantId")
        opp_pid = record.get("opponentParticipantId")
        if my_pid is None or opp_pid is None:
            continue

        gold_retries += 1
        try:
            timeline = get_match_timeline(mid, regional)
            lead = gold_lead_at_15(timeline, my_pid, opp_pid)
            if lead is not None:
                record["goldLead15"] = lead
        except Exception as error:
            print(f"[timeline retry] failed {mid}: {error}")

    return checked, enriched, deeplol_retries, gold_retries


def derive_leblanc_lp_attribution(st, stats):
    """
    Attribute an LP snapshot delta to LeBlanc only when every known ranked game
    between the two rank snapshots was a LeBlanc game. This lets the existing
    D2 22 -> D3 57 interval be recovered once match timestamps are backfilled.
    """
    attributions = stats.setdefault("leblancLpAttributions", {})
    lp_history = st.get("lpHistory", [])
    match_meta = st.get("matchMeta", {})
    matches_seen = st.get("matchesSeen", [])

    # Do not attribute until every seen match has timestamp/champion metadata.
    if not matches_seen or any(mid not in match_meta for mid in matches_seen):
        stats["leblancLpNet"] = sum(
            int(v.get("delta", 0) or 0) for v in attributions.values()
        )
        tracked = set()
        for v in attributions.values():
            tracked.update(v.get("matchIds", []))
        stats["leblancLpTrackedGames"] = len(tracked)
        return

    for prev, curr in zip(lp_history, lp_history[1:]):
        prev_ts = int(prev.get("ts", 0) or 0)
        curr_ts = int(curr.get("ts", 0) or 0)
        if not prev_ts or not curr_ts or curr_ts <= prev_ts:
            continue

        key = f"{prev_ts}->{curr_ts}"
        if key in attributions:
            continue

        interval = []
        for mid in matches_seen:
            meta = match_meta.get(mid, {})
            end_ts = meta.get("gameEndTs")
            if isinstance(end_ts, (int, float)) and prev_ts < end_ts <= curr_ts:
                interval.append((mid, meta))

        if not interval:
            continue

        if not all(str(meta.get("championId")) == LEBLANC_CHAMPION_ID for _, meta in interval):
            continue

        delta = rank_value(curr.get("tier"), curr.get("division"), curr.get("lp", 0)) - rank_value(
            prev.get("tier"), prev.get("division"), prev.get("lp", 0)
        )

        attributions[key] = {
            "delta": int(delta),
            "matchIds": [mid for mid, _ in interval],
            "from": {
                "tier": prev.get("tier"),
                "division": prev.get("division"),
                "lp": prev.get("lp"),
            },
            "to": {
                "tier": curr.get("tier"),
                "division": curr.get("division"),
                "lp": curr.get("lp"),
            },
        }

    stats["leblancLpNet"] = sum(int(v.get("delta", 0) or 0) for v in attributions.values())
    tracked = set()
    for value in attributions.values():
        tracked.update(value.get("matchIds", []))
    stats["leblancLpTrackedGames"] = len(tracked)


def update_player(state, p):
    label = p["label"]
    platform = p["platform"]
    regional = p["regional"]

    players = state.setdefault("players", {})
    st, stats = ensure_player_state(players, p)
    stats["advancedTrackerVersion"] = TRACKER_SCHEMA_VERSION

    if not st["puuid"]:
        st["puuid"] = get_puuid(p["gameName"], p["tagLine"], regional)

    # Capture the previous current rank BEFORE overwriting it.
    previous_current = dict(st["current"]) if isinstance(st.get("current"), dict) else None

    entries = get_league_entries_by_puuid(st["puuid"], platform)
    if not isinstance(entries, list):
        raise RuntimeError(f"League entries lookup failed: {entries}")

    solo = get_ranked_solo_entry(entries)
    now_ts = int(time.time())
    snap = {"ts": now_ts}
    lwins = llosses = lgames = None

    if solo:
        tier = solo.get("tier")
        div = solo.get("rank")
        lp = int(solo.get("leaguePoints", 0))
        lwins = int(solo.get("wins", 0))
        llosses = int(solo.get("losses", 0))
        lgames = lwins + llosses
        lwinrate = (lwins / lgames) if lgames else None

        st["current"] = {
            "tier": tier,
            "division": div,
            "lp": lp,
            "wins": lwins,
            "losses": llosses,
            "games": lgames,
            "winrate": lwinrate,
        }
        st["leagueRecord"] = {
            "wins": lwins,
            "losses": llosses,
            "games": lgames,
            "winrate": lwinrate,
        }
        snap.update({"tier": tier, "division": div, "lp": lp})
    else:
        st["current"] = None
        st["leagueRecord"] = None

    rank_changed_this_run = False
    if "tier" in snap:
        last = st["lpHistory"][-1] if st["lpHistory"] else None
        if (not last) or (
            last.get("tier"), last.get("division"), last.get("lp")
        ) != (snap["tier"], snap["division"], snap["lp"]):
            st["lpHistory"].append(snap)
            rank_changed_this_run = True

        cur_val = rank_value(snap["tier"], snap["division"], snap["lp"])
        peak = st.get("peak")
        if (not peak) or cur_val > rank_value(peak["tier"], peak["division"], peak["lp"]):
            st["peak"] = {
                "tier": snap["tier"],
                "division": snap["division"],
                "lp": snap["lp"],
                "ts": now_ts,
            }

    seen = set(st["matchesSeen"])
    backfill = st["backfill"]
    pending = st["pendingMatchIds"]
    pending_set = set(pending)

    # 1) Historical ID scan while backfilling.
    pages_scanned = 0
    if not backfill.get("done", False):
        while pages_scanned < BACKFILL_PAGES_PER_RUN:
            start = int(backfill.get("nextStart", 0))
            ids = get_match_ids(
                st["puuid"], regional,
                start=start,
                count=MATCH_PAGE_SIZE,
                start_time=SPLIT_START_UNIX,
            )
            if not isinstance(ids, list):
                raise RuntimeError(f"Match ID lookup failed: {ids}")

            pages_scanned += 1
            if not ids:
                backfill["done"] = True
                break

            intersects_seen = any(mid in seen for mid in ids)
            for mid in ids:
                if mid not in seen and mid not in pending_set:
                    pending.append(mid)
                    pending_set.add(mid)

            backfill["nextStart"] = start + MATCH_PAGE_SIZE
            if STOP_BACKFILL_ON_FIRST_SEEN and intersects_seen:
                break

    # 2) Always scan newest IDs.
    recent_ids = get_match_ids(
        st["puuid"], regional,
        start=0,
        count=MATCH_PAGE_SIZE,
        start_time=SPLIT_START_UNIX,
    )
    if not isinstance(recent_ids, list):
        raise RuntimeError(f"Match ID lookup failed: {recent_ids}")

    for mid in recent_ids:
        if mid in seen:
            break
        if mid not in pending_set:
            pending.append(mid)
            pending_set.add(mid)

    # 3) Process unseen match details oldest -> newest.
    to_process = []
    while pending and len(to_process) < MAX_MATCH_DETAILS_PER_RUN:
        to_process.append(pending.pop())
        pending_set.discard(to_process[-1])

    rate_limit_hits = 0
    new_match_results = []

    for index, mid in enumerate(to_process):
        try:
            match = get_match(mid, regional)
        except requests.HTTPError as error:
            msg = str(error)
            if "429" in msg:
                rate_limit_hits += 1
                if rate_limit_hits >= RATE_LIMIT_HIT_LIMIT:
                    print("Too many 429s; stopping match processing early.")
                    for back_mid in reversed(to_process[index:]):
                        if back_mid not in seen and back_mid not in pending_set:
                            pending.append(back_mid)
                            pending_set.add(back_mid)
                    break

            print(f"Skipping match due to HTTPError: {mid} -> {error}")
            if mid not in seen and mid not in pending_set:
                pending.append(mid)
                pending_set.add(mid)
            continue

        info = match.get("info", {})
        participants = info.get("participants", [])
        me = next((x for x in participants if x.get("puuid") == st["puuid"]), None)
        if not me:
            if mid not in seen and mid not in pending_set:
                pending.append(mid)
                pending_set.add(mid)
            continue

        win = bool(me.get("win"))
        champ_id = str(me.get("championId"))
        duration = int(info.get("gameDuration", 0) or 0)

        stats["games"] = int(stats.get("games", 0) or 0) + 1
        if win:
            stats["wins"] = int(stats.get("wins", 0) or 0) + 1
        else:
            stats["losses"] = int(stats.get("losses", 0) or 0) + 1

        stats["totalPlaytimeSeconds"] = int(stats.get("totalPlaytimeSeconds", 0) or 0) + max(duration, 0)

        champ = stats["champions"].setdefault(
            champ_id,
            {"games": 0, "wins": 0, "losses": 0, "playtimeSeconds": 0},
        )
        champ["games"] = int(champ.get("games", 0) or 0) + 1
        if win:
            champ["wins"] = int(champ.get("wins", 0) or 0) + 1
        else:
            champ["losses"] = int(champ.get("losses", 0) or 0) + 1
        champ["playtimeSeconds"] = int(champ.get("playtimeSeconds", 0) or 0) + max(duration, 0)

        write_basic_match_meta(st, mid, info, me)
        if champ_id == LEBLANC_CHAMPION_ID:
            build_or_update_leblanc_record(st, stats, p, mid, match, regional, fetch_external=True)

        st["matchesSeen"].append(mid)
        seen.add(mid)
        new_match_results.append({
            "matchId": mid,
            "win": win,
            "championId": champ_id,
        })

    stats["splitMatchesProcessed"] = len(st["matchesSeen"])
    stats["totalPlaytimeHMS"] = seconds_to_hms(stats.get("totalPlaytimeSeconds", 0))

    derive_basic_champion_stats(stats)

    # Backfill the advanced data missing from the old updater.
    advanced_checked, advanced_enriched, deeplol_retries, gold_retries = backfill_advanced_match_data(
        st, stats, p, regional
    )

    derive_leblanc_advanced_stats(stats)
    derive_leblanc_lp_attribution(st, stats)

    # Preserve the original generic best-effort LP averages, but fix division arithmetic
    # and only attribute a newly observed delta once, on the same run that the rank changed.
    if rank_changed_this_run and previous_current and st.get("current") and len(new_match_results) == 1:
        lp_delta = rank_value(
            st["current"].get("tier"),
            st["current"].get("division"),
            st["current"].get("lp", 0),
        ) - rank_value(
            previous_current.get("tier"),
            previous_current.get("division"),
            previous_current.get("lp", 0),
        )

        if new_match_results[0]["win"]:
            stats["lpDelta"]["wins"].append(lp_delta)
        else:
            stats["lpDelta"]["losses"].append(abs(lp_delta))

    wins = stats["lpDelta"].get("wins", [])
    losses = stats["lpDelta"].get("losses", [])
    stats["avgLpGainPerWin"] = (sum(wins) / len(wins)) if wins else None
    stats["avgLpLossPerLoss"] = (sum(losses) / len(losses)) if losses else None

    newest = recent_ids[0] if recent_ids else None
    newest_is_seen = bool(recent_ids and recent_ids[0] in set(st["matchesSeen"]))
    records = list(stats.get("leblancMatchStats", {}).values())
    ai_count = sum(1 for r in records if r.get("aiScore") is not None)
    gold_count = sum(1 for r in records if r.get("goldLead15") is not None)

    print(f"[{label}] SOLO ladder games now={lgames if solo else 'n/a'} W={lwins if solo else 'n/a'} L={llosses if solo else 'n/a'}")
    print(f"[{label}] seen_total={len(st['matchesSeen'])} new_details_processed_this_run={len(new_match_results)}")
    print(f"[{label}] pending={len(st['pendingMatchIds'])} backfill_done={backfill.get('done')} nextStart={backfill.get('nextStart')} pages_scanned={pages_scanned}")
    print(f"[{label}] fetched_ids={len(recent_ids)} newest={newest} newest_is_seen={newest_is_seen}")
    print(
        f"[{label}] LeBlanc games={stats.get('leblancGames', 0)} "
        f"advanced_records={len(records)} AI_scores={ai_count} gold15_games={gold_count} "
        f"advanced_checks={advanced_checked} enriched={advanced_enriched} "
        f"deeplol_retries={deeplol_retries} timeline_retries={gold_retries} "
        f"LP_net={stats.get('leblancLpNet')} LP_tracked_games={stats.get('leblancLpTrackedGames')}"
    )


def main():
    state = load_state()
    state["trackerSchemaVersion"] = TRACKER_SCHEMA_VERSION
    state.setdefault(
        "split",
        {
            "queue": QUEUE_RANKED_SOLO,
            "queueType": QUEUE_TYPE_SOLO,
            "startUnix": SPLIT_START_UNIX,
        },
    )

    # Permanently remove stale player labels from old versions of the tracker.
    active_labels = {p["label"] for p in PLAYERS}
    state["players"] = {
        label: player_data
        for label, player_data in state.get("players", {}).items()
        if label in active_labels
    }
    state["activePlayerLabels"] = [p["label"] for p in PLAYERS]

    for p in PLAYERS:
        update_player(state, p)

    now_ny = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    state["updatedAt"] = {
        "iso": now_ny.isoformat(),
        "display": now_ny.strftime("Last Updated: %-I:%M%p on %-m/%-d"),
    }

    save_state(state)

    # Loud post-save diagnostics so CI cannot silently publish an old/basic schema.
    for label, player in state.get("players", {}).items():
        stats = player.get("stats", {})
        records = stats.get("leblancMatchStats", {})
        print(
            f"[schema] {label}: version={stats.get('advancedTrackerVersion')} "
            f"LB_games={stats.get('leblancGames')} records={len(records)} "
            f"avgKDA={stats.get('leblancAverageKda')} "
            f"avgCS={stats.get('leblancAverageCsPerMin')} "
            f"avgAI={stats.get('leblancAverageAiScore')} "
            f"avgGold15={stats.get('leblancAverageGoldLead15')} "
            f"LP={stats.get('leblancLpNet')} trackedLPgames={stats.get('leblancLpTrackedGames')}"
        )


if __name__ == "__main__":
    main()
