"""Conservative team-first matching against external football sources."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from fantasybot.matching import normalize
from fantasybot.sources.lineups import probable_lineups
from fantasybot.sources.market_trends import market_trends
from fantasybot.sources.matchday import next_kickoff


MAX_EXTERNAL_VALUE_DIFF = 0.35

TEAM_EQUIVALENCES = {
    "deportivo-alaves": "alaves",
    "athletic-club": "athletic",
    "atletico-de-madrid": "atletico",
    "atletico-madrid": "atletico",
    "fc-barcelona": "barcelona",
    "barca": "barcelona",
    "real-betis": "betis",
    "rc-celta": "celta",
    "celta-de-vigo": "celta",
    "rc-deportivo": "deportivo",
    "deportivo-la-coruna": "deportivo",
    "elche-cf": "elche",
    "rcd-espanyol": "espanyol",
    "espanyol-de-barcelona": "espanyol",
    "getafe-cf": "getafe",
    "levante-ud": "levante",
    "malaga-cf": "malaga",
    "c-a-osasuna": "osasuna",
    "ca-osasuna": "osasuna",
    "real-racing-club": "racing",
    "racing-santander": "racing",
    "real-madrid-cf": "real-madrid",
    "sevilla-fc": "sevilla",
    "valencia-cf": "valencia",
    "villarreal-cf": "villarreal",
}


def normalise_team_slug(value: Any) -> str | None:
    """Return one canonical team slug across API and FutbolFantasy variants."""
    if value is None:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", normalize(str(value))).strip("-")
    return TEAM_EQUIVALENCES.get(slug, slug) or None


def _team_from_api_player(
    player: dict[str, Any], api_team_ids: dict[str, str]
) -> str | None:
    team = player.get("equipo") or {}
    if not isinstance(team, dict):
        return normalise_team_slug(team)
    for field in ("slug", "name", "nickname", "shortName"):
        if team.get(field):
            return normalise_team_slug(team[field])
    team_id = team.get("id")
    return api_team_ids.get(str(team_id)) if team_id is not None else None


def api_team_id_map(players: list[dict[str, Any]]) -> dict[str, str]:
    """Map API team ids only when the same response supplies its slug or name."""
    result: dict[str, str] = {}
    for player in players:
        team = player.get("equipo") or {}
        if not isinstance(team, dict) or team.get("id") is None:
            continue
        canonical = None
        for field in ("slug", "name", "nickname", "shortName"):
            if team.get(field):
                canonical = normalise_team_slug(team[field])
                break
        if canonical:
            result[str(team["id"])] = canonical
    return result


def build_trend_team_map(
    trends: list[dict[str, Any]], lineups: list[dict[str, Any]]
) -> dict[str, str]:
    """Resolve FutbolFantasy numeric team ids by strong cross-source consensus."""
    lineup_teams_by_identity: dict[str, set[str]] = defaultdict(set)
    for player in lineups:
        team = normalise_team_slug(player.get("equipo"))
        if not team:
            continue
        for value in (player.get("nombre"), player.get("slug")):
            identity = normalise_team_slug(value)
            if identity:
                lineup_teams_by_identity[identity].add(team)

    votes: dict[str, Counter[str]] = defaultdict(Counter)
    for player in trends:
        raw_team = player.get("equipo")
        identity = normalise_team_slug(player.get("nombre"))
        if raw_team is None or not identity:
            continue
        teams = lineup_teams_by_identity.get(identity, set())
        if len(teams) == 1:
            votes[str(raw_team)][next(iter(teams))] += 1

    result: dict[str, str] = {}
    for raw_team, counts in votes.items():
        ranked = counts.most_common()
        if ranked and ranked[0][1] >= 2 and (
            len(ranked) == 1 or ranked[0][1] > ranked[1][1]
        ):
            result[raw_team] = ranked[0][0]
    return result


def load_external_data(
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str], str | None, list[str]]:
    """Read optional public signals; a failure never becomes an invented value."""
    trend_data: list[dict[str, Any]] = []
    lineup_data: list[dict[str, Any]] = []
    kickoff: str | None = None
    unavailable: list[str] = []
    try:
        trend_data = market_trends()
    except Exception:
        unavailable.append("tendencias_mercado_futbolfantasy")
    try:
        lineup_data = list(probable_lineups().values())
    except Exception:
        unavailable.append("alineaciones_probables_futbolfantasy")
    try:
        kickoff = next_kickoff()
    except Exception:
        unavailable.append("inicio_proxima_jornada_futbolfantasy")
    trend_team_map = build_trend_team_map(trend_data, lineup_data)
    return trend_data, lineup_data, trend_team_map, kickoff, unavailable


def _identity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize(str(value or ""))).strip()


def _tokens(value: Any) -> set[str]:
    return {part for part in _identity(value).split() if len(part) > 1}


def _name_confidence(
    player: dict[str, Any], candidate: dict[str, Any]
) -> tuple[int, str]:
    full = _identity(player.get("nombre_completo"))
    nickname = _identity(player.get("nombre"))
    api_slug = _identity(player.get("slug"))
    candidate_name = _identity(candidate.get("nombre"))
    candidate_slug = _identity(candidate.get("slug"))
    external_forms = {form for form in (candidate_name, candidate_slug) if form}

    if full and full in external_forms:
        return 3, "nombre completo exacto"
    if api_slug and len(_tokens(api_slug)) >= 2 and api_slug in external_forms:
        return 3, "slug de jugador exacto"
    if nickname and len(_tokens(nickname)) >= 2 and nickname in external_forms:
        return 2, "nickname exacto de varias palabras"

    full_tokens = _tokens(full)
    nickname_tokens = _tokens(nickname)
    for form in external_forms:
        external_tokens = _tokens(form)
        if len(external_tokens) >= 2 and full_tokens and (
            external_tokens <= full_tokens or full_tokens <= external_tokens
        ):
            return 2, "al menos dos tokens coinciden con el nombre completo"
        if len(external_tokens) >= 2 and len(nickname_tokens) >= 2 and (
            external_tokens <= nickname_tokens or nickname_tokens <= external_tokens
        ):
            return 2, "al menos dos tokens coinciden con el nickname"

    if nickname and any(nickname in _tokens(form) for form in external_forms):
        return 1, "solo coincide un nickname de una palabra"
    return 0, "nombre, nickname y slug no coinciden suficientemente"


def match_external_player(
    player: dict[str, Any],
    candidates: list[dict[str, Any]],
    candidate_team,
    api_team_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Match team-first and return an auditable result; never guess ambiguity."""
    api_team = _team_from_api_player(player, api_team_ids or {})
    base = {
        "candidate": None,
        "confianza_matching": "sin_coincidencia",
        "motivo_matching": "",
        "equipo_api_normalizado": api_team,
        "equipo_externo_normalizado": None,
    }
    if not api_team:
        base["motivo_matching"] = "no se pudo resolver el equipo de la API"
        return base

    same_team = [
        candidate for candidate in candidates if candidate_team(candidate) == api_team
    ]
    if not same_team:
        base["motivo_matching"] = (
            f"no hay candidatos de FutbolFantasy para el equipo {api_team}"
        )
        return base

    scored = [(_name_confidence(player, candidate), candidate) for candidate in same_team]
    sufficient = [(score, candidate) for score, candidate in scored if score[0] >= 2]
    if len(sufficient) > 1:
        base["confianza_matching"] = "baja"
        base["motivo_matching"] = (
            f"varios candidatos compatibles dentro del equipo {api_team}"
        )
        return base
    if not sufficient:
        low = [item for item in scored if item[0][0] == 1]
        base["confianza_matching"] = "baja" if low else "sin_coincidencia"
        base["motivo_matching"] = (
            f"coincidencia nominal insuficiente dentro del equipo {api_team}"
        )
        return base

    (rank, reason), candidate = sufficient[0]
    base.update(
        {
            "candidate": candidate,
            "confianza_matching": "alta" if rank == 3 else "media",
            "motivo_matching": f"equipo {api_team} confirmado; {reason}",
            "equipo_externo_normalizado": api_team,
        }
    )
    return base


def _public_match_detail(result: dict[str, Any]) -> dict[str, Any]:
    candidate = result.get("candidate") or {}
    return {
        "confianza_matching": result["confianza_matching"],
        "motivo_matching": result["motivo_matching"],
        "equipo_api_normalizado": result.get("equipo_api_normalizado"),
        "equipo_externo_normalizado": result.get("equipo_externo_normalizado"),
        "candidato_nombre": candidate.get("nombre"),
        "candidato_slug": candidate.get("slug"),
    }


def enrich_player(
    player: dict[str, Any],
    trend_data: list[dict[str, Any]],
    lineup_data: list[dict[str, Any]],
    trend_team_map: dict[str, str],
    api_team_ids: dict[str, str],
) -> None:
    trend_result = match_external_player(
        player,
        trend_data,
        lambda candidate: trend_team_map.get(str(candidate.get("equipo"))),
        api_team_ids,
    )
    lineup_result = match_external_player(
        player,
        lineup_data,
        lambda candidate: normalise_team_slug(candidate.get("equipo")),
        api_team_ids,
    )

    trend = trend_result.get("candidate")
    trend_detail = _public_match_detail(trend_result)
    api_value = player.get("valor_mercado")
    external_value = trend.get("valor") if trend else None
    value_diff = None
    suspicious_value = False
    if api_value and external_value is not None:
        value_diff = abs(external_value - api_value) / api_value
        suspicious_value = value_diff > MAX_EXTERNAL_VALUE_DIFF
    trend_detail["diferencia_valor_pct"] = (
        round(value_diff * 100, 1) if value_diff is not None else None
    )
    trend_detail["valor_sospechoso"] = suspicious_value
    if suspicious_value:
        trend_detail["motivo_matching"] += (
            f"; datos descartados por diferencia de valor superior al "
            f"{MAX_EXTERNAL_VALUE_DIFF:.0%}"
        )

    trend_payload = (
        {
            "valor_fuente": trend.get("valor"),
            "valor_1_dia": trend.get("valor1"),
            "valor_3_dias": trend.get("valor3"),
            "valor_7_dias": trend.get("valor7"),
            "tendencia": trend.get("tendencia"),
            "aceleracion": trend.get("aceleracion"),
        }
        if trend and not suspicious_value
        else None
    )
    likely = lineup_result.get("candidate")
    lineup_detail = _public_match_detail(lineup_result)
    lineup_payload = (
        {
            "probabilidad": likely.get("prob"),
            "lesionado": likely.get("lesionado"),
            "sancionado": likely.get("sancionado"),
            "disponible": likely.get("disponible"),
            "equipo_slug_fuente": likely.get("equipo"),
        }
        if likely
        else None
    )
    player["tendencia_valor_externa"] = trend_payload
    player["titularidad_externa"] = lineup_payload
    player["confianza_matching"] = {
        "tendencia_valor": trend_detail["confianza_matching"],
        "titularidad": lineup_detail["confianza_matching"],
    }
    player["motivo_matching"] = {
        "tendencia_valor": trend_detail["motivo_matching"],
        "titularidad": lineup_detail["motivo_matching"],
    }
    player["detalle_matching"] = {
        "tendencia_valor": trend_detail,
        "titularidad": lineup_detail,
    }
