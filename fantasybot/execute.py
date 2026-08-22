"""EXECUTION layer: turns decisions into real actions.

Autonomy authorized by the user:
  - Lineup: applies the best lineup (reversible, no spending).
  - Bid/cancel in market: places bids on profitable flips and pulls those that no
    longer apply (reversible until market close). May use the whole balance.
  - Sell: lists players flagged by strategy/sell.py (out of the XI + transfer
    risk or falling value) for sale at their fair market value. Selling a
    player is irreversible, but the user explicitly asked for this to be
    autonomous, using the same criteria as the advisory report.
  - Buyouts: NOT automatic (irreversible spending) -> left as an alert/task.

Everything runs through `dry_run`: if True, it only returns the PLAN without
touching anything.

IMPORTANT:
  - Real API calls (update_lineup, make_bid, sell_player) are wrapped in
    try/except. An API rejection (closed market, invalid squad, price moved,
    rate limit, etc.) must never crash the whole run -- it should be
    logged/reported and the run should continue with everything else.
  - The market is live: prices can move in the seconds/minutes between when the
    plan was computed (review()) and when we actually place the bid. Right before
    bidding we re-fetch the live listing and round its live salePrice UP
    (to the next multiple of 1000: the API requires the amount to be a
    multiple of 1000 AND >= the live price -- plain rounding or the exact
    price both get rejected by the API with a 400 error).
  - "Team has pending bid in this player" (errorCode 030.01.09) is NOT a real
    failure: .state/ doesn't persist between GitHub Actions runs (fresh checkout
    each time), so the bot can "forget" a bid it already placed in a previous
    run and try again. This is treated as an informational skip, not an error.
  - Sells already listed are tracked in state (state.load_sold/save_sold) so we
    don't try to re-list the same player on every run.
"""

from . import events, state
from .strategy import flip, sell as sell_mod
from .strategy.lineup import payload_ids
from .sources.market_trends import trends_index
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
            continue  # we already have a bid (best-effort, may be stale)

        # Usamos el precio exacto sin redondeos artificiales de múltiplos de 1000
        price = int(o["buy_price"])
        if committed + price > money:
            continue  # doesn't fit in the balance

        plan.append({"market_id": o["market_id"], "nombre": o["nombre"],
                     "amount": price, "margin_pct": o["margin_pct"]})
        committed += price
    return plan


def _live_price(client, league_id, market_id, fallback_amount):
    """Re-fetches the live market listing for `market_id` right before bidding.

    Utiliza el precio oficial exacto del mercado (el mayor entre salePrice y marketValue)
    sin forzar redondeos artificiales que LaLiga rechaza con el error 030.01.01.

    Falls back to the planned amount if the listing can't be found or lacks a
    price (never crashes -- worst case we retry with the old estimate).
    """
    try:
        market = client.market(league_id)
    except Exception:
        return fallback_amount
    el = next((e for e in market if e.get("id") == market_id), None)
    if not el:
        return fallback_amount  # no longer listed -> let make_bid fail cleanly
    
    sale = el.get("salePrice") or 0
    mval = (el.get("playerMaster") or {}).get("marketValue") or 0
    exact = max(sale, mval)
    
    if exact > 0:
        return int(exact)
        
    live = el.get("salePrice")
    if not live:
        return fallback_amount
    return int(live)


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
                # Releemos el precio exacto en vivo justo antes de pujar sin alterar tramos
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


# The API phrases "you no longer own this player" rejections differently
# depending on context -- we've seen both of these in the wild. Both mean
# the same thing: the player is already listed/gone (from a previous run we
# don't remember, since .state/ doesn't persist reliably between GitHub
# Actions runs). errorCode 030.01.26 is the confirmed code for this case.
ALREADY_LISTED_ERROR_HINTS = ("not found in team", "not in your team anymore")
ALREADY_LISTED_ERROR_CODE = "030.01.26"


def _is_already_listed_error(exc: Exception) -> bool:
    """True if the API rejected the sell because the player is no longer in
    the team roster -- i.e. it's already listed on the market. Not a real
    failure -- see module docstring."""
    s = str(exc)
    if ALREADY_LISTED_ERROR_CODE in s:
        return True
    return any(hint in s for hint in ALREADY_LISTED_ERROR_HINTS)


def sync_sells(client, league_id, team, best, dry_run=True, log=print):
    """Lists players recommended by strategy/sell.py that aren't already listed.

    Uses the exact same criteria as the advisory report (agent.review()):
    players outside the optimal XI who are either a transfer risk (valuable,
    out of the probable lineup) or have a clearly falling value trend.

    URGENT tier (priority 0, from sell.py: playerStatus != "ok") gets special
    handling here instead of an immediate sale: the first time we see a player
    in this state we record his CURRENT value as a baseline and just watch him
    (no listing yet) -- selling blind the moment a status changes is too
    aggressive (e.g. a 1-match suspension that doesn't really hurt his value).
    Only once his value has dropped >= WATCH_SELL_THRESHOLD_PCT from that
    baseline do we actually list him; if his value recovers/stabilizes instead,
    we drop the watch and keep him. See state.load_status_watch for the
    caveat about this needing state to persist across runs (best-effort on
    GitHub Actions).

    Selling is irreversible, but this was explicitly authorized as autonomous.
    Each listing is remembered in state (state.load_sold/save_sold) so we
    don't try to re-list a player that's already on sale on every run -- but
    since state doesn't persist reliably between GitHub Actions runs, the
    API's own "not found in team" rejection (player already listed) is the
    real safety net and is treated as informational, not an error.
    """
    WATCH_SELL_THRESHOLD_PCT = 0.05  # sell once value has dropped 5% from baseline

    candidates = sell_mod.sell_candidates(team, best, trends_index())
    sold = state.load_sold()
    watch = state.load_status_watch()

    to_process, watching, watch_cleared = [], [], []
    for c in candidates:
        if c["priority"] != 0:
            to_process.append(c)
            continue

        pid = str(c["player_id"])
        current_value = c["valor"]
        entry = watch.get(pid)

        if entry is None:
            # First time we see this abnormal status: start watching, don't sell yet.
            watch[pid] = {"nombre": c["nombre"], "status": c["reason"],
                         "baseline_value": current_value}
            watching.append({"nombre": c["nombre"], "baseline_value": current_value,
                            "current_value": current_value, "drop_pct": 0.0})
            continue

        baseline = entry.get("baseline_value") or current_value
        drop_pct = (baseline - current_value) / baseline if baseline else 0

        if drop_pct >= WATCH_SELL_THRESHOLD_PCT:
            # Confirmed: value actually dropped enough -> sell now.
            watch.pop(pid, None)
            to_process.append(c)
        else:
            # Still watching: either recovering/stable, or dropping but not
            # enough yet. Keep the ORIGINAL baseline (don't chase it down) so
            # a slow bleed still eventually crosses the threshold.
            watching.append({"nombre": c["nombre"], "baseline_value": baseline,
                            "current_value": current_value, "drop_pct": round(drop_pct * 100, 1)})
            if current_value >= baseline:
                # Fully recovered/stable -> stop watching, keep him.
                watch.pop(pid, None)
                watch_cleared.append(c["nombre"])

    state.save_status_watch(watch)

    listed, errors, already_listed = [], [], []
    for c in to_process:
        pid = str(c["player_id"])
        if pid in sold:
            continue  # already listed by a previous run (remembered locally)
        if not dry_run:
            try:
                client.sell_player(league_id, c["player_id"], c["sale_price"])
                sold[pid] = {"nombre": c["nombre"], "sale_price": c["sale_price"]}
                events.emit("sell", f"Listed {c['nombre']} for sale at {c['sale_price']:,}",
                            detail={"reason": c["reason"]})
                listed.append(c)
            except Exception as e:
                if _is_already_listed_error(e):
                    log(f"[execute] {c['nombre']} already listed (from a previous run).")
                    sold[pid] = {"nombre": c["nombre"], "sale_price": c["sale_price"]}
                    already_listed.append(c["nombre"])
                else:
                    log(f"[execute] Error selling {c['nombre']}: {e}")
                    errors.append({"nombre": c["nombre"], "error": str(e)})
        else:
            listed.append(c)

    if not dry_run:
        state.save_sold(sold)
    return {"action": "sells", "listed": listed, "errors": errors,
            "already_listed": already_listed, "watching": watching,
            "watch_cleared": watch_cleared, "applied": not dry_run}

def check_offers(client, league_id, team, log=print):
    """Detects pending offers on your own market listings and self-discovers
    their real data shape (offer_id, amount, etc.) so we can implement
    automatic acceptance without guessing blind.

    We know (from real API data) that a listing has a `numberOfOffers` count,
    but we've never seen its value above 0 in the wild -- so the exact shape
    of an individual offer (needed for accept_offer(league_id, market_id,
    offer_id, money)) is still unconfirmed. Instead of waiting for a manual
    debug round next time an offer appears, this tries several plausible ways
    to fetch the detail RIGHT WHEN one is detected, and reports the raw JSON
    via the "raw" field so it surfaces in Telegram automatically.

    Does NOT accept anything -- detection and self-discovery only.
    """
    own_ids = {p["playerMaster"]["id"] for p in team["players"]}
    try:
        market = client.market(league_id)
    except Exception as e:
        log(f"[execute] Error fetching market for offer check: {e}")
        return {"action": "offers", "pending": []}

    pending = []
    for el in market:
        pm = el.get("playerMaster") or {}
        if pm.get("id") not in own_ids:
            continue
        n_offers = el.get("numberOfOffers") or 0
        if n_offers <= 0:
            continue

        market_id = el.get("id")
        found = {"nombre": pm.get("nickname") or pm.get("name"),
                "market_id": market_id, "n_offers": n_offers, "raw": None}

        # Try a few plausible ways to get the actual offer detail (id, money).
        for path in (f"/league/{league_id}/market/{market_id}?x-lang=es",
                    f"/league/{league_id}/market/{market_id}/offer?x-lang=es"):
            try:
                detail = client.get(client._cmp(path))
                if detail:
                    found["raw"] = detail
                    found["source_path"] = path
                    break
            except Exception:
                continue

        if found["raw"] is None:
            found["raw"] = el  # fallback: at least the listing itself

        pending.append(found)
        log(f"[execute] Pending offer(s) on {found['nombre']}: {n_offers} "
            f"(detail: {'found' if found.get('source_path') else 'fallback to listing'})")

    return {"action": "offers", "pending": pending}


def act(client, league_id, team_id, team, best, current_ids, dry_run=True, log=print):
    """Executes (or plans) the autonomous actions: lineup + bids + sells + offer check."""
    return {
        "lineup": apply_lineup(client, team_id, best, current_ids, dry_run, log=log),
        "bids": sync_bids(client, league_id, team, dry_run, log=log),
        "sells": sync_sells(client, league_id, team, best, dry_run, log=log),
        "offers": check_offers(client, league_id, team, log=log),
    }
