"""Persistent agent state: team snapshot + task list.

Enables two things that make the agent feel human:
  1) Detect WHAT HAS CHANGED since the last connection (someone got signed away
     from you, your balance went up/down, money came in from a sale...).
  2) Keep a self-updating list of the week's pending items (you're missing a
     goalkeeper, you need to bid on X before the market closes...).

Stored in `.state/` (gitignored). No sensitive data.
"""

import json
import os
import time

from . import config

STATE_DIR = os.path.join(config.ROOT, ".state")
SNAPSHOT_PATH = os.path.join(STATE_DIR, "snapshot.json")
TASKS_PATH = os.path.join(STATE_DIR, "tasks.json")
REMINDERS_PATH = os.path.join(STATE_DIR, "reminders.json")
BIDS_PATH = os.path.join(STATE_DIR, "bids.json")
BID_PLAN_PATH = os.path.join(STATE_DIR, "bid_plan.json")
SOLD_PATH = os.path.join(STATE_DIR, "sold.json")
ACTIVITY_PATH = os.path.join(STATE_DIR, "activity_history.json")
PLAYERS_CACHE_PATH = os.path.join(STATE_DIR, "players_cache.json")


def load_bids() -> dict:
    """Bids placed by the agent: {market_id: {bid_id, amount, nombre}}."""
    return _read(BIDS_PATH, {})


def save_bids(bids: dict):
    _write(BIDS_PATH, bids)


# --- last-minute bid plan (targets a cron job will close out at market close) ---
def load_bid_plan() -> list:
    """Bid targets for market close: [{market_id, max_bid}]."""
    return _read(BID_PLAN_PATH, [])


def add_bid_target(market_id: str, max_bid: int, nombre: str | None = None):
    plan = [t for t in load_bid_plan() if t["market_id"] != market_id]
    entry = {"market_id": market_id, "max_bid": max_bid}
    if nombre:
        entry["nombre"] = nombre  # store the player name so displays never guess it
    plan.append(entry)
    _write(BID_PLAN_PATH, plan)


def clear_bid_plan():
    _write(BID_PLAN_PATH, [])


# --- players listed for sale by the agent (autonomous selling) ---
def load_sold() -> dict:
    """Players the agent has already listed for sale: {player_id: {nombre, sale_price}}.

    Prevents re-listing the same player on every run while the sale is pending.
    """
    return _read(SOLD_PATH, {})


def save_sold(sold: dict):
    _write(SOLD_PATH, sold)


# --- league activity history (for strategy/historia.py trading P&L) ---
#
# CAVEAT: .state/ does NOT persist between GitHub Actions runs (fresh
# checkout each time). This means record_activity() can only accumulate
# across events returned by a SINGLE live call, not across days/runs, unless
# the activity endpoint itself returns the full historical log (not just
# recent events). If it only returns recent activity, the accumulated
# "cumulative" history will effectively reset on every run -- something to
# verify once we confirm which endpoint/shape the real activity feed has.
def record_activity(activity_live: list, league_id: str) -> list:
    """Merges freshly-fetched activity events into the locally accumulated
    history (deduped by id), and returns the full accumulated list sorted by
    date. Best-effort: see caveat above about GH Actions state not persisting.
    """
    existing = _read(ACTIVITY_PATH, {})
    bucket = existing.get(str(league_id), [])
    seen_ids = {e.get("id") for e in bucket if e.get("id") is not None}
    for ev in activity_live or []:
        eid = ev.get("id")
        if eid is None or eid not in seen_ids:
            bucket.append(ev)
            if eid is not None:
                seen_ids.add(eid)
    bucket.sort(key=lambda e: str(e.get("createdAt") or ""))
    existing[str(league_id)] = bucket
    _write(ACTIVITY_PATH, existing)
    return bucket


def load_players_cache() -> dict:
    """Cached mapping playerMasterId -> {name, pos, market_value}, used by
    historia.py to avoid refetching player details repeatedly."""
    return _read(PLAYERS_CACHE_PATH, {})


def save_players_cache(cache: dict):
    _write(PLAYERS_CACHE_PATH, cache)


def _read(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(path, value):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


# --- team snapshot ---
def snapshot(team) -> dict:
    return {
        "ts": int(time.time()),
        "money": team.get("teamMoney"),
        "squad": {p["playerMaster"]["id"]: (p["playerMaster"].get("nickname")
                  or p["playerMaster"].get("name"))
                  for p in team["players"]},
    }


def load_snapshot() -> dict:
    return _read(SNAPSHOT_PATH, {})


def save_snapshot(snap: dict):
    _write(SNAPSHOT_PATH, snap)


def diff_snapshots(prev: dict, curr: dict) -> dict:
    """Changes between two snapshots: players in/out and balance variation."""
    if not prev:
        return {"first_run": True, "added": [], "removed": [], "money_delta": 0}
    prev_sq, curr_sq = prev.get("squad", {}), curr.get("squad", {})
    added = [curr_sq[i] for i in curr_sq if i not in prev_sq]
    removed = [prev_sq[i] for i in prev_sq if i not in curr_sq]
    money_delta = (curr.get("money") or 0) - (prev.get("money") or 0)
    return {"first_run": False, "added": added, "removed": removed,
            "money_delta": money_delta}


# --- task list ---
def load_tasks() -> list:
    return _read(TASKS_PATH, [])


def save_tasks(tasks: list):
    _write(TASKS_PATH, tasks)


def _next_id(tasks):
    return (max([t["id"] for t in tasks], default=0)) + 1


def add_task(text: str, due=None, key=None) -> dict:
    """Add a task. `key` prevents duplicates (same key = not repeated)."""
    tasks = load_tasks()
    if key and any(t.get("key") == key for t in tasks):
        return next(t for t in tasks if t.get("key") == key)
    task = {"id": _next_id(tasks), "text": text, "due": due, "key": key,
            "done": False, "created": int(time.time())}
    tasks.append(task)
    save_tasks(tasks)
    return task


def expire_tasks(prefix: str, max_age_days: int, now=None) -> int:
    """Drop tasks whose key starts with `prefix` and were created > max_age_days ago.
    Returns how many were removed. For recommendation tasks (e.g. "llm-rec:*") that have
    no natural completion signal and would otherwise accumulate indefinitely."""
    now = int(time.time()) if now is None else int(now)
    cutoff = now - max_age_days * 86400
    tasks = load_tasks()
    kept = [t for t in tasks
            if not (str(t.get("key", "")).startswith(prefix)
                    and (t.get("created") or 0) < cutoff)]
    removed = len(tasks) - len(kept)
    if removed:
        save_tasks(kept)
    return removed


def complete_task(task_id: int):
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["done"] = True
    save_tasks(tasks)


def complete_by_key(key: str):
    tasks = load_tasks()
    changed = False
    for t in tasks:
        if t.get("key") == key and not t["done"]:
            t["done"] = True
            changed = True
    if changed:
        save_tasks(tasks)


def complete_missing(prefix: str, keep_keys) -> None:
    """Close tasks whose key starts with `prefix` and is NOT in `keep_keys`.

    Lets tasks for targets that no longer apply (e.g. a buyout whose player left
    the market) close themselves.
    """
    keep = set(keep_keys)
    tasks = load_tasks()
    changed = False
    for t in tasks:
        k = t.get("key") or ""
        if k.startswith(prefix) and k not in keep and not t["done"]:
            t["done"] = True
            changed = True
    if changed:
        save_tasks(tasks)


def pending_tasks() -> list:
    return [t for t in load_tasks() if not t["done"]]


# --- reminders (to fire with an external scheduler) ---
def save_reminders(reminders: list):
    """Save reminders, preserving the 'fired' flag of already-known ones."""
    old = {r["key"]: r for r in load_reminders()}
    for r in reminders:
        r["fired"] = old.get(r["key"], {}).get("fired", False)
    _write(REMINDERS_PATH, reminders)


def load_reminders() -> list:
    return _read(REMINDERS_PATH, [])


def due_reminders(now) -> list:
    """Reminders whose fire time has passed and haven't fired yet.

    `now` is a timezone-aware datetime. Compares datetimes (not strings) to avoid
    timezone mix-ups.
    """
    from datetime import datetime
    out = []
    for r in load_reminders():
        if r.get("fired"):
            continue
        try:
            fire_at = datetime.fromisoformat(r["fire_at"])
        except (TypeError, ValueError):
            continue
        if fire_at <= now:
            out.append(r)
    return out


def mark_reminder_fired(key: str):
    reminders = load_reminders()
    for r in reminders:
        if r["key"] == key:
            r["fired"] = True
    _write(REMINDERS_PATH, reminders)
