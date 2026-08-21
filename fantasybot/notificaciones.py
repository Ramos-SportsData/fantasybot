"""Background notification and automated alerts scheduler for Telegram users.

Provides proactive alerts for:
  - 🛒 Daily market reset (new auctions launched by the league)
  - 🚑 Squad injuries & fitness doubt alerts
  - 🟥 Expulsions / suspensions / red cards in squad
  - ⚽ Player points gained after matches & official matchday closure
  - 🔔 Market flip/resale profit opportunities
  - 🤖 Autonomous lineup optimization (auto-lineup)
"""

import logging
import threading
import time
from typing import Dict, Any, Set, Tuple

from . import sessions
from . import ui
from ..strategy import flip as flip_mod
from ..strategy import lineup as lineup_opt
from .. import agent as agent_mod
from .. import execute as execute_mod
from ..matching import match_name

logger = logging.getLogger("fantasybot.notifications")

# State trackers to prevent duplicate alerts per user
_SEEN_MARKET_FLIPS: Dict[int, Set[str]] = {}
_LAST_SEEN_MARKET_BATCH: Dict[int, Set[str]] = {}
_LAST_PROCESSED_WEEK: Dict[int, int] = {}
_LAST_SEEN_PLAYER_STATUS: Dict[int, Dict[str, Dict[str, Any]]] = {}
_LAST_SEEN_PLAYER_POINTS: Dict[int, Dict[str, int]] = {}
_LAST_SEEN_GW_REMINDER: Dict[int, str] = {}


def start_notification_worker(bot_instance):
    """Starts the background worker thread for user notifications."""
    t = threading.Thread(target=_notification_loop, args=(bot_instance,), daemon=True)
    t.start()
    return t


def _notification_loop(bot):
    logger.info("Notification worker loop started.")
    time.sleep(10)  # Initial grace period

    while bot.running:
        try:
            chat_ids = sessions.get_all_logged_in_chat_ids()
            for chat_id in chat_ids:
                try:
                    _check_user_notifications(bot, chat_id)
                except Exception as e:
                    logger.debug("Error checking notifications for chat_id %d: %s", chat_id, e)
        except Exception as e:
            logger.error("Error in notification worker loop: %s", e)

        # Check every 5 minutes
        for _ in range(30):
            if not bot.running:
                break
            time.sleep(10)


def _check_user_notifications(bot, chat_id: int):
    settings = sessions.get_user_settings(chat_id)
    if not any(settings.values()):
        return

    client = sessions.get_client_for_user(chat_id)
    try:
        lid, tid = client.default_ids()
    except Exception:
        return

    try:
        team_data = client.team(lid, tid)
    except Exception as e:
        logger.debug("Failed to fetch team data for %d: %s", chat_id, e)
        team_data = None

    # 1. Squad Injuries and Suspensions / Expulsions Alerts
    if team_data:
        _check_player_injuries_and_expulsions(bot, chat_id, team_data, settings)

    # 2. Player Match Points & Matchday Final Rewards
    if team_data:
        _check_player_points(bot, chat_id, client, lid, tid, team_data, settings)

    # 3. Market Daily Reset Notification
    if settings.get("notify_market_reset", True):
        _check_market_reset(bot, chat_id, client, lid)

    # 4. Market Flips Opportunities
    if settings.get("notify_flips", True) and team_data:
        _check_market_flips(bot, chat_id, client, lid, team_data)

    # 5. Gameweek 6-Hour Countdown Alert & Negative Balance Warning
    if settings.get("notify_gameweek_6h", True) and team_data:
        _check_gameweek_reminder(bot, chat_id, client, lid, tid, team_data, settings)

    # 6. Auto-Lineup Automation
    if settings.get("auto_lineup", False) and team_data:
        _check_auto_lineup(bot, chat_id, client, lid, tid, team_data)


def _check_player_injuries_and_expulsions(bot, chat_id: int, team_data: Dict[str, Any], settings: Dict[str, bool]):
    """Checks for state transitions in the user's squad (injury, doubtful, suspension, recovery)."""
    notify_inj = settings.get("notify_injuries", True)
    notify_exp = settings.get("notify_expulsions", True)
    if not notify_inj and not notify_exp:
        return

    prob_index = {}
    try:
        from ..sources.lineups import probable_lineups
        prob_index = probable_lineups() or {}
    except Exception:
        pass

    players = team_data.get("players", [])
    current_status_map: Dict[str, Dict[str, Any]] = {}

    for p in players:
        pm = p.get("playerMaster") or {}
        pid = str(pm.get("id") or p.get("playerTeamId") or "")
        if not pid:
            continue
        name = pm.get("nickname") or pm.get("name") or "Jugador"
        pos_id = pm.get("positionId")
        pos_str = ui.POS.get(pos_id, "?")

        raw_status = (pm.get("playerStatus") or "ok").lower()
        info = match_name(name, pm.get("name", ""), prob_index) if prob_index else None

        # Determine normalized status type
        is_suspended = (
            raw_status in ("suspended", "sanctioned", "sancionado", "expelled", "redcard", "tarjeta_roja")
            or (info and info.get("sancionado") is True)
        )
        is_injured = (
            raw_status in ("injured", "lesionado")
            or (info and info.get("lesionado") is True)
        )
        is_doubtful = (
            raw_status in ("doubtful", "duda", "warned")
        )

        if is_suspended:
            st_type = "suspended"
        elif is_injured:
            st_type = "injured"
        elif is_doubtful:
            st_type = "doubtful"
        else:
            st_type = "ok"

        current_status_map[pid] = {
            "name": name,
            "pos": pos_str,
            "status_type": st_type,
            "raw_status": raw_status,
        }

    prev_status_map = _LAST_SEEN_PLAYER_STATUS.get(chat_id)

    # First run for this user: initialize baseline without spamming
    if prev_status_map is None:
        _LAST_SEEN_PLAYER_STATUS[chat_id] = current_status_map
        return

    # Check state transitions
    for pid, cur in current_status_map.items():
        prev = prev_status_map.get(pid)
        prev_type = prev.get("status_type", "ok") if prev else "ok"
        cur_type = cur["status_type"]
        name = cur["name"]
        pos = cur["pos"]

        # 1. Transition to Injured
        if cur_type == "injured" and prev_type != "injured" and notify_inj:
            bot.send_message(
                chat_id,
                f"🚑 <b>¡Alerta de Lesión en tu Plantilla!</b>\n\n"
                f"• <b>{name}</b> ({pos}) ha pasado a estado <b>Lesionado</b> ⚠️\n\n"
                f"<i>💡 Te recomendamos revisar tu alineación y preparar un sustituto si formaba parte de tu XI titular.</i>",
                reply_markup=ui.team_keyboard()
            )

        # 2. Transition to Doubtful
        elif cur_type == "doubtful" and prev_type == "ok" and notify_inj:
            bot.send_message(
                chat_id,
                f"⚠️ <b>¡Alerta de Duda / Molestias Físicas!</b>\n\n"
                f"• <b>{name}</b> ({pos}) figura como <b>Duda</b> para la próxima jornada.\n\n"
                f"<i>💡 Permanece atento a las convocatorias antes del inicio de la jornada.</i>",
                reply_markup=ui.team_keyboard()
            )

        # 3. Transition to Suspended / Expelled
        elif cur_type == "suspended" and prev_type != "suspended" and notify_exp:
            bot.send_message(
                chat_id,
                f"🟥 <b>¡Alerta de Sanción / Expulsión!</b>\n\n"
                f"• <b>{name}</b> ({pos}) ha sido <b>Sancionado o Expulsado</b> ⛔\n\n"
                f"<i>💡 Recuerda cambiarlo en tu alineación antes del bloqueo de la jornada para no jugar con uno menos.</i>",
                reply_markup=ui.team_keyboard()
            )

        # 4. Recovery
        elif cur_type == "ok" and prev_type in ("injured", "suspended") and notify_inj:
            bot.send_message(
                chat_id,
                f"✅ <b>¡Jugador Recuperado y Disponible!</b>\n\n"
                f"• <b>{name}</b> ({pos}) vuelve a estar al <b>100% disponible</b> para jugar. ⚽",
                reply_markup=ui.team_keyboard()
            )

    _LAST_SEEN_PLAYER_STATUS[chat_id] = current_status_map


def _check_player_points(bot, chat_id: int, client, lid: Any, tid: Any, team_data: Dict[str, Any], settings: Dict[str, bool]):
    """Tracks player point increments after matches and sends matchday summary reports."""
    notify_pts = settings.get("notify_player_points", True) or settings.get("notify_matchday_points", True)
    if not notify_pts:
        return

    players = team_data.get("players", [])
    current_points_map: Dict[str, Tuple[str, str, int]] = {}

    for p in players:
        pm = p.get("playerMaster") or {}
        pid = str(pm.get("id") or p.get("playerTeamId") or "")
        if not pid:
            continue
        name = pm.get("nickname") or pm.get("name") or "Jugador"
        pos_str = ui.POS.get(pm.get("positionId"), "?")
        pts = int(pm.get("points") or 0)
        current_points_map[pid] = (name, pos_str, pts)

    prev_points_map = _LAST_SEEN_PLAYER_POINTS.get(chat_id)

    if prev_points_map is None:
        _LAST_SEEN_PLAYER_POINTS[chat_id] = {pid: v[2] for pid, v in current_points_map.items()}
    else:
        # Detect any player whose points increased (e.g. at the end of a match)
        gains = []
        for pid, (name, pos_str, cur_pts) in current_points_map.items():
            prev_p = prev_points_map.get(pid)
            if prev_p is not None and cur_pts > prev_p:
                diff = cur_pts - prev_p
                gains.append((name, pos_str, diff, cur_pts))

        if gains:
            gains.sort(key=lambda x: -x[2])
            lines = [
                "⚽ <b>¡Puntuaciones de Partido Actualizadas!</b>\n",
                "Tus jugadores han sumado nuevos puntos en sus encuentros:\n"
            ]
            for name, pos_str, diff, total_p in gains:
                star = " ⭐" if diff >= 8 else (" 🔥" if diff >= 6 else "")
                lines.append(f"• <b>{name}</b> ({pos_str}): <b>+{diff} pts</b> (Total: {total_p} pts){star}")

            team_pts = team_data.get("teamPoints", 0)
            team_pos = team_data.get("position", "-")
            lines.append(f"\n📊 <b>Total de tu equipo:</b> <b>{team_pts} pts</b> (Posición #{team_pos})")
            bot.send_message(chat_id, "\n".join(lines), reply_markup=ui.team_keyboard())

        _LAST_SEEN_PLAYER_POINTS[chat_id] = {pid: v[2] for pid, v in current_points_map.items()}

    # Check official matchday closure event in activity feed
    try:
        activity = client.league_activity(lid, fetch_all=False)
        reward_events = [ev for ev in (activity or []) if ev.get("type") == 6 and str(ev.get("data", {}).get("team", {}).get("id")) == str(tid)]
        if reward_events:
            latest_reward = reward_events[0]
            ev_data = latest_reward.get("data", {})
            week_num = ev_data.get("week") or ev_data.get("weekNumber") or 1
            last_week = _LAST_PROCESSED_WEEK.get(chat_id)

            if last_week is not None and week_num > last_week:
                _LAST_PROCESSED_WEEK[chat_id] = week_num
                pts = ev_data.get("points", 0)
                money = ev_data.get("money", 0)
                pos = team_data.get("position", "-")

                lines = [
                    f"🏆 <b>¡Puntuaciones de la Jornada {week_num} Publicadas!</b>\n",
                    f"📊 <b>Puntos conseguidos:</b> <b>{pts} pts</b> (Posición #{pos})",
                    f"💰 <b>Prima recibida:</b> +{ui.fmt_eur(money)}\n",
                    "⚽ <b>Puntos de tus jugadores:</b>"
                ]

                players_with_pts = []
                for p in players:
                    pm = p.get("playerMaster", {})
                    p_pts = pm.get("points") or 0
                    name = pm.get("nickname") or pm.get("name") or "Jugador"
                    pos_str = ui.POS.get(pm.get("positionId"), "?")
                    players_with_pts.append((name, pos_str, p_pts))

                players_with_pts.sort(key=lambda x: -x[2])
                for name, pos_str, p_pts in players_with_pts[:11]:
                    star = " ⭐" if p_pts >= 10 else ""
                    lines.append(f"• <b>{name}</b> ({pos_str}): <b>{p_pts} pts</b>{star}")

                bot.send_message(chat_id, "\n".join(lines), reply_markup=ui.team_keyboard())
            elif last_week is None:
                _LAST_PROCESSED_WEEK[chat_id] = week_num
    except Exception as e:
        logger.debug("Error checking matchday activity reward for %d: %s", chat_id, e)


def _check_market_reset(bot, chat_id: int, client, lid: Any):
    """Detects when the daily market resets with new players auctioned by the league."""
    try:
        market_items = client.market(lid)
        current_ids = {str(it.get("id")) for it in (market_items or []) if it.get("id")}
        last_batch = _LAST_SEEN_MARKET_BATCH.get(chat_id)

        if last_batch is not None:
            new_in_market = [it for it in (market_items or []) if str(it.get("id")) not in last_batch and it.get("discr") == "marketPlayerLeague"]
            if len(new_in_market) >= 2:  # New market arrival / daily reset
                leagues = client.leagues()
                cur_lg = next((l for l in (leagues or []) if str(l.get("id")) == str(lid)), {})
                lg_name = cur_lg.get("name", "Tu Liga")

                lines = [
                    f"🛒 <b>¡Mercado Diario Renovado! ({lg_name})</b>\n",
                    f"✨ Han salido <b>{len(new_in_market)} nuevos jugadores</b> a subasta hoy:\n"
                ]
                new_in_market.sort(key=lambda x: -(x.get("playerMaster", {}).get("marketValue") or 0))
                for it in new_in_market[:8]:
                    pm = it.get("playerMaster", {})
                    name = pm.get("nickname") or pm.get("name") or "Jugador"
                    pos = ui.POS.get(pm.get("positionId"), "?")
                    val = pm.get("marketValue") or it.get("price") or 0
                    lines.append(f"• <b>{name}</b> ({pos}): {ui.fmt_eur(val)}")

                lines.append("\n<i>💡 Pulsa en Mercado en Vivo para consultar la lista completa.</i>")
                bot.send_message(chat_id, "\n".join(lines))

        _LAST_SEEN_MARKET_BATCH[chat_id] = current_ids
    except Exception as e:
        logger.debug("Error checking market reset for %d: %s", chat_id, e)


def _check_market_flips(bot, chat_id: int, client, lid: Any, team_data: Dict[str, Any]):
    """Finds and notifies about high-margin flip opportunities."""
    try:
        owned = {p.get("playerMaster", {}).get("id") for p in team_data.get("players", []) if p.get("playerMaster", {}).get("id")}
        flips = flip_mod.opportunities(client, lid, owned=owned)
        profitable = [f for f in flips if f.get("via") == "SISTEMA" and f.get("margin", 0) > 200_000 and f.get("margin_pct", 0) >= 3.0]

        seen = _SEEN_MARKET_FLIPS.setdefault(chat_id, set())
        new_flips = [f for f in profitable if f["market_id"] not in seen]

        if new_flips:
            for f in new_flips:
                seen.add(f["market_id"])

            lines = ["🔔 <b>¡Nuevas Oportunidades de Reventa (Flip) en tu Mercado!</b>\n"]
            for f in new_flips[:4]:
                diff_sign = "+" if f.get("margin", 0) >= 0 else ""
                lines.append(
                    f"• <b>{f['nombre']}</b> ({f['pos']})\n"
                    f"  💵 Compra: {ui.fmt_eur(f['buy_price'])} → Proy: {ui.fmt_eur(f['proyeccion'])}\n"
                    f"  📈 Margen: <b>{diff_sign}{ui.fmt_eur(f['margin'])}</b> ({f['margin_pct']:+.1f}%)\n"
                )
            lines.append("<i>💡 Pulsa en Oportunidades (Flip) en el menú para verlas.</i>")
            bot.send_message(chat_id, "\n".join(lines))
    except Exception as e:
        logger.debug("Error checking market flips for %d: %s", chat_id, e)


def _check_gameweek_reminder(bot, chat_id: int, client, lid: Any, tid: Any, team_data: Dict[str, Any], settings: Dict[str, bool]):
    """Sends a proactive smart alert ~6 hours before the upcoming gameweek kickoff deadline."""
    if not settings.get("notify_gameweek_6h", True):
        return

    try:
        from ..sources import matchday
        from datetime import datetime, timezone
        gw_kickoff_iso = matchday.next_gameweek_kickoff()
        if not gw_kickoff_iso:
            return

        iso_clean = gw_kickoff_iso.replace("Z", "+00:00")
        kickoff_dt = datetime.fromisoformat(iso_clean)
        if kickoff_dt.tzinfo is None:
            kickoff_dt = kickoff_dt.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        time_left_sec = (kickoff_dt - now).total_seconds()

        # Alert window: between 0 and 6 hours (+10 min buffer)
        if 0 < time_left_sec <= (6 * 3600 + 600):
            last_notified = _LAST_SEEN_GW_REMINDER.get(chat_id)
            if last_notified == gw_kickoff_iso:
                return

            _LAST_SEEN_GW_REMINDER[chat_id] = gw_kickoff_iso

            hours_left = max(1, int(round(time_left_sec / 3600.0)))
            money = team_data.get("teamMoney", 0)

            lines = [
                f"⏰ <b>¡AVISO DE JORNADA: Faltan ~{hours_left}h para el Inicio!</b> ⚽",
                f"📅 <b>Límite Alineación:</b> {kickoff_dt.strftime('%d/%m a las %H:%M UTC')}\n"
            ]

            if money < 0:
                lines.append(
                    f"🚨 <b>¡SALDO NEGATIVO DETECTADO! ({ui.fmt_eur(money)})</b>\n"
                    f"⚠️ <i>Recuerda que si estás en números rojos al arrancar el primer partido, NO sumarás ningún punto esta jornada. ¡Vende a algún jugador antes del cierre!</i>\n"
                )
            else:
                lines.append(f"💰 <b>Tu Saldo:</b> {ui.fmt_eur(money)} (Positivo ✅)\n")

            # Check lineup status
            best = lineup_opt.optimize(team_data)
            current_ids = agent_mod._current_xi_ids(client, tid)
            best_ids = set(p["playerMaster"]["id"] for p in best["xi"])
            if set(current_ids) == best_ids:
                d, m, f = best["formation"]
                lines.append(f"⚽ <b>Alineación:</b> Tu XI actual ya coincide con el óptimo ({d}-{m}-{f}) ✅")
            else:
                d, m, f = best["formation"]
                lines.append(
                    f"⚠️ <b>Recomendación de Once:</b> Hay mejoras posibles respecto a tu XI actual.\n"
                    f"El XI Óptimo sugerido es un <b>{d}-{m}-{f}</b>."
                )

            lines.append("\n<i>👉 Escribe /lineup para revisar tu equipo o /autopilot para optimizarlo al instante.</i>")

            bot.send_message(chat_id, "\n".join(lines), reply_markup=ui.lineup_keyboard(can_apply=True))
    except Exception as e:
        logger.debug("Error checking gameweek reminder for %d: %s", chat_id, e)


def _check_auto_lineup(bot, chat_id: int, client, lid: Any, tid: Any, team_data: Dict[str, Any]):
    """Optimizes and automatically submits the best XI if auto-lineup is enabled."""
    try:
        best = lineup_opt.optimize(team_data)
        current_ids = agent_mod._current_xi_ids(client, tid)
        res = execute_mod.apply_lineup(client, tid, best, current_ids, dry_run=False)
        if res.get("changed"):
            d, m, f = best["formation"]
            bot.send_message(
                chat_id,
                f"🤖 <b>Auto-Alinear Ejecutado:</b>\n\n"
                f"Tu alineación ha sido actualizada automáticamente al <b>XI Óptimo ({d}-{m}-{f})</b> para la próxima jornada. ⚽"
            )
    except Exception as e:
        logger.debug("Error in auto-lineup for %d: %s", chat_id, e)
