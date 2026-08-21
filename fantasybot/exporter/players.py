"""Player, lineup, team, listing, and offer projections for the export."""

from __future__ import annotations

from typing import Any

from fantasybot.matching import POS

from .security import strip_sensitive


def summarize_team(team: Any) -> Any:
    if not isinstance(team, dict):
        return team
    return strip_sensitive(
        {
            key: team.get(key)
            for key in ("id", "name", "nickname", "slug", "shortName")
            if team.get(key) is not None
        }
    )


def summarize_offer(offer: Any) -> Any:
    # Offer schemas can evolve. Keep all useful response fields but pass them
    # through the same credential filter and final validator.
    return strip_sensitive(offer) if isinstance(offer, dict) else offer


def summarize_listing(listing: Any) -> Any:
    if not isinstance(listing, dict):
        return None
    return strip_sensitive(
        {
            "id": listing.get("id"),
            "tipo": listing.get("discr"),
            "estado": listing.get("status"),
            "precio_venta": listing.get("salePrice"),
            "vence": listing.get("expirationDate"),
            "numero_pujas": listing.get("numberOfBids"),
            "numero_ofertas": listing.get("numberOfOffers"),
            "oferta_directa": listing.get("directOffer"),
            "oferta": summarize_offer(listing.get("offer")),
        }
    )


def summarize_player(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    master = entry.get("playerMaster")
    if not isinstance(master, dict):
        return None
    position_id = master.get("positionId")
    player_team = master.get("team")
    if not isinstance(player_team, dict) and master.get("teamId") is not None:
        player_team = {"id": master.get("teamId")}
    summary = {
        "player_team_id": entry.get("playerTeamId"),
        "id": master.get("id"),
        "nombre": master.get("nickname") or master.get("name"),
        "nombre_completo": master.get("name"),
        "slug": master.get("slug"),
        "posicion_id": position_id,
        "posicion": POS.get(position_id, master.get("position")),
        "equipo": summarize_team(player_team),
        "valor_mercado": master.get("marketValue"),
        "puntos": master.get("points"),
        "media_puntos": master.get("averagePoints"),
        "puntos_ultima_temporada": master.get("lastSeasonPoints"),
        "puntos_jornada": master.get("weekPoints"),
        "estado_api": master.get("playerStatus"),
        "ultimas_estadisticas": strip_sensitive(master.get("lastStats") or []),
        "clausula": entry.get("buyoutClause"),
        "clausula_bloqueada_hasta": entry.get("buyoutClauseLockedEndTime"),
        "protegido": entry.get("isShielded"),
        "en_mercado": summarize_listing(entry.get("playerMarket")),
    }
    return strip_sensitive(summary)


def summarize_lineup_group(group: Any) -> list[dict[str, Any]]:
    if group is None:
        return []
    entries = group if isinstance(group, list) else [group]
    return [
        player for item in entries if (player := summarize_player(item)) is not None
    ]


def summarize_lineup(lineup: Any) -> dict[str, Any]:
    if not isinstance(lineup, dict):
        return {}
    formation = lineup.get("formation") or {}
    bench = formation.get("bench") or {}
    positions = ("goalkeeper", "defender", "midfield", "striker")
    return strip_sensitive(
        {
            "id": lineup.get("id"),
            "actualizada": lineup.get("updatedAt"),
            "formacion_tactica": formation.get("tacticalFormation"),
            "capitan_player_team_id": formation.get("captain"),
            "titulares": {
                position: summarize_lineup_group(formation.get(position))
                for position in positions
            },
            "banquillo": {
                position: summarize_lineup_group(bench.get(position))
                for position in positions
            },
            "entrenador": summarize_lineup_group(formation.get("coach")),
        }
    )


def latest_stats_week(players: list[dict[str, Any]]) -> int | None:
    weeks: list[int] = []
    for player in players:
        for stat in player.get("ultimas_estadisticas") or []:
            if isinstance(stat, dict) and isinstance(stat.get("weekNumber"), int):
                weeks.append(stat["weekNumber"])
    return max(weeks) if weeks else None
