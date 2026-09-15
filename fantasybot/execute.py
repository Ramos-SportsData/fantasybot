"""EXECUTION layer: turns decisions into real actions.

Autonomy authorized by the user:
  - Lineup: applies the best lineup (reversible, no spending). Always on when
    `--execute` is passed.
  - Bid/cancel in market: places bids on profitable flips and pulls those that no
    longer apply (reversible until market close). May use the whole balance.
    Always on when `--execute` is passed.
  - Sells: lists `sell_candidates()` for sale at the recommended price. AUTOMATIC
    when `--execute-sells` is also passed; off by default.
  - Buyouts: irreversible spending. AUTOMATIC when `--execute-clauses` is also
    passed; off by default, and even when on, capped by `max_clause_spend` per
    run (default: half the current balance) so a burst of clauses opening at
    once can never empty the balance in one pass.

Everything runs through `dry_run`: if True, it only returns the PLAN without
touching anything.
"""

from datetime import datetime, timezone

from . import events, state
from .strategy import flip
from .strategy.lineup import payload_ids

# Position name (as used in `best["missing"]`) → Spanish abbreviation for the copy.
_POS_ABBREV = {"goalkeeper": "POR", "defender": "DEF", "midfield": "MED", "striker": "DEL"}


def _norm_id(x):
    """Compare ids across int/str/None uniformly (the captain is a string in the payload,
    but the current lineup may hand it back as an int, or None when unset)."""
    return None if x is None else str(x)


def describe_missing(missing):
    """Format a `best["missing"]` dict ({"midfield": 1, ...}) as Spanish slot text.

    e.g. {"midfield": 1} -> "1 MED"; {"defender": 2, "midfield": 1} -> "2 DEF, 1 MED".
    Ordered POR/DEF/MED/DEL so the phrasing is stable regardless of dict order.
    """
    order = ["goalkeeper", "defender", "midfield", "striker"]
    parts = [f"{missing[pos]} {_POS_ABBREV[pos]}" for pos in order if missing.get(pos)]
    return ", ".join(parts)


def apply_lineup(client, team_id, best, current_ids, dry_run=True,
                 current_coach=None, current_captain=None):
    """Applies the optimal lineup if it differs from the current one.

    Fielding the best available XI beats leaving a worse one, so a partial (incomplete)
    lineup is still applied — but when `best["incomplete"]`, the emitted event flags it as
    a problem (which line is short) instead of reporting a clean "lineup applied", so the
    user is told to sign a player for the empty slot.

    PREMIUM: the payload may also carry a `coach`/`captain` (see lineup.optimize). A captain
    (or coach) change with the SAME XI must still count as "changed", or we would never PUT
    the new captain. So the comparison also checks coach/captain against the current lineup's
    (`current_coach`/`current_captain`, from agent._current_lineup) — but ONLY when the payload
    actually carries that field (non-premium payloads have neither, so this is a no-op there
    and behaviour stays byte-identical).
    """
    new_ids = payload_ids(best)
    incomplete = bool(best.get("incomplete"))
    missing = best.get("missing") or {}
    payload = best["payload"]
    # Premium extras only participate when present in the payload (gated on premium upstream).
    coach_same = ("coach" not in payload) or (
        _norm_id(payload.get("coach")) == _norm_id(current_coach))
    captain_same = ("captain" not in payload) or (
        _norm_id(payload.get("captain")) == _norm_id(current_captain))
    if new_ids == current_ids and coach_same and captain_same:
        return {"action": "lineup", "changed": False,
                "incomplete": incomplete, "missing": missing}
    has_extras = any(k in payload for k in ("coach", "captain", "bench"))
    premium_stripped = False
    if not dry_run:
        # PREMIUM extras (coach/captain/bench) use a to-be-live-confirmed PUT format. If the
        # API rejects it, a working XI still beats a rejected PUT — retry WITHOUT the extras.
        # A non-premium payload (no extras) has nothing to strip, so its failure re-raises as
        # before (a real error we must not swallow).
        try:
            client.update_lineup(team_id, payload)
        except Exception:
            base = {k: v for k, v in payload.items()
                    if k not in ("coach", "captain", "bench")}
            if base != payload:
                client.update_lineup(team_id, base)   # premium extras dropped, XI still set
                premium_stripped = True               # ...so we must NOT claim we set them
            else:
                raise
        d, m, f = best["formation"]
        if incomplete:
            desc = describe_missing(missing)
            title = f"⚠️ Alineación INCOMPLETA {d}-{m}-{f}: falta(n) {desc} sin cubrir"
        else:
            title = f"Lineup {d}-{m}-{f} applied"
        events.emit("lineup", title, detail={"score": best.get("total")})
    # premium_applied tells the caller whether the coach/captain ACTUALLY made it to LaLiga,
    # so it never reports a captain/coach it silently had to drop (honest paid-user panels).
    return {"action": "lineup", "changed": True, "applied": not dry_run,
            "formation": best["formation"], "incomplete": incomplete, "missing": missing,
            "premium_applied": has_extras and not premium_stripped}


def _system_flips(client, league_id):
    """Profitable SYSTEM flips (a single pass over market + trends)."""
    return [o for o in flip.opportunities(client, league_id)
            if o["via"] == "SISTEMA" and o["margin_pct"] > 0]


def plan_bids(client, league_id, team, ops=None):
    """What to bid on: profitable SYSTEM flips that fit the balance, by margin.

    SYSTEM only (auction). Buyouts are outside the scope of autonomy.
    """
    money = team["teamMoney"]
    if ops is None:
        ops = _system_flips(client, league_id)
    # The live market is the truth about what already has money on it, not the local
    # file. The file only remembers THIS bot's bids: anything placed from the app or
    # by hand is invisible to it, and the bot re-proposes players that already carry
    # a bid. Reading `bid`/`offer` off each listing catches them all.
    already = set(state.load_bids())
    try:
        for el in client.market(league_id):
            if el.get("bid") or el.get("offer"):
                already.add(str(el.get("id")))
    except Exception:
        pass          # without the market, the local file is still better than nothing
    plan, committed = [], 0
    for o in ops:
        if str(o["market_id"]) in already:
            continue  # money is already on that player
        if committed + o["buy_price"] > money:
            continue  # doesn't fit in the balance
        plan.append({"market_id": o["market_id"], "nombre": o["nombre"],
                     "amount": o["buy_price"], "margin_pct": o["margin_pct"]})
        committed += o["buy_price"]
    return plan


def sync_bids(client, league_id, team, dry_run=True):
    """Places new bids from the plan and cancels those that no longer apply."""
    ops = _system_flips(client, league_id)   # a single pass, reused below
    plan = plan_bids(client, league_id, team, ops)
    bids = state.load_bids()
    valid_ids = {o["market_id"] for o in ops}

    placed, cancelled = [], []
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
            resp = client.make_bid(league_id, b["market_id"], b["amount"])
            bid_id = resp.get("id") if isinstance(resp, dict) else None
            bids[b["market_id"]] = {"bid_id": bid_id, "amount": b["amount"],
                                    "nombre": b["nombre"]}
            events.emit("bid", f"Bid {b['amount']:,} for {b['nombre']}",
                        detail={"margin": f"{b['margin_pct']}%"})
        placed.append(b)

    if not dry_run:
        state.save_bids(bids)
    return {"action": "bids", "placed": placed, "cancelled": cancelled,
            "applied": not dry_run}


def sync_sells(client, league_id, sells, dry_run=True):
    """Lists `agent.review()`'s sell_candidates() for sale at the recommended price.

    Runs automatically when `--execute-sells` is passed to `agent --execute` (see
    `sell_enabled` in `act()`) -- opt-in via that flag, not gated further here.

    Skips a player already listed (reading the live market, same pattern as
    `plan_bids`) so a re-run doesn't relist or double-list him.
    """
    already_listed = set()
    try:
        for el in client.market(league_id):
            if el.get("discr") == "marketPlayerLeague" and el.get("status") == "on_sale":
                pm = el.get("playerMaster") or {}
                if pm.get("id"):
                    already_listed.add(pm["id"])
    except Exception:
        pass  # without the market, we just risk a harmless re-list attempt below

    listed, skipped, failed = [], [], []
    for s in sells:
        pid = s["player_id"]
        if pid in already_listed:
            skipped.append(s["nombre"])
            continue
        if not dry_run:
            try:
                client.sell_player(league_id, pid, s["sale_price"])
                events.emit("sell", f"Listed {s['nombre']} for sale",
                            detail={"price": f"{s['sale_price']:,}", "reason": s["reason"]})
            except Exception as e:
                events.emit("sell", f"Failed to list {s['nombre']}", detail=str(e),
                            status="error")
                failed.append({"nombre": s["nombre"], "error": str(e)})
                continue
        listed.append(s)

    return {"action": "sells", "listed": listed, "skipped": skipped, "failed": failed,
            "applied": not dry_run}


def pay_clauses(client, league_id, targets, team_money, dry_run=True,
                max_clause_spend=None, min_clause_prob=60):
    """Pays buyout clauses that are open RIGHT NOW (unlock time already passed) and
    meet the safety bar, up to `max_clause_spend` total for this run.

    Irreversible spending, so this is opt-in on top of `dry_run=False` (see
    `clause_enabled` in `act()`) and always capped:
      - `max_clause_spend`: hard ceiling on total € spent on clauses THIS run.
        None -> capped at half the current balance, never the whole balance, so a
        burst of several clauses opening together can't wipe out the team's cash.
      - `min_clause_prob`: skip anyone below this starting-XI probability, even if
        `clause_targets()` already filtered a lower bar (MIN_CLAUSE_PROB=40) -- an
        irreversible buy deserves a stricter bar than a reversible bid/task.
      - Only clauses whose `unlock` time has already passed are candidates: paying
        BEFORE unlock isn't offered by the game, and clause_targets() already
        prefers the open-sale route (`cheaper_via_bid`) over the clause when it's
        cheaper, so those are skipped here (the bid path in sync_bids covers them).
      - Cheapest-first, so the budget stretches across as many gaps as possible
        instead of blowing it all on the single priciest target.
    """
    if max_clause_spend is None:
        max_clause_spend = team_money // 2

    now = datetime.now(timezone.utc)
    candidates = []
    for t in targets:
        if t.get("cheaper_via_bid"):
            continue  # the open sale is the intended route for this one, not the clause
        if t.get("prob") is not None and t["prob"] < min_clause_prob:
            continue
        try:
            unlock = datetime.fromisoformat(t["unlock"])
        except (TypeError, ValueError):
            continue
        if unlock > now:
            continue  # clause hasn't opened yet
        candidates.append(t)
    candidates.sort(key=lambda t: t["clause"])

    paid, spent = [], 0
    for t in candidates:
        if spent + t["clause"] > max_clause_spend:
            continue
        if spent + t["clause"] > team_money:
            continue
        if not dry_run:
            try:
                client.pay_buyout_clause(league_id, t["player_id"], t["clause"])
                events.emit("clause", f"Buyout: {t['nombre']} for {t['clause']:,}",
                            detail={"pos": t["pos"], "reason": t["reason"]})
            except Exception as e:
                events.emit("clause", f"Failed buyout: {t['nombre']}", detail=str(e),
                            status="error")
                continue
        paid.append(t)
        spent += t["clause"]

    return {"action": "clauses", "paid": paid, "spent": spent,
            "budget": max_clause_spend, "applied": not dry_run}


def act(client, league_id, team_id, team, best, current_ids, dry_run=True,
        current_coach=None, current_captain=None,
        sell_enabled=False, sells=None,
        clause_enabled=False, clause_targets=None, max_clause_spend=None):
    """Executes (or plans) the autonomous actions: lineup + bids always; sells and
    buyout clauses only when explicitly enabled (see module docstring for why).

    `current_coach`/`current_captain` (premium) let apply_lineup detect a captain/coach
    change that leaves the XI unchanged; None (non-premium/default) keeps today's behaviour.
    """
    result = {
        "lineup": apply_lineup(client, team_id, best, current_ids, dry_run,
                               current_coach=current_coach, current_captain=current_captain),
        "bids": sync_bids(client, league_id, team, dry_run),
    }
    if sell_enabled and sells:
        result["sells"] = sync_sells(client, league_id, sells, dry_run)
    if clause_enabled and clause_targets:
        result["clauses"] = pay_clauses(client, league_id, clause_targets,
                                        team["teamMoney"], dry_run,
                                        max_clause_spend=max_clause_spend)
    return result
