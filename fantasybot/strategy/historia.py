"""Manager trading history, portfolio P&L, completed flips & ROI tracker.

Tracks:
  1) Completed Flips (closed positions): Buy & Sell matching (FIFO) with Realized P&L and ROI %.
  2) Open Holdings (current squad purchases): Unrealized P&L and ROI % vs live market value.
  3) Initial Squad Liquidations: Revenue from selling players assigned on day 1.
"""

from typing import Any, Dict, List, Optional
from collections import defaultdict
from datetime import datetime
from ..matching import POS
from .. import state

# Activity type IDs:
TYPE_MARKET_BUY = 31
TYPE_MARKET_SELL = 33
TYPE_DIRECT_TRANSFER = 1
TYPE_MATCHDAY_REWARD = 6


def resolve_player_names(
    client,
    activity_history: List[Dict[str, Any]],
    teams: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Dict[str, Any]]:
    """Builds a cached mapping from playerMasterId (str) to {name, pos, market_value}."""
    cache = state.load_players_cache() or {}

    # Seed from current teams if provided
    for t in teams or []:
        for p in t.get("players", []):
            pm = p.get("playerMaster") or {}
            pid = str(pm.get("id"))
            if pid:
                cache[pid] = {
                    "name": pm.get("nickname") or pm.get("name") or f"Player #{pid}",
                    "pos": POS.get(pm.get("positionId"), "?"),
                    "market_value": pm.get("marketValue") or 0,
                }

    # Find any missing playerMasterIds from activity
    needed_pids = set()
    for a in activity_history or []:
        pid = a.get("playerMasterId")
        if pid and str(pid) not in cache:
            needed_pids.add(str(pid))

    if needed_pids:
        for pid in needed_pids:
            try:
                r = client.get(f"/v1/competition/1/player/{pid}?x-lang=es")
                cache[pid] = {
                    "name": r.get("nickname") or r.get("name") or f"Player #{pid}",
                    "pos": POS.get(r.get("positionId"), "?"),
                    "market_value": r.get("marketValue") or 0,
                }
            except Exception:
                cache[pid] = {"name": f"Player #{pid}", "pos": "?", "market_value": 0}
        state.save_players_cache(cache)

    return cache


def compute_manager_trading_history(
    activity_history: List[Dict[str, Any]],
    manager_id: int,
    player_names: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Computes completed flips, open holdings, initial squad sales, and P&L statistics."""
    user_events = []
    for a in activity_history or []:
        atype = a.get("activityTypeId")
        u1 = a.get("user1Id")
        u2 = a.get("user2Id")
        pid = str(a.get("playerMasterId") or "")
        amt = a.get("amount") or 0
        dt = str(a.get("createdAt") or "")[:10]

        if not pid:
            continue

        try:
            u1_int = int(u1) if u1 is not None else None
            u2_int = int(u2) if u2 is not None else None
        except (ValueError, TypeError):
            continue

        if atype in (TYPE_MARKET_BUY, TYPE_DIRECT_TRANSFER) and u1_int == manager_id:
            user_events.append({"action": "BUY", "pid": pid, "amount": amt, "date": dt})
        elif atype == TYPE_MARKET_SELL and u1_int == manager_id:
            user_events.append({"action": "SELL", "pid": pid, "amount": amt, "date": dt})
        elif atype == TYPE_DIRECT_TRANSFER and u2_int == manager_id:
            user_events.append({"action": "SELL", "pid": pid, "amount": amt, "date": dt})

    # Group by playerMasterId
    player_events = defaultdict(list)
    for ev in user_events:
        player_events[ev["pid"]].append(ev)

    completed_flips = []
    open_holdings = []
    initial_sales = []

    today = datetime.now()

    for pid, evs in player_events.items():
        p_info = player_names.get(pid, {"name": f"Player #{pid}", "pos": "?", "market_value": 0})
        buys = [e for e in evs if e["action"] == "BUY"]
        sells = [e for e in evs if e["action"] == "SELL"]

        while buys and sells:
            b = buys.pop(0)
            s = sells.pop(0)
            diff = s["amount"] - b["amount"]
            roi = (diff / b["amount"]) * 100 if b["amount"] else 0

            try:
                d_buy = datetime.strptime(b["date"], "%Y-%m-%d")
                d_sell = datetime.strptime(s["date"], "%Y-%m-%d")
                days = (d_sell - d_buy).days
            except Exception:
                days = 0

            completed_flips.append({
                "pid": pid,
                "name": p_info["name"],
                "pos": p_info["pos"],
                "buy_date": b["date"],
                "buy_price": b["amount"],
                "sell_date": s["date"],
                "sell_price": s["amount"],
                "profit": diff,
                "roi_pct": roi,
                "holding_days": max(0, days),
            })

        # Remaining buys are open holdings (still in squad)
        for b in buys:
            curr_mv = p_info.get("market_value") or 0
            diff = curr_mv - b["amount"]
            roi = (diff / b["amount"]) * 100 if b["amount"] else 0
            try:
                d_buy = datetime.strptime(b["date"], "%Y-%m-%d")
                days = (today - d_buy).days
            except Exception:
                days = 0

            open_holdings.append({
                "pid": pid,
                "name": p_info["name"],
                "pos": p_info["pos"],
                "buy_date": b["date"],
                "buy_price": b["amount"],
                "market_value": curr_mv,
                "unrealized_profit": diff,
                "roi_pct": roi,
                "holding_days": max(0, days),
            })

        # Remaining sells are sales of initial squad players
        for s in sells:
            initial_sales.append({
                "pid": pid,
                "name": p_info["name"],
                "pos": p_info["pos"],
                "sell_date": s["date"],
                "sell_price": s["amount"],
            })

    completed_flips.sort(key=lambda x: x["sell_date"], reverse=True)
    open_holdings.sort(key=lambda x: -x["unrealized_profit"])
    initial_sales.sort(key=lambda x: x["sell_date"], reverse=True)

    total_trades = len(completed_flips)
    winning_trades = sum(1 for f in completed_flips if f["profit"] > 0)
    losing_trades = sum(1 for f in completed_flips if f["profit"] < 0)
    win_rate = (winning_trades / total_trades) * 100 if total_trades else 0
    realized_profit = sum(f["profit"] for f in completed_flips)
    unrealized_profit = sum(o["unrealized_profit"] for o in open_holdings)
    total_pnl = realized_profit + unrealized_profit
    avg_roi = sum(f["roi_pct"] for f in completed_flips) / total_trades if total_trades else 0
    total_sales_revenue = sum(f["sell_price"] for f in completed_flips) + sum(s["sell_price"] for s in initial_sales)
    total_purchases_spent = sum(f["buy_price"] for f in completed_flips) + sum(o["buy_price"] for o in open_holdings)
    best_trade = max(completed_flips, key=lambda x: x["profit"]) if completed_flips else None

    return {
        "manager_id": manager_id,
        "completed_flips": completed_flips,
        "open_holdings": open_holdings,
        "initial_sales": initial_sales,
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate_pct": win_rate,
        "realized_profit": realized_profit,
        "unrealized_profit": unrealized_profit,
        "total_pnl": total_pnl,
        "avg_roi_pct": avg_roi,
        "total_sales_revenue": total_sales_revenue,
        "total_purchases_spent": total_purchases_spent,
        "best_trade": best_trade,
    }


def analyze_league_trading_history(
    client,
    league_id: str
) -> Dict[str, Any]:
    """Computes trading history for all managers across the entire league."""
    teams = client.league_teams(league_id) or []
    activity_live = client.league_activity(league_id) or []
    activity_cumulative = state.record_activity(activity_live, league_id)

    player_names = resolve_player_names(client, activity_cumulative, teams)

    results = []
    for t in teams:
        manager = t.get("manager") or {}
        manager_id = t.get("managerId") or manager.get("id")
        try:
            mid_int = int(manager_id) if manager_id is not None else None
        except (ValueError, TypeError):
            continue

        if mid_int is None:
            continue

        stats = compute_manager_trading_history(activity_cumulative, mid_int, player_names)
        stats["manager_name"] = manager.get("managerName") or "Unknown"
        stats["position"] = t.get("position") or 0
        stats["points"] = t.get("teamPoints") or 0
        stats["team_value"] = t.get("teamValue") or 0
        stats["is_me"] = t.get("teamMoney") is not None
        results.append(stats)

    # Sort league summary by total P&L (realized + unrealized) descending
    results.sort(key=lambda x: -x["total_pnl"])

    oldest_ts = activity_cumulative[0].get("createdAt") if activity_cumulative else None
    newest_ts = activity_cumulative[-1].get("createdAt") if activity_cumulative else None

    return {
        "league_id": league_id,
        "tracked_events": len(activity_cumulative),
        "tracked_from": oldest_ts[:10] if oldest_ts else None,
        "tracked_to": newest_ts[:10] if newest_ts else None,
        "managers": results,
    }
