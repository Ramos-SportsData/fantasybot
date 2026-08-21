"""Squad needs + signings advisor.

Detects positions where you're short (e.g. a single goalkeeper) and looks for
candidates in the market to sign, prioritizing those who will start for their team.
"""

from ..matching import match_name, POS
from ..sources.lineups import probable_lineups

# Recommended minimum per position (1 starter + rotation/injury margin).
MIN_SQUAD = {"POR": 2, "DEF": 5, "MED": 5, "DEL": 3}


def squad_counts(team):
    counts = {"POR": 0, "DEF": 0, "MED": 0, "DEL": 0}
    for p in team["players"]:
        pos = POS.get(p["playerMaster"]["positionId"])
        if pos:
            counts[pos] += 1
    return counts


def gaps(team):
    """Positions below the recommended minimum, with how many are missing."""
    counts = squad_counts(team)
    return {pos: MIN_SQUAD[pos] - counts[pos]
            for pos in counts if counts[pos] < MIN_SQUAD[pos]}


def candidates(client, league_id, position, prob_index=None, money=None, owned=None):
    """Market candidates in a position, sorted by starting probability.

    Returns dicts with buy price, route (system/buyout) and starting prob.
    Excludes players you already own (`owned` = set of playerMaster.id).

    A market entry can be malformed/incomplete right after a manual action outside
    the bot (e.g. accepting an offer): missing "playerMaster" or "discr". That must
    never crash the whole run -> skip the entry instead.
    """
    if prob_index is None:
        prob_index = probable_lineups()
    owned = owned or set()
    pos_id = {v: k for k, v in POS.items()}[position]

    out = []
    for el in client.market(league_id):
        pm = el.get("playerMaster")
        if not pm:
            continue
        if pm.get("positionId") != pos_id:
            continue
        if pm.get("id") in owned:
            continue  # already yours
        if el.get("discr") == "marketPlayerLeague":
            via, price = "SISTEMA", el.get("salePrice") or pm.get("marketValue")
        else:
            via, price = "CLAUSULA", el.get("playerTeam", {}).get("buyoutClause")
        if not price:
            continue
        info = match_name(pm.get("nickname", ""), pm.get("name", ""), prob_index)
        prob = info.get("prob") if info else None
        disponible = pm.get("playerStatus", "ok") == "ok"
        if info and (info.get("lesionado") or not info.get("disponible", True)):
            disponible = False
        out.append({
            "nombre": pm.get("nickname") or pm.get("name"),
            "market_id": el.get("id"),
            "player_id": pm.get("id"),
            "via": via,
            "price": price,
            "prob": prob,
            "disponible": disponible,
            "valor": pm.get("marketValue"),
            "affordable": (money is None or price <= money),
        })
    # sort: available starters first, then by probability and value
    out.sort(key=lambda c: (c["disponible"], c["prob"] or 0, c["valor"] or 0),
             reverse=True)
    return out


def advise(client, league_id, team, days_to_matchday=None):
    """Report: squad gaps and the best candidates to fill them."""
    prob_index = probable_lineups()
    money = team["teamMoney"]
    owned = {p["playerMaster"].get("id") for p in team["players"]}
    report = {"gaps": gaps(team), "urgency_multiplier": 1.0, "suggestions": {}}
    for pos in report["gaps"]:
        cands = candidates(client, league_id, pos, prob_index, money, owned)
        for c in cands:
            # recommended bid cap: use exact official market price to respect official tranches
            c["max_bid"] = c["price"]
        report["suggestions"][pos] = cands
    return report
