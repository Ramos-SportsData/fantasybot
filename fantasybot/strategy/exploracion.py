"""Player multi-season historical intelligence and scouting analysis.

Analyzes:
  - 📊 Past season totals (lastSeasonPoints) and tier classification
  - 📈 Current scoring evolution vs historic rhythm
  - 🏃‍♂️ Starter status shifts (was starter last year -> benched now, or vice versa)
  - 🩺 Physical availability and injury risk
  - 💎 Value-for-money efficiency (€/point ratio)
"""

from typing import Dict, Any, List, Optional
from ..matching import match_name, normalize
from ..sources.lineups import probable_lineups

POS_LABELS = {1: "Portero", 2: "Defensa", 3: "Centrocampista", 4: "Delantero"}


def analyze_player_profile(
    pm: Dict[str, Any],
    prob_index: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Builds a comprehensive scouting report for a single player."""
    if prob_index is None:
        try:
            prob_index = probable_lineups() or {}
        except Exception:
            prob_index = {}

    pid = str(pm.get("id") or "")
    name = pm.get("nickname") or pm.get("name") or "Jugador"
    full_name = pm.get("name") or name
    pos_id = int(pm.get("positionId") or 0)
    pos_str = POS_LABELS.get(pos_id, "Jugador")
    team_data = pm.get("team") or {}
    team_name = team_data.get("name") or "LaLiga"
    market_val = int(pm.get("marketValue") or 0)
    current_pts = int(pm.get("points") or 0)
    avg_pts = float(pm.get("averagePoints") or 0.0)
    last_season_pts = int(pm.get("lastSeasonPoints") or 0)
    raw_status = (pm.get("playerStatus") or "ok").lower()

    # 1. Historical Season Tier
    if last_season_pts >= 220:
        tier_badge = "🌟 Estrella Top LaLiga"
        tier_desc = "Rendimiento élite absoluto la pasada temporada."
    elif last_season_pts >= 150:
        tier_badge = "🛡️ Titular Fijo Consolidado"
        tier_desc = "Pilar indiscutible con puntuaciones sólidas y regulares."
    elif last_season_pts >= 80:
        tier_badge = "🔄 Jugador de Rotación"
        tier_desc = "Alternó titularidades y suplencias el curso anterior."
    elif last_season_pts > 0:
        tier_badge = "🪑 Rol Secundario / Suplente"
        tier_desc = "Pocos minutos disputados en la temporada anterior."
    else:
        tier_badge = "🆕 Sin Registro Anterior / Fichaje"
        tier_desc = "Primer año en LaLiga o canterano ascendido."

    last_season_avg = round(last_season_pts / 38.0, 1) if last_season_pts > 0 else 0.0

    # 2. FutbolFantasy Match Prob & Injury Context
    info = match_name(name, full_name, prob_index) if prob_index else None
    starting_prob = info.get("prob") if info else None
    ff_injured = info.get("lesionado") is True if info else False
    ff_suspended = info.get("sancionado") is True if info else False

    # Starter status
    if starting_prob is not None:
        if starting_prob >= 80:
            starter_status = f"🔥 Titular Indiscutible ({starting_prob}%)"
        elif starting_prob >= 50:
            starter_status = f"⚡ Titular Probable ({starting_prob}%)"
        elif starting_prob >= 25:
            starter_status = f"🔄 Rotación / Revulsivo ({starting_prob}%)"
        else:
            starter_status = f"🪑 Suplente / Banquillo ({starting_prob}%)"
    else:
        starter_status = "❓ Titularidad sin estimar"

    # 3. Role Shift Detection (Past vs Present)
    if last_season_pts >= 140 and starting_prob is not None and starting_prob < 40:
        role_shift = "⚠️ Pérdida de Rol: Era titular fijo el año pasado y ahora ha perdido el puesto."
        role_shift_level = "WARNING"
    elif last_season_pts < 60 and starting_prob is not None and starting_prob >= 75:
        role_shift = "🚀 Jugador Emergente: Sin protagonismo el año pasado, ahora asentado en el XI."
        role_shift_level = "BOOST"
    elif last_season_pts >= 140 and starting_prob is not None and starting_prob >= 75:
        role_shift = "✅ Titular Consagrado: Mantiene su condición de titular indiscutible."
        role_shift_level = "OK"
    else:
        role_shift = "⚖️ Rol habitual acorde a su trayectoria."
        role_shift_level = "NEUTRAL"

    # 4. Evolution & Scoring Trend
    if avg_pts > 0 and last_season_avg > 0:
        ratio = avg_pts / last_season_avg
        if ratio >= 1.20:
            evolution = f"📈 En Clara Ascensión (+{(ratio - 1.0) * 100:.0f}% vs año anterior)"
            evolution_score = 1
        elif ratio <= 0.70:
            evolution = f"📉 En Declive / Menor Puntuación ({(ratio - 1.0) * 100:.0f}% vs año anterior)"
            evolution_score = -1
        else:
            evolution = "⚖️ Rendimiento Estable (en línea con su media)"
            evolution_score = 0
    elif avg_pts >= 5.0:
        evolution = "📈 Gran Arranque de Temporada"
        evolution_score = 1
    else:
        evolution = "📊 Datos de temporada en desarrollo"
        evolution_score = 0

    # 5. Physical Risk & Status
    if raw_status in ("suspended", "sanctioned", "sancionado", "expelled") or ff_suspended:
        physical_status = "🟥 Sancionado / Expulsado (No disponible)"
        is_available = False
    elif raw_status in ("injured", "lesionado") or ff_injured:
        physical_status = "🚑 Lesionado (Baja médica confirmada)"
        is_available = False
    elif raw_status in ("doubtful", "duda", "warned"):
        physical_status = "⚠️ Duda / Molestias Físicas (Riesgo de baja)"
        is_available = True
    else:
        physical_status = "✅ 100% Disponible y en forma"
        is_available = True

    # 6. Value Efficiency (€/pt)
    if last_season_pts > 0 and market_val > 0:
        cost_per_pt = market_val / last_season_pts
        if cost_per_pt < 150_000:
            efficiency = "💎 Ganga de Rendimiento (Puntos muy baratos)"
        elif cost_per_pt > 550_000:
            efficiency = "💸 Sobreprecio / Prima Alta"
        else:
            efficiency = "💵 Precio Justo de Mercado"
    else:
        cost_per_pt = None
        efficiency = "📊 Sin histórico para ratio económico"

    # 7. Final Verdict
    positive_signals = (
        (1 if last_season_pts >= 140 else 0)
        + (1 if (starting_prob or 0) >= 70 else 0)
        + (1 if is_available else -2)
        + (1 if evolution_score > 0 else (0 if evolution_score == 0 else -1))
    )
    if not is_available:
        verdict = "🔴 NO RECOMENDABLE (Lesión / Sanción Activa)"
        verdict_color = "red"
    elif positive_signals >= 3:
        verdict = "🟢 MUY RECOMENDABLE (Fichar / Titular Fijo)"
        verdict_color = "green"
    elif positive_signals >= 1:
        verdict = "🟡 COMPRA DE ROTACIÓN / ESPECULACIÓN"
        verdict_color = "yellow"
    else:
        verdict = "🔴 EVITAR / RIESGO ELEVADO (Suplente / Declive)"
        verdict_color = "red"

    return {
        "id": pid,
        "name": name,
        "full_name": full_name,
        "pos": pos_str,
        "pos_id": pos_id,
        "team": team_name,
        "market_value": market_val,
        "current_points": current_points,
        "current_avg": avg_pts,
        "last_season_points": last_season_pts,
        "last_season_avg": last_season_avg,
        "tier_badge": tier_badge,
        "tier_desc": tier_desc,
        "starting_prob": starting_prob,
        "starter_status": starter_status,
        "role_shift": role_shift,
        "role_shift_level": role_shift_level,
        "evolution": evolution,
        "physical_status": physical_status,
        "is_available": is_available,
        "cost_per_pt": cost_per_pt,
        "efficiency": efficiency,
        "verdict": verdict,
        "verdict_color": verdict_color,
    }


def search_player_in_list(query: str, players_list: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Finds the best matching player in a list of player dicts."""
    q = normalize(query.strip())
    if not q:
        return None

    # 1. Exact ID Match
    for p in players_list:
        pm = p.get("playerMaster") if "playerMaster" in p else p
        pid = str(pm.get("id") or p.get("playerTeamId") or "")
        if pid == q:
            return pm

    # 2. Exact Nickname / Name Match
    for p in players_list:
        pm = p.get("playerMaster") if "playerMaster" in p else p
        nick = normalize(pm.get("nickname") or "")
        name = normalize(pm.get("name") or "")
        if nick == q or name == q:
            return pm

    # 3. Substring / Word Match
    for p in players_list:
        pm = p.get("playerMaster") if "playerMaster" in p else p
        nick = normalize(pm.get("nickname") or "")
        name = normalize(pm.get("name") or "")
        if q in nick or q in name or nick in q:
            return pm

    return None


def analyze_team_squad(team_data: Dict[str, Any], prob_index: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Runs a full scouting audit across all players in the user's squad."""
    if prob_index is None:
        try:
            prob_index = probable_lineups() or {}
        except Exception:
            prob_index = {}

    players = team_data.get("players", [])
    reports = []
    for p in players:
        pm = p.get("playerMaster") or {}
        rep = analyze_player_profile(pm, prob_index=prob_index)
        rep["buyoutClause"] = p.get("buyoutClause") or pm.get("marketValue") or 0
        reports.append(rep)

    total_val = sum(r["market_value"] for r in reports)
    total_last_pts = sum(r["last_season_points"] for r in reports)
    avg_last_pts = round(total_last_pts / max(1, len(reports)), 1)

    stars = [r for r in reports if r["last_season_points"] >= 150]
    injured_or_suspended = [r for r in reports if not r["is_available"]]
    role_risk = [r for r in reports if r["role_shift_level"] == "WARNING"]
    emerging = [r for r in reports if r["role_shift_level"] == "BOOST"]

    by_pos = {1: [], 2: [], 3: [], 4: []}
    for r in reports:
        by_pos.setdefault(r["pos_id"], []).append(r)

    for pid in (1, 2, 3, 4):
        if pid in by_pos:
            by_pos[pid].sort(key=lambda x: -x["market_value"])

    return {
        "team_name": team_data.get("name", "Mi Plantilla"),
        "total_players": len(reports),
        "total_val": total_val,
        "team_money": team_data.get("teamMoney", 0),
        "total_last_pts": total_last_pts,
        "avg_last_pts": avg_last_pts,
        "stars": stars,
        "injured_or_suspended": injured_or_suspended,
        "role_risk": role_risk,
        "emerging": emerging,
        "reports": reports,
        "by_pos": by_pos,
    }
