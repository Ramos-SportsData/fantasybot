"""Market projections and canonical market-section classification."""

from __future__ import annotations

from typing import Any

from .players import summarize_offer, summarize_player
from .security import strip_sensitive


def summarize_market_owner(
    entry: dict[str, Any], own_team_id: Any
) -> dict[str, Any] | None:
    market_type = entry.get("discr")
    if market_type == "marketPlayerLeague":
        return {
            "tipo": "laliga",
            "equipo_id": None,
            "manager_id": None,
            "nombre_manager": None,
            "es_mi_equipo": False,
        }
    if market_type != "marketPlayerTeam":
        return None

    seller = entry.get("sellerTeam")
    if not isinstance(seller, dict):
        return None
    manager = seller.get("manager")
    if not isinstance(manager, dict):
        manager = {}
    seller_id = seller.get("id")
    manager_id = seller.get("managerId") or manager.get("id")
    manager_name = manager.get("managerName") or manager.get("name")
    return strip_sensitive(
        {
            "tipo": "manager",
            "equipo_id": seller_id,
            "manager_id": manager_id,
            "nombre_manager": manager_name,
            "es_mi_equipo": (
                str(seller_id) == str(own_team_id)
                if seller_id is not None and own_team_id is not None
                else None
            ),
        }
    )


def summarize_market(entry: Any, own_team_id: Any = None) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    player = summarize_player(entry)
    if player is None:
        return None
    player_team = entry.get("playerTeam")
    summary = {
        "market_id": entry.get("id"),
        "tipo": entry.get("discr"),
        "estado": entry.get("status"),
        "vence": entry.get("expirationDate"),
        "precio_venta": entry.get("salePrice"),
        "numero_pujas": entry.get("numberOfBids"),
        "numero_ofertas": entry.get("numberOfOffers"),
        "oferta_directa": entry.get("directOffer"),
        "oferta_existente": summarize_offer(entry.get("offer")),
        "puja_existente": summarize_offer(entry.get("bid")),
        "puja_explicita_disponible": "bid" in entry,
        "jugador": player,
        "clausula": player_team.get("buyoutClause")
        if isinstance(player_team, dict)
        else None,
        "clausula_bloqueada_hasta": player_team.get("buyoutClauseLockedEndTime")
        if isinstance(player_team, dict)
        else None,
        "vendedor": strip_sensitive(entry.get("sellerTeam")),
        "propietario": summarize_market_owner(entry, own_team_id),
    }
    return strip_sensitive(summary)


def _own_listing_from_squad(
    player: dict[str, Any], own_team_id: Any
) -> dict[str, Any] | None:
    listing = player.get("en_mercado")
    if not isinstance(listing, dict):
        return None
    return {
        "market_id": listing.get("id"),
        "tipo": listing.get("tipo"),
        "estado": listing.get("estado"),
        "vence": listing.get("vence"),
        "precio_venta": listing.get("precio_venta"),
        "numero_pujas": listing.get("numero_pujas"),
        "numero_ofertas": listing.get("numero_ofertas"),
        "oferta_directa": listing.get("oferta_directa"),
        "oferta_existente": listing.get("oferta"),
        "puja_existente": None,
        "puja_explicita_disponible": False,
        "jugador": player,
        "clausula": player.get("clausula"),
        "clausula_bloqueada_hasta": player.get("clausula_bloqueada_hasta"),
        "vendedor": None,
        "propietario": {
            "tipo": "manager",
            "equipo_id": own_team_id,
            "manager_id": None,
            "nombre_manager": None,
            "es_mi_equipo": True,
        },
    }


def build_market_sections(
    market: list[dict[str, Any]],
    squad: list[dict[str, Any]],
    own_team_id: Any,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Split market data without turning global counters into user actions."""
    limitations: list[dict[str, str]] = []
    league_market = [item for item in market if item.get("tipo") == "marketPlayerLeague"]
    team_market = [item for item in market if item.get("tipo") == "marketPlayerTeam"]
    unknown_market = [
        item
        for item in market
        if item.get("tipo") not in {"marketPlayerLeague", "marketPlayerTeam"}
    ]

    own_listings = [
        item
        for item in team_market
        if (item.get("propietario") or {}).get("es_mi_equipo") is True
    ]
    known_listing_ids = {
        str(item.get("market_id"))
        for item in own_listings
        if item.get("market_id") is not None
    }
    for player in squad:
        fallback = _own_listing_from_squad(player, own_team_id)
        if fallback is None:
            continue
        listing_id = fallback.get("market_id")
        if listing_id is not None and str(listing_id) in known_listing_ids:
            continue
        own_listings.append(fallback)
        if listing_id is not None:
            known_listing_ids.add(str(listing_id))

    manager_market = [
        item
        for item in team_market
        if (item.get("propietario") or {}).get("es_mi_equipo") is not True
    ]
    if any(item.get("propietario") is None for item in manager_market):
        limitations.append(
            {
                "codigo": "propietario_mercado_no_disponible",
                "campo": "mercado_managers[].propietario",
                "detalle": (
                    "La respuesta GET no identificó al equipo o manager vendedor; "
                    "propietario queda en null y el listado no se atribuye."
                ),
            }
        )

    explicit_bid_supported = any(
        item.get("puja_explicita_disponible") is True for item in league_market
    )
    if explicit_bid_supported:
        my_bids: list[dict[str, Any]] | None = [
            {
                "market_id": item.get("market_id"),
                "jugador": item.get("jugador"),
                "puja": item.get("puja_existente"),
            }
            for item in league_market
            if item.get("puja_existente") is not None
        ]
    else:
        my_bids = None
        limitations.append(
            {
                "codigo": "mis_pujas_no_disponibles",
                "campo": "mis_pujas",
                "detalle": (
                    "El endpoint GET de mercado solo expone numberOfBids, un contador "
                    "global que no permite identificar una puja propia."
                ),
            }
        )

    received_offers: list[dict[str, Any]] = []
    received_offer_state_known = True
    missing_received_offer_details = False
    for item in own_listings:
        offer = item.get("oferta_existente")
        offer_count = item.get("numero_ofertas")
        if offer is not None:
            received_offers.append(
                {
                    "market_id": item.get("market_id"),
                    "jugador": item.get("jugador"),
                    "oferta": offer,
                }
            )
        elif isinstance(offer_count, int) and offer_count > 0:
            received_offers.append(
                {
                    "market_id": item.get("market_id"),
                    "jugador": item.get("jugador"),
                    "numero_ofertas": offer_count,
                    "oferta": None,
                }
            )
            missing_received_offer_details = True
        elif offer_count is None:
            received_offer_state_known = False
    my_received_offers: list[dict[str, Any]] | None = (
        received_offers if received_offers or received_offer_state_known else None
    )
    if missing_received_offer_details:
        limitations.append(
            {
                "codigo": "detalle_ofertas_recibidas_no_disponible",
                "campo": "mis_ofertas_recibidas[].oferta",
                "detalle": (
                    "La API informa del número de ofertas recibidas, pero no expone "
                    "sus detalles; oferta queda en null."
                ),
            }
        )
    elif my_received_offers is None:
        limitations.append(
            {
                "codigo": "mis_ofertas_recibidas_no_disponibles",
                "campo": "mis_ofertas_recibidas",
                "detalle": (
                    "La respuesta GET no permite determinar si existen ofertas "
                    "recibidas; el campo queda en null."
                ),
            }
        )

    activity = []
    for item in market:
        bid_count = item.get("numero_pujas")
        offer_count = item.get("numero_ofertas")
        explicit_bid = item.get("puja_existente")
        explicit_offer = item.get("oferta_existente")
        if not any(
            (
                isinstance(bid_count, int) and bid_count > 0,
                isinstance(offer_count, int) and offer_count > 0,
                explicit_bid is not None,
                explicit_offer is not None,
            )
        ):
            continue
        owner = item.get("propietario") or {}
        activity.append(
            {
                "market_id": item.get("market_id"),
                "tipo_mercado": item.get("tipo"),
                "jugador": item.get("jugador", {}).get("nombre"),
                "propietario": item.get("propietario"),
                "numero_pujas_globales": bid_count,
                "numero_ofertas_globales": offer_count,
                "mi_puja": explicit_bid,
                "mi_oferta_enviada": (
                    explicit_offer
                    if owner.get("es_mi_equipo") is False
                    and owner.get("tipo") == "manager"
                    else None
                ),
                "oferta_recibida": (
                    explicit_offer if owner.get("es_mi_equipo") is True else None
                ),
            }
        )

    if unknown_market:
        limitations.append(
            {
                "codigo": "tipo_mercado_desconocido",
                "campo": "mercado",
                "detalle": (
                    "Hay elementos con un discriminador no reconocido; se conservan "
                    "en el campo de compatibilidad mercado sin clasificarlos."
                ),
            }
        )

    return (
        {
            "mercado_laliga": league_market,
            "mercado_managers": manager_market,
            "mis_jugadores_publicados": own_listings,
            "mis_pujas": my_bids,
            "mis_ofertas_recibidas": my_received_offers,
            "actividad_mercado": activity,
        },
        limitations,
    )
