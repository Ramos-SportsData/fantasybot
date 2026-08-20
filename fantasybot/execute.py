"""EXECUTION layer: turns decisions into real actions.

Autonomy authorized by the user:
  - Lineup: applies the best lineup (reversible, no spending).
  - Bid/cancel in market: places bids on profitable flips and pulls those that no
    longer apply (reversible until market close). May use the whole balance.
  - Buyouts: NOT automatic (irreversible spending) -> left as an alert/task.

Everything runs through `dry_run`: if True, it only returns the PLAN without
touching anything.

IMPORTANT:
  - Real API calls (update_lineup, make_bid) are wrapped in try/except. An API
    rejection (closed market, invalid squad, price moved, rate limit, etc.) must
    never crash the whole run -- it should be logged/reported and the run should
    continue with everything else.
  - The market is live: prices can move in the seconds/minutes between when the
    plan was computed (review()) and when we actually place the bid. Right before
    bidding we re-fetch the live listing and use its current salePrice instead of
    the (possibly stale) amount computed earlier -- this is what prevents
    "X is not a valid money quantity for this player" (HTTP 400) errors from a
    price that moved mid-run.
"""

from . import events, state
from .strategy import flip
from .strategy.lineup import payload_ids


def apply_lineup(client, team_id, best, current_ids, dry_run=True, log=print):
    """Applies the optimal lineup if it differs from the current one."""
    new_ids = payload_ids(best)
    if new_ids == current_ids:
        return {"action": "lineup", "changed": False}
    if not dry_run:
        try:
            client.update_lineup(team_id, best["payload"])
            d, m, f = best["formation"]
            events.emit("lineup", f"Lineup {d}-{m}-{f} applied",
                        detail={"score": best.get("total")})
        except Exception as e:
            log(f"[execute] Error applying lineup: {e}")
            return {"action": "lineup", "changed": True, "applied": False,
                    "formation": best["formation"], "error": str(e)}
    return {"action": "lineup", "changed": True, "applied": not dry_run,
            "formation": best["formation"]}


def _system_flips(client, league_id):
    """Profitable SYSTEM flips (a single pass over market + trends)."""
    return [o for o in flip.opportunities(client, league_id)
            if o["via"] == "SISTEMA" and o["margin_pct"] > 0]


def plan_bids(client, league_id, team, ops=None):
    """What to bid on: profitable SYSTEM flips that fit the balance, by margin.

    SYSTEM only (auction). Buyouts are outside the scope of autonomy.

    The amount here is a PLANNING estimate (rounded to the nearest thousand to
    match the API's expected granularity). It is re-checked against the live
    price right before the actual bid in sync_bids(), since the market moves.
    """
    money = team["teamMoney"]
    if ops is None:
        ops = _system_flips(client, league_id)
    already = state.load_bids()
    plan, committed = [], 0
    for o in ops:
        if o["market_id"] in already:
            continue  # we already have a bid

        # Redondeo a los miles para cumplir con los tramos que exige la API.
        raw_price = o["buy_price"]
        rounded_price = int(round(raw_price, -3))

        if committed + rounded_price > money:
            continue  # doesn't fit in the balance

        plan.append({"market_id": o["market_id"], "nombre": o["nombre"],
                     "amount": rounded_price, "margin_pct": o["margin_pct"]})
        committed += rounded_price
    return plan


def _live_price(client, league_id, market_id, fallback_amount):
    """Re-fetches the live market listing for `market_id` right before bidding.

    The market moves in real time: the amount computed earlier in plan_bids()
    (itself computed even earlier in review()) can be stale by the time we
    actually place the bid. Using the live salePrice here is what avoids
    "not a valid money quantity for this player" (HTTP 400) errors.

    Falls back to the planned amount if the listing can't be found or lacks a
    salePrice (never crashes -- worst case we retry with the old estimate).
    """
    try:
        market = client.market(league_id)
    except Exception:
        return fallback_amount
    el = next((e for e in market if e.get("id") == market_id), None)
    if not el:
        return fallback_amount  # no longer listed (closed/taken) -> let make_bid fail cleanly
    live = el.get("salePrice")
    if not live:
        return fallback_amount
    return int(round(live, -3))


def sync_bids(client, league_id, team, dry_run=True, log=print):
    """Places new bids from the plan and cancels those that no longer apply."""
    ops = _system_flips(client, league_id)   # a single pass, reused below
    plan = plan_bids(client, league_id, team, ops)
    bids = state.load_bids()
    valid_ids = {o["market_id"] for o in ops}

    placed, cancelled, errors = [], [], []
    # cancel bids whose target is no longer profitable
    for mid, info in list(bids.items()):
        if mid not in valid_ids:
            if not dry_run:
                try:
                    client.cancel_bid(league_id, mid, info["bid_id"])
                except Exception:
                    pass
                bids.pop(mid, None)
                events.emit("cancel", f"Bid cancelled: {info.get('nombre', mid)}",
                            detail="no longer profitable")
            cancelled.append(info.get("nombre", mid))
    # place new bids
    for b in plan:
        if not dry_run:
            try:
                # Releemos el precio justo antes de pujar: el mercado es en vivo y
                # puede haber subido desde que se calculo el plan.
                amount = _live_price(client, league_id, b["market_id"], b["amount"])

                resp = client.make_bid(league_id, b["market_id"], amount)
                bid_id = resp.get("id") if isinstance(resp, dict) else None
                bids[b["market_id"]] = {"bid_id": bid_id, "amount": amount,
                                        "nombre": b["nombre"]}
                events.emit("bid", f"Bid {amount:,} for {b['nombre']}",
                            detail={"margin": f"{b['margin_pct']}%"})
                placed.append({**b, "amount": amount})
            except Exception as e:
                log(f"[execute] Error bidding on {b['nombre']}: {e}")
                errors.append({"nombre": b["nombre"], "error": str(e)})
        else:
            placed.append(b)

    if not dry_run:
        state.save_bids(bids)
    return {"action": "bids", "placed": placed, "cancelled": cancelled,
            "errors": errors, "applied": not dry_run}


def act(client, league_id, team_id, team, best, current_ids, dry_run=True, log=print):
    """Executes (or plans) the autonomous actions: set lineup + bid."""
    return {
        "lineup": apply_lineup(client, team_id, best, current_ids, dry_run, log=log),
        "bids": sync_bids(client, league_id, team, dry_run, log=log),
    }
