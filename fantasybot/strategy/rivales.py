"""Rival budget, transfer accounting & squad clause tracker.

Combines:
  1) Persistent transaction history (.state/activity_history.json) to track market sales, purchases,
     manager-to-manager transfers, and matchday prize payouts.
  2) Real-time squad snapshots with player market values, buyout clauses, and protection levels.
"""

from typing import Any, Dict, List, Optional
from ..matching import POS
from .. import state

# Activity type IDs from LaLiga Fantasy API:
# 31 = market purchase (user1Id buys player from market)
# 33 = market sale (user1Id sells player to market)
# 1 = direct transfer / buyout (user1Id buys from user2Id)
# 6 = matchday reward / prize (user1Id receives prize for weekNumber)
TYPE_MARKET_BUY = 31
TYPE_MARKET_SELL = 33
TYPE_DIRECT_TRANSFER = 1
TYPE_MATCHDAY_REWARD = 6


def parse_activity(activity_feed: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Aggregates all transactions by manager ID (user ID).

    Returns a dict mapping manager_id (int) to their cash flow metrics:
      {
        manager_id: {
          "purchases": int,
          "sales": int,
          "prizes": int,
          "transactions_count": int,
          "net_profit": int,
        }
      }
    """
    stats: Dict[int, Dict[str, Any]] = {}

    def _get_entry(uid: Optional[int]) -> Optional[Dict[str, Any]]:
        if not uid:
            return None
        try:
            uid_int = int(uid)
        except (ValueError, TypeError):
            return None
        if uid_int not in stats:
            stats[uid_int] = {
                "purchases": 0,
                "sales": 0,
                "prizes": 0,
                "transactions_count": 0,
                "net_profit": 0,
            }
        return stats[uid_int]

    for act in activity_feed or []:
        atype = act.get("activityTypeId")
        amount = act.get("amount") or 0
        u1 = act.get("user1Id")
        u2 = act.get("user2Id")

        e1 = _get_entry(u1)
        if e1 is not None:
            e1["transactions_count"] += 1

        if atype == TYPE_MARKET_BUY:
            if e1 is not None:
                e1["purchases"] += amount
        elif atype == TYPE_MARKET_SELL:
            if e1 is not None:
                e1["sales"] += amount
        elif atype == TYPE_DIRECT_TRANSFER:
            # u1 buys from u2
            if e1 is not None:
                e1["purchases"] += amount
            e2 = _get_entry(u2)
            if e2 is not None:
                e2["sales"] += amount
                e2["transactions_count"] += 1
        elif atype == TYPE_MATCHDAY_REWARD:
            if e1 is not None:
                e1["prizes"] += amount

    for entry in stats.values():
        entry["net_profit"] = entry["sales"] + entry["prizes"] - entry["purchases"]

    return stats


def autocalibrate_initial_cash(
    teams: List[Dict[str, Any]],
    flow_by_user: Dict[int, Dict[str, Any]],
    fallback: int = 15_000_000
) -> int:
    """Derives baseline cash from the authenticated user's exact balance."""
    for t in teams:
        if t.get("teamMoney") is not None:
            mid = t.get("managerId") or (t.get("manager") or {}).get("id")
            try:
                mid_int = int(mid) if mid is not None else None
            except (ValueError, TypeError):
                mid_int = None

            u_flow = flow_by_user.get(mid_int, {"purchases": 0, "sales": 0, "prizes": 0})
            calc_init = t["teamMoney"] - u_flow["sales"] - u_flow["prizes"] + u_flow["purchases"]
            return max(0, calc_init)
    return fallback


def analyze_squad_clauses(players: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Finds top protected player, maximum clause, and total squad clause value."""
    max_clause = 0
    max_clause_player = None
    max_inv = 0
    top_protected = None
    total_clause = 0

    for p in players:
        pm = p.get("playerMaster") or {}
        clause = p.get("buyoutClause") or 0
        mv = pm.get("marketValue") or 0
        pos = POS.get(pm.get("positionId"), "?")
        name = pm.get("nickname") or pm.get("name") or "Unknown"

        total_clause += clause
        if clause > max_clause:
            max_clause = clause
            max_clause_player = {
                "player_id": pm.get("id"),
                "name": name,
                "pos": pos,
                "buyout_clause": clause,
            }

        inv = max(0, clause - mv)
        if inv > max_inv:
            max_inv = inv
            top_protected = {
                "player_id": pm.get("id"),
                "name": name,
                "pos": pos,
                "market_value": mv,
                "buyout_clause": clause,
                "invested": inv,
            }

    return {
        "max_clause_player": max_clause_player,
        "top_protected": top_protected,
        "total_clause": total_clause,
    }


def analyze_player_acquisitions(
    players: List[Dict[str, Any]],
    manager_id: Optional[int],
    activity_history: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Enriches each player in a squad with purchase price, buy date, and capital gain/loss."""
    buy_map = {}
    if manager_id is not None:
        for a in activity_history or []:
            if (a.get("activityTypeId") in (TYPE_MARKET_BUY, TYPE_DIRECT_TRANSFER)
                    and a.get("user1Id") is not None
                    and int(a.get("user1Id")) == manager_id):
                pid = str(a.get("playerMasterId"))
                buy_map[pid] = {
                    "amount": a.get("amount") or 0,
                    "date": str(a.get("createdAt") or "")[:10],
                }

    enriched = []
    for p in players or []:
        pm = p.get("playerMaster") or {}
        pid = str(pm.get("id"))
        pname = pm.get("nickname") or pm.get("name") or "Unknown"
        pos = POS.get(pm.get("positionId"), "?")
        mv = pm.get("marketValue") or 0
        clause = p.get("buyoutClause") or 0
        protection = max(0, clause - mv)

        buy_info = buy_map.get(pid)
        if buy_info:
            bought_price = buy_info["amount"]
            bought_date = buy_info["date"]
            diff = mv - bought_price
            diff_pct = (diff / bought_price) * 100 if bought_price else 0
            is_initial = False
        else:
            bought_price = None
            bought_date = None
            diff = 0
            diff_pct = 0
            is_initial = True

        enriched.append({
            "player_id": pid,
            "name": pname,
            "pos": pos,
            "market_value": mv,
            "buyout_clause": clause,
            "protection": protection,
            "bought_price": bought_price,
            "bought_date": bought_date,
            "diff": diff,
            "diff_pct": diff_pct,
            "is_initial": is_initial,
        })

    enriched.sort(key=lambda x: -x["market_value"])
    return enriched


DEFAULT_INITIAL_BUDGET = 100_000_000


def analyze_rivals(
    client,
    league_id: str,
    initial_budget: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Fetches teams and merges league activity into persistent history to calculate metrics."""
    teams = client.league_teams(league_id) or []
    activity_live = client.league_activity(league_id) or []

    # Accumulate into persistent history (.state/activity_history.json)
    activity_cumulative = state.record_activity(activity_live, league_id)
    flow_by_user = parse_activity(activity_cumulative)

    initial_cash = DEFAULT_INITIAL_BUDGET if initial_budget is None else initial_budget

    oldest_ts = activity_cumulative[0].get("createdAt") if activity_cumulative else None
    newest_ts = activity_cumulative[-1].get("createdAt") if activity_cumulative else None
    oldest_date = oldest_ts[:10] if oldest_ts and len(oldest_ts) >= 10 else None
    newest_date = newest_ts[:10] if newest_ts and len(newest_ts) >= 10 else None

    rivals = []
    for t in teams:
        manager = t.get("manager") or {}
        manager_id = t.get("managerId") or manager.get("id")
        try:
            mid_int = int(manager_id) if manager_id is not None else None
        except (ValueError, TypeError):
            mid_int = None

        m_flow = flow_by_user.get(mid_int, {
            "purchases": 0,
            "sales": 0,
            "prizes": 0,
            "transactions_count": 0,
            "net_profit": 0,
        })

        players = t.get("players") or []
        clause_info = analyze_squad_clauses(players)

        purchases = m_flow["purchases"]
        sales = m_flow["sales"]
        prizes = m_flow["prizes"]
        net_profit = m_flow["net_profit"]

        est_balance = max(0, initial_cash + net_profit)
        known_balance = t.get("teamMoney")

        rivals.append({
            "manager_id": mid_int,
            "manager_name": manager.get("managerName") or "Unknown",
            "team_id": t.get("id"),
            "position": t.get("position") or 0,
            "points": t.get("teamPoints") or 0,
            "team_value": t.get("teamValue") or 0,
            "total_clause": clause_info["total_clause"],
            "players_count": len(players),
            "purchases": purchases,
            "sales": sales,
            "prizes": prizes,
            "net_profit": net_profit,
            "initial_cash": initial_cash,
            "estimated_balance": est_balance,
            "known_balance": known_balance,
            "is_me": known_balance is not None,
            "top_protected": clause_info["top_protected"],
            "max_clause_player": clause_info["max_clause_player"],
            "players": analyze_player_acquisitions(players, mid_int, activity_cumulative),
            "transactions_count": m_flow["transactions_count"],
            "tracked_events_count": len(activity_cumulative),
            "tracked_from_date": oldest_date,
            "tracked_to_date": newest_date,
        })

    rivals.sort(key=lambda r: (r["position"] if r["position"] > 0 else 999, -(r["points"] or 0)))
    return rivals
