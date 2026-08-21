"""Composition of account reads and pure projections into the export JSON."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable

from fantasybot import config
from fantasybot.api import FantasyError

from .matching import api_team_id_map, enrich_player, load_external_data
from .market import build_market_sections, summarize_market
from .players import latest_stats_week, summarize_lineup, summarize_player
from .security import strip_sensitive, validate_safe


ExternalData = tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    str | None,
    list[str],
]


def select_league(
    leagues: list[dict[str, Any]],
) -> tuple[dict[str, Any], Any, Any]:
    if not leagues:
        raise FantasyError("The user has no leagues.")
    wanted = os.environ.get("FANTASYBOT_LEAGUE")
    selected = (
        next(
            (league for league in leagues if str(league.get("id")) == str(wanted)),
            None,
        )
        if wanted
        else leagues[0]
    )
    if selected is None:
        raise FantasyError("The configured league is not in this account.")
    team = selected.get("team") or {}
    return selected, selected.get("id"), str(team.get("id"))


def build_export(
    client,
    external_data_loader: Callable[[], ExternalData] | None = None,
) -> dict[str, Any]:
    # These are the same four account reads used by the existing CLI commands.
    leagues_raw = client.leagues()
    selected, league_id, team_id = select_league(leagues_raw)
    team_raw = client.team(league_id, team_id)
    market_raw = client.market(league_id)
    lineup_raw = client.lineup(team_id)

    squad = [
        player
        for item in team_raw.get("players", [])
        if (player := summarize_player(item)) is not None
    ]
    market = [
        item
        for raw in market_raw
        if (item := summarize_market(raw, team_id)) is not None
    ]
    external_loader = external_data_loader or load_external_data
    trend_data, probable_data, trend_team_map, kickoff, unavailable = (
        external_loader()
    )
    api_team_ids = api_team_id_map(squad)
    for player in squad:
        enrich_player(player, trend_data, probable_data, trend_team_map, api_team_ids)
    for item in market:
        enrich_player(
            item["jugador"], trend_data, probable_data, trend_team_map, api_team_ids
        )
    market_sections, market_limitations = build_market_sections(
        market, squad, team_id
    )
    own_listings = [
        {"jugador": player["nombre"], "detalle": player["en_mercado"]}
        for player in squad
        if player.get("en_mercado")
    ]
    market_activity = [
        {
            "market_id": item.get("market_id"),
            "jugador": item.get("jugador", {}).get("nombre"),
            "numero_pujas": item.get("numero_pujas"),
            "numero_ofertas": item.get("numero_ofertas"),
            "oferta_existente": item.get("oferta_existente"),
        }
        for item in market
        if item.get("numero_pujas")
        or item.get("numero_ofertas")
        or item.get("oferta_existente")
    ]
    league_summaries = []
    for league in leagues_raw:
        league_team = league.get("team") or {}
        league_summaries.append(
            strip_sensitive(
                {
                    "id": league.get("id"),
                    "nombre": league.get("name"),
                    "descripcion": league.get("description"),
                    "tipo": league.get("type", {}).get("id")
                    if isinstance(league.get("type"), dict)
                    else league.get("type"),
                    "numero_managers": league.get("managersNumber"),
                    "premium": league.get("premium"),
                    "mi_equipo": {
                        "id": league_team.get("id"),
                        "saldo": league_team.get("money"),
                        "valor": league_team.get("teamValue"),
                        "puntos": league_team.get("teamPoints"),
                        "jugadores": league_team.get("playersNumber"),
                    },
                }
            )
        )

    exported = {
        "meta": {
            "generado_en": datetime.now(timezone.utc).isoformat(),
            "modo": "solo_lectura_GET_sin_refresco_oauth",
            "fuente": "LALIGA Fantasy API mediante fantasybot.FantasyClient",
            "liga_seleccionada_id": league_id,
            "limitaciones": market_limitations,
            "campos_deprecados": {
                "mercado": {
                    "deprecado": True,
                    "sustituido_por": [
                        "mercado_laliga",
                        "mercado_managers",
                        "mis_jugadores_publicados",
                    ],
                },
                "pujas_y_ofertas": {
                    "deprecado": True,
                    "sustituido_por": [
                        "mis_jugadores_publicados",
                        "mis_pujas",
                        "mis_ofertas_recibidas",
                        "actividad_mercado",
                    ],
                },
            },
            "fuentes_externas_lectura": {
                "tendencias": config.FF_MARKET_URL,
                "alineaciones_probables": config.FF_LINEUPS_INDEX,
                "no_disponibles": unavailable,
            },
        },
        "ligas": league_summaries,
        "equipo": {
            "id": team_raw.get("id"),
            "saldo_disponible": team_raw.get("teamMoney"),
            "valor_total": team_raw.get("teamValue"),
            "puntos": team_raw.get("teamPoints"),
            "numero_jugadores": team_raw.get("playersNumber"),
            "plantilla": squad,
        },
        **market_sections,
        "mercado": market,
        "pujas_y_ofertas": {
            "nota": (
                "Campo deprecado conservado por compatibilidad. Los contadores "
                "son globales y no identifican por sí solos al autor."
            ),
            "actividad_visible": market_activity,
            "mis_jugadores_publicados": own_listings,
        },
        "alineacion_actual": summarize_lineup(lineup_raw),
        "jornadas": {
            "actual": None,
            "proxima": None,
            "proximo_partido_fuente_externa": {
                "inicio": kickoff,
                "fuente": "FutbolFantasy",
            }
            if kickoff
            else None,
            "campos_auxiliares_api": {
                "semana_inicio_equipo": team_raw.get("startingWeek"),
                "ultima_semana_en_estadisticas": latest_stats_week(squad),
            },
            "nota": (
                "La API de cuenta no expone campos explícitos de jornada actual o "
                "próxima. La fecha externa es el próximo partido pendiente que su "
                "lector encuentra, no necesariamente el comienzo de una jornada."
            ),
        },
        "disponibilidad": {
            "nota": (
                "estado_api conserva el estado literal de LALIGA Fantasy. Las "
                "banderas externas de lesión/sanción se mantienen separadas y "
                "pueden no coincidir con la API."
            ),
            "jugadores_no_ok": [
                {
                    "id": player.get("id"),
                    "nombre": player.get("nombre"),
                    "estado_api": player.get("estado_api"),
                }
                for player in squad
                if str(player.get("estado_api") or "ok").lower() != "ok"
            ],
            "alertas_fuente_externa": [
                {
                    "id": player.get("id"),
                    "nombre": player.get("nombre"),
                    **(player.get("titularidad_externa") or {}),
                }
                for player in squad
                if (player.get("titularidad_externa") or {}).get("lesionado")
                or (player.get("titularidad_externa") or {}).get("sancionado")
                or (player.get("titularidad_externa") or {}).get("disponible") is False
            ],
        },
    }
    exported = strip_sensitive(exported)
    validate_safe(exported)
    return exported
