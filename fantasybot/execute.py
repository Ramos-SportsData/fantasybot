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
    bidding we re-fetch the live listing and use its EXACT current salePrice
    (unrounded -- the API rejects amounts that don't match its own expected
    quantity, e.g. "3828000 is not a valid money quantity" when the real price
    was 3828353) instead of the (possibly stale) amount computed earlier.
  - "Team has pending bid in this player" (errorCode 030.01.09) is NOT a real
    failure: .state/ doesn't persist between GitHub Actions runs (fresh checkout
    each time), so the bot can "forget" a bid it already placed in a previous
    run and try again. This is treated as an informational skip, not an error.
"""

from . import events, state
from .strategy import flip
from .strategy.lineup import payload_ids
import math

ALREADY_BIDDING_ERROR_CODE = "030.01.09"


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

    The amount here is a PLANNING estimate for the affordability check. The
    EXACT amount actually sent to the API is re-read live right before the bid
    in sync_bids() (see _live_price), since the market moves and the API is
    strict about the exact quantity it expects.
    """
    money = team["teamMoney"]
    if ops is None:
        ops = _system_flips(client, league_id)
    already = state.load_bids()
    plan, committed = [], 0
    for o in ops:
        if o["market_id"] in already:
            continue  # we already have a bid (best-effort, may be stale — see below)

        price = o["buy_price"]
        if committed + price > money:
            continue  # doesn't fit in the balance

        plan.append({"market_id": o["market_id"], "nombre": o["nombre"],
                     "amount": price, "margin_pct": o["margin_pct"]})
        committed += price
    return plan


def _live_price(client, league_id, market_id, fallback_amount):
    """Re-fetches the live market listing for `market_id` right before bidding.

    The market moves in real time: the amount computed earlier in plan_bids()
    (itself computed even earlier in review()) can be stale by the time we
    actually place the bid. We use the EXACT live salePrice, unrounded — the
    API rejects amounts that don't match its own expected quantity for that
    player (rounding to the nearest thousand is NOT safe: e.g. it rejected
    "3828000" while the real price was 3828353).

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
    return live if live else fallback_amount


def _is_already_bidding_error(exc: Exception) -> bool:
    """True if the API rejected the bid because we already have one pending on
    this player (errorCode 030.01.09). Not a real failure -- see module docstring."""
    return ALREADY_BIDDING_ERROR_CODE in str(exc)


def sync_bids(client, league_id, team, dry_run=True, log=print):
    """Places new bids from the plan and cancels those that no longer apply."""
    ops = _system_flips(client, league_id)   # a single pass, reused below
    plan = plan_bids(client, league_id, team, ops)
    bids = state.load_bids()
    valid_ids = {o["market_id"] for o in ops}

    placed, cancelled, errors, already_bidding = [], [], [], []
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
                # puede haber subido desde que se calculo el plan. Se usa EXACTO,
                # sin redondear -- la API rechaza cantidades que no coincidan.
                amount = _live_price(client, league_id, b["market_id"], b["amount"])

                resp = client.make_bid(league_id, b["market_id"], amount)
                bid_id = resp.get("id") if isinstance(resp, dict) else None
                bids[b["market_id"]] = {"bid_id": bid_id, "amount": amount,
                                        "nombre": b["nombre"]}
                events.emit("bid", f"Bid {amount:,} for {b['nombre']}",
                            detail={"margin": f"{b['margin_pct']}%"})
                placed.append({**b, "amount": amount})
            except Exception as e:
                if _is_already_bidding_error(e):
                    # We (or a previous run) already have a pending bid here --
                    # .state/ doesn't persist between GH Actions runs, so we just
                    # "forgot". Not a real error: keep it informational, and
                    # remember it locally for the rest of THIS run at least.
                    log(f"[execute] Already bidding on {b['nombre']} (from a previous run).")
                    bids.setdefault(b["market_id"], {"bid_id": None, "amount": b["amount"],
                                                      "nombre": b["nombre"]})
                    already_bidding.append(b["nombre"])
                else:
                    log(f"[execute] Error bidding on {b['nombre']}: {e}")
                    errors.append({"nombre": b["nombre"], "error": str(e)})
        else:
            placed.append(b)

    if not dry_run:
        state.save_bids(bids)
    return {"action": "bids", "placed": placed, "cancelled": cancelled,
            "errors": errors, "already_bidding": already_bidding, "applied": not dry_run}


def act(client, league_id, team_id, team, best, current_ids, dry_run=True, log=print):
    """Executes (or plans) the autonomous actions: set lineup + bid."""
    return {
        "lineup": apply_lineup(client, team_id, best, current_ids, dry_run, log=log),
        "bids": sync_bids(client, league_id, team, dry_run, log=log),
    }
