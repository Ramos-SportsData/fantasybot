"""Recommends squad sales: who to offload and at what price.

Safe rule: only proposes selling players NOT in your optimal XI (so it doesn't break your
lineup) and, by default, only for ONE clear, data-grounded reason — a clearly FALLING
value (trend below the threshold) → cash in before losing more.

It deliberately does NOT flag a valuable player merely for being outside today's PROBABLE
lineup: that data lags for recent signings, so "transfer risk / might leave" is a false
positive (the Aubameyang case) — and with no real transfer data we never invent it. A
cheap backup whose value is rising or stable is kept too (an appreciating asset/rotation).

EXCEPTION, opt-in via `prob_index`: when cash-on-hand is thin (`is_low_cash`), a bench
player who's very unlikely to start (probable-lineup % below `BENCH_PROB_THRESHOLD`) IS
flagged — "not playing and we need the room" is a legitimate reason to free up cash, even
without a falling trend. It only kicks in under low cash on purpose: with healthy cash
there's no rush to sell a fine, merely-benched squad player.
"""

from ..matching import match_name, POS
from .lineup import payload_ids

FALLING_THRESHOLD = -20  # trend (from futbolfantasy) below which it's "falling"
LOW_CASH_RATIO = 0.15    # cash below 15% of squad value counts as "thin" (relative to the
                         # squad's own scale — an absolute euro floor is meaningless across
                         # leagues with very different budgets)
BENCH_PROB_THRESHOLD = 15  # probable-lineup % below which a player "isn't really playing"


def squad_value(team) -> int:
    """Sum of marketValue across the whole squad — the scale `is_low_cash` judges against."""
    return sum((p["playerMaster"].get("marketValue") or 0) for p in team["players"])


def is_low_cash(team) -> bool:
    """True when cash-on-hand is thin — outright NEGATIVE always counts (regardless of
    squad value), or thin relative to the squad's own value otherwise."""
    money = team.get("teamMoney") or 0
    if money < 0:
        return True
    value = squad_value(team)
    return value > 0 and money < LOW_CASH_RATIO * value


def _prob(pm, prob_index):
    info = match_name(pm.get("nickname", ""), pm.get("name", ""), prob_index)
    return info.get("prob") if info else None


def sell_candidates(team, best, trends_index, falling_threshold=FALLING_THRESHOLD,
                    prob_index=None):
    """Players recommended to sell, with reason, priority and suggested price.

    `best` may be None when the squad can't field a valid XI yet (e.g. no goalkeeper).
    A missing lineup shouldn't silence the sell advice — there are simply no protected
    starters, so we fall back to flagging clearly-falling-value players.

    `prob_index` (optional, from `sources.lineups.probable_lineups()`) enables the
    low-cash "not playing" exception described in the module docstring. Omitting it keeps
    the original falling-value-only behaviour byte-identical.
    """
    xi_ids = payload_ids(best) if best else set()
    # Protect the lineup's COACH too (premium): he's not in payload_ids but is in use, so
    # selling him would empty the coach slot. A SURPLUS coach (not the selected one) stays
    # sellable — only the one the lineup uses is protected, like a starter.
    coach_id = str(best["coach"]) if best and best.get("coach") else None
    low_cash = prob_index is not None and is_low_cash(team)

    out = []
    for p in team["players"]:
        pm = p["playerMaster"]
        ptid = p.get("playerTeamId") or pm["id"]
        if ptid in xi_ids or (coach_id and str(ptid) == coach_id):
            continue  # a starter, or the lineup's coach → don't sell

        valor = pm.get("marketValue") or 0
        trend = match_name(pm.get("nickname", ""), pm.get("name", ""), trends_index)
        tendencia = trend.get("tendencia") if trend else None
        prob = _prob(pm, prob_index) if low_cash else None

        # out_of_league = the player LEFT LaLiga (transferred abroad). This is an OFFICIAL,
        # data-grounded reason: his fantasy value collapses, so sell REGARDLESS of trend.
        # Only the exact status triggers it (not injured/doubtful/suspended/unknown), so it
        # is nothing like the forbidden "transfer risk" guesswork below.
        if pm.get("playerStatus") == "out_of_league":
            reason, prio = "fuera de LaLiga (su valor se desploma)", 0
        # A clearly FALLING value is always a sell signal.
        elif tendencia is not None and tendencia <= falling_threshold:
            reason, prio = f"falling value (trend {tendencia})", 1
        # Low cash only: a bench player who's essentially not going to start. Gated on a
        # KNOWN probability (never on an unmatched name) — same convention MIN_CLAUSE_PROB
        # uses for spending decisions, so we don't act on a name-match miss.
        elif prob is not None and prob < BENCH_PROB_THRESHOLD:
            reason, prio = f"no juega (prob. titular {prob}%), caja ajustada", 2
        else:
            continue  # stable/rising and (playing, or cash is fine) → keep

        out.append({
            "nombre": pm.get("nickname") or pm.get("name"),
            # NOTE: must be the in-team instance id (playerTeamId), not the catalog
            # id (pm["id"]) -- the sell endpoint rejects the catalog id with
            # "player not in your team" even when the player clearly is. `ptid`
            # is computed above for the exact same reason (protecting the XI).
            "player_id": ptid,
            "pos": POS.get(pm.get("positionId"), "?"),
            "valor": valor,
            "sale_price": round(valor),  # fair price for a quick sale
            "tendencia": tendencia,
            "reason": reason,
            "priority": prio,
        })
    out.sort(key=lambda c: (c["priority"], -c["valor"]))
    return out
