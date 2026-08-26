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
from .sources import value_history

STATE_DIR = os.environ.get("FANTASY_STATE_DIR") or os.path.join(config.ROOT, ".state")
SNAPSHOT_PATH = os.path.join(STATE_DIR, "snapshot.json")
TASKS_PATH = os.path.join(STATE_DIR, "tasks.json")
REMINDERS_PATH = os.path.join(STATE_DIR, "reminders.json")
BIDS_PATH = os.path.join(STATE_DIR, "bids.json")
BID_PLAN_PATH = os.path.join(STATE_DIR, "bid_plan.json")
RIVALS_SNAPSHOT_PATH = os.path.join(STATE_DIR, "rivals_snapshot.json")
ACTIVITY_HISTORY_PATH = os.path.join(STATE_DIR, "activity_history.json")
VALUE_HISTORY_DIR = os.path.join(STATE_DIR, "value_history")
SQUAD_HISTORY_PATH = os.path.join(STATE_DIR, "squad_history.json")
PLAYERS_CACHE_PATH = os.path.join(STATE_DIR, "players_cache.json")


def load_players_cache() -> dict:
    return _read(PLAYERS_CACHE_PATH, {})


def save_players_cache(cache: dict):
    _write(PLAYERS_CACHE_PATH, cache)


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


def _read(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


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
    """Add a task. `key` prevents duplicates (same key = not repeated).

    A re-added key REFRESHES text and due date: a task's identity is its key, but
    its content follows the world — when a target's acquisition route changes
    (clause -> open sale), keeping the old wording pinned a stale price and a
    stale deadline on screen."""
    tasks = load_tasks()
    if key and any(t.get("key") == key for t in tasks):
        task = next(t for t in tasks if t.get("key") == key)
        if task.get("text") != text or task.get("due") != due:
            task["text"], task["due"] = text, due
            save_tasks(tasks)
        return task
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


# --- rival snapshots & clause increases ---
def snapshot_rivals(teams: list) -> dict:
    """Takes a snapshot of all teams' player clauses: {manager_name: {player_id: {name, clause}}}."""
    snap = {
        "ts": int(time.time()),
        "managers": {},
    }
    for t in teams or []:
        mgr_name = (t.get("manager") or {}).get("managerName") or str(t.get("managerId") or "Unknown")
        player_clauses = {}
        for p in t.get("players") or []:
            pm = p.get("playerMaster") or {}
            pid = str(pm.get("id"))
            pname = pm.get("nickname") or pm.get("name") or "Unknown"
            clause = p.get("buyoutClause") or 0
            player_clauses[pid] = {"name": pname, "clause": clause}
        snap["managers"][mgr_name] = player_clauses
    return snap


def load_rivals_snapshot() -> dict:
    return _read(RIVALS_SNAPSHOT_PATH, {})


def save_rivals_snapshot(snap: dict):
    _write(RIVALS_SNAPSHOT_PATH, snap)


def save_value_snapshot(day_iso: str, snapshot: dict) -> str:
    """Bank one day's OFFICIAL market values ({playerMasterId: {v, s}}, from
    value_history.snapshot_from_players) under VALUE_HISTORY_DIR. Returns the path written."""
    return value_history.save_snapshot(VALUE_HISTORY_DIR, day_iso, snapshot)


def load_value_trend(player_id, days_back, today_iso=None):
    """A player's official value trend vs `days_back` days ago, or None if not enough
    history has been banked yet. `today_iso` defaults to today (Spain-agnostic UTC date —
    a one-day skew around midnight doesn't matter for a multi-day trend)."""
    from datetime import date
    today_iso = today_iso or date.today().isoformat()
    return value_history.trend(VALUE_HISTORY_DIR, player_id, today_iso, days_back)


def diff_rival_clauses(prev: dict, curr: dict) -> list:
    """Detects clause increases across managers between snapshots: [{manager, name, old_clause, new_clause, delta}]."""
    if not prev or "managers" not in prev or not curr or "managers" not in curr:
        return []
    increases = []
    prev_mgrs = prev.get("managers", {})
    curr_mgrs = curr.get("managers", {})
    for mgr, curr_players in curr_mgrs.items():
        prev_players = prev_mgrs.get(mgr, {})
        for pid, pdata in curr_players.items():
            if pid in prev_players:
                old_c = prev_players[pid].get("clause") or 0
                new_c = pdata.get("clause") or 0
                if new_c > old_c:
                    increases.append({
                        "manager": mgr,
                        "player_id": pid,
                        "name": pdata.get("name"),
                        "old_clause": old_c,
                        "new_clause": new_c,
                        "delta": new_c - old_c,
                    })
    return increases


# --- cumulative transaction & squad history ---
def load_activity_history(league_id: str | None = None) -> list:
    """Loads all accumulated transactions."""
    raw = _read(ACTIVITY_HISTORY_PATH, {})
    if league_id:
        return raw.get(str(league_id), [])
    # Return all if no league_id given
    items = []
    for lg_items in raw.values():
        items.extend(lg_items)
    return items


def record_activity(new_items: list, league_id: str) -> list:
    """Merges new activity items into persistent storage without duplicates.

    Returns the complete chronological history for the league.
    """
    raw = _read(ACTIVITY_HISTORY_PATH, {})
    lg_key = str(league_id)
    known = {}
    for item in raw.get(lg_key, []):
        iid = str(item.get("id") or f"{item.get('activityTypeId')}_{item.get('user1Id')}_{item.get('user2Id')}_{item.get('playerMasterId')}_{item.get('amount')}_{item.get('createdAt')}")
        known[iid] = item

    for item in new_items or []:
        iid = str(item.get("id") or f"{item.get('activityTypeId')}_{item.get('user1Id')}_{item.get('user2Id')}_{item.get('playerMasterId')}_{item.get('amount')}_{item.get('createdAt')}")
        known[iid] = item

    merged = sorted(known.values(), key=lambda x: str(x.get("createdAt") or x.get("id") or ""))
    raw[lg_key] = merged
    _write(ACTIVITY_HISTORY_PATH, raw)
    return merged


def load_squad_history(league_id: str) -> dict:
    """Returns {manager_id: {player_id: {bought_price, first_seen, ...}}}."""
    raw = _read(SQUAD_HISTORY_PATH, {})
    return raw.get(str(league_id), {})


def save_squad_history(history: dict, league_id: str):
    raw = _read(SQUAD_HISTORY_PATH, {})
    raw[str(league_id)] = history
    _write(SQUAD_HISTORY_PATH, raw)


