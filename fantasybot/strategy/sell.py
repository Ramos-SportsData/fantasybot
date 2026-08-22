"""SELL advisor: who to offload and at what price.

Safe rule: only proposes selling players NOT in your optimal XI (so it doesn't
break your lineup) and only for a clear reason:
  1) Transfer risk: valuable but outside the probable lineup (the ⚠ Etta types) →
     sell him before he leaves LaLiga and his value collapses.
  2) Falling value: his trend is clearly negative → cash in before losing more.

It does NOT touch a cheap backup whose value is rising or stable (an appreciating
asset or useful rotation).

URGENT tier (priority 0): a player whose live playerStatus is anything other
than "ok" (red card / suspension / injury picked up during the match, etc.)
is listed IMMEDIATELY, even if he's currently in your optimal XI -- this is
the one case where we override the "never sell a starter" safeguard. Listing
is reversible (can be cancelled, and only becomes a real sale if someone
actually buys him), so acting fast here is safe. The actual sell vs. watch
decision (5% value-drop threshold) is handled in execute.py, not here --
this module only flags the candidate.

IMPORTANT: player_id here is playerTeamId (the instance-specific id for this
player on YOUR roster), not playerMaster's generic id. A real API rejection
("player is not in your team anymore", errorCode 030.01.26) confirmed via
debug that these players WERE genuinely owned and NOT already listed -- so
using the wrong id (playerMaster's generic id instead of the roster-specific
playerTeamId) is the most likely explanation, consistent with how every other
write action in this codebase (bids, cancels) always uses an instance-specific
id rather than a generic one.
"""

from ..matching import match_name, POS
from .lineup import payload_ids

FALLING_THRESHOLD = -20  # trend (from futbolfantasy) below which it's "falling"


def sell_candidates(team, best, trends_index, falling_threshold=FALLING_THRESHOLD):
    """Players recommended to sell, with reason, priority and suggested price.

    `best` may be None when the squad can't field a valid XI yet (e.g. no goalkeeper).
    A missing lineup shouldn't silence the sell advice -- there are simply no protected
    starters, so we fall back to flagging clearly-falling-value players.
    """
    xi_ids = payload_ids(best) if best else set()
    watch_ids = {w["playerTeamId"] for w in best.get("watch", [])} if best else set()

    out = []
    for p in team["players"]:
        pm = p["playerMaster"]
        ptid = p.get("playerTeamId") or pm["id"]
        valor = pm.get("marketValue") or 0
        status = pm.get("playerStatus")

        # URGENT: live status change (expulsion/injury/suspension), regardless
        # of whether he's a starter. We don't know every exact status string
        # the API uses, so treat anything other than "ok" as urgent.
        if status and status != "ok":
            out.append({
                "nombre": pm.get("nickname") or pm.get("name"),
                "player_id": ptid,  # playerTeamId, not playerMaster's generic id -- see module docstring
                "pos": POS.get(pm.get("positionId"), "?"),
                "valor": valor,
                "sale_price": round(valor),
                "tendencia": None,
                "reason": f"URGENTE: estado en directo = '{status}' (posible expulsion/lesion)",
                "priority": 0,
            })
            continue  # already flagged, skip the other (non-urgent) checks below

        if ptid in xi_ids:
            continue  # he's a starter -> don't sell (unless urgent, handled above)

        trend = match_name(pm.get("nickname", ""), pm.get("name", ""), trends_index)
        tendencia = trend.get("tendencia") if trend else None

        if ptid in watch_ids:
            reason, prio = "transfer risk (out of the lineup, valuable)", 1
        elif tendencia is not None and tendencia <= falling_threshold:
            reason, prio = f"falling value (trend {tendencia})", 2
        else:
            continue  # stable/rising backup -> keep

        out.append({
            "nombre": pm.get("nickname") or pm.get("name"),
            "player_id": ptid,  # playerTeamId, not playerMaster's generic id -- see module docstring
            "pos": POS.get(pm.get("positionId"), "?"),
            "valor": valor,
            "sale_price": round(valor),  # fair price for a quick sale
            "tendencia": tendencia,
            "reason": reason,
            "priority": prio,
        })
    out.sort(key=lambda c: (c["priority"], -c["valor"]))
    return out
