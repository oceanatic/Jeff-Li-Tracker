# Emerald Race — Jam vs Antoñio

A static GitHub Pages dashboard (`docs/index.html`) and a Python/CircleCI collector (`scripts/update.py`) tracking **NA ranked solo/duo** games for:

- **Jam#0304** — climbs **up** to Emerald.
- **Antoñio#9401** — descends **down** to Emerald.

Tracking cutoff: **`1792307345` Unix seconds = October 18, 2026, 07:09:05 UTC (03:09:05 EDT)**. Only ranked-solo (queue 420) matches that **started** on or after that time count. No pre-cutoff match results, previous-player history, or old LeBlanc stats are imported. The player's starting rank is the **first rank observed by a successful run after the cutoff** (Riot's API does not expose historical rank snapshots).

## Data tracked

- Total ranked games, wins/losses, win rate, and summed game time.
- Most-played champion and top champions.
- Mean per-game KDA (kills + assists, divided by `max(deaths, 1)`).
- Mean per-game CS/minute (lane + neutral CS / duration in minutes).
- Mean DeepLoL AI scores **in wins** and **in losses**, with sample counts.
- Mean **gold difference at 15:00 vs the matched opposing laner**, **excluding support** and unknown roles; only valid 15:00 timeline frames are counted. Displays the sample count.
- Estimated fate counts (Godlike / Solid / Balanced / Messy / Doomed) calculated using the original tracker formula from DeepLoL teammates' AI scores. These are **not guaranteed to be official DeepLoL fate categories**. Unavailable cases are counted explicitly.
- Role distribution: Top, Jungle, Mid, Bot, Support, Unknown.
- Live solo rank, approximate LP-equivalent distance to *either edge* of Emerald, and the Emerald-centered progress race: Jam left-to-right, Antoñio right-to-left. It resets the visual baseline using each player's initial rank; the display **does not** claim exact LP needed for demotions/promotions, which can be non-linear.

DeepLoL uses an **unofficial third-party cached endpoint**. AI/fate data may be temporarily or permanently unavailable; missing values are never substituted with zero. Third-party stats are retried periodically. All Riot-based match statistics keep working if DeepLoL cannot supply data.

## Deployment / update setup

1. Push the modified project to your GitHub repository with **GitHub Pages set to publish from `/docs` on `main`**. Until the collector first executes, the dashboard displays `Awaiting rank` and blank/zero statistics; **no current ranks are invented**.
2. In the existing CircleCI project's Environment Variables, configure a valid `RIOT_API_KEY` from the [Riot Developer Portal](https://developer.riotgames.com/) (keep the key secret; never place it in `docs/`). Personal development keys expire, so a long-running tracker needs a renewed/appropriate key.
3. Preserve or update the existing CircleCI SSH deploy key (the fingerprint in `.circleci/config.yml` is copied from your project) so CircleCI can push `docs/data.json` back to `main`.
4. Trigger the **CircleCI pipeline with parameter `run_tracker=true`** to collect data now, and configure a **scheduled pipeline with `run_tracker=true`** (e.g. every 15 minutes) for automatic updates. The old project used this gated scheduler; merely pushing code does **not** execute the collector. Each run may take time for pending matches or API cooldowns.
5. Open `https://<username>.github.io/<repository>/` or locally serve the docs directory (`python -m http.server 8000 --directory docs`) and visit `http://localhost:8000/`.

To run manually:

```bash
pip install -r requirements.txt
# Windows PowerShell: $env:RIOT_API_KEY="RGAPI-..."
# Bash: export RIOT_API_KEY="RGAPI-..."
python scripts/update.py
```

The updater deliberately starts a fresh state when it sees the old schema or a different cutoff. Do not overwrite the newly initialized `docs/data.json` with the old one. It saves state atomically and deduplicates by match ID. A failed upstream API request for one player does not prevent the other player's completed data from being saved (CI reports failure so it is visible).

## Local verification

```bash
python -m unittest discover -s tests -v
```

Requires Python 3.12+ and `requests`. Real-time ranks and AI scores **cannot be included in this archive** because there is no configured Riot API key in the build environment. The included `docs/data.json` is an empty baseline for the requested cutoff.
