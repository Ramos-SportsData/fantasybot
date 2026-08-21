"""Telegram notifications.

Sends the agent's review summary, executed actions, and errors to a Telegram
chat via the Bot API (stdlib only, no extra dependency — consistent with the
rest of the project).

Configuration (env vars, same ones already wired in the GH Actions workflow):
  TELEGRAM_TOKEN    Bot token from @BotFather
  TELEGRAM_CHAT_ID  Numeric chat id to send messages to

If either is missing, sending is silently skipped (this must never break a
run — notifications are best-effort, like fantasybot's own `events.emit`).
"""

import json
import os
import urllib.error
import urllib.request

API_BASE = "https://api.telegram.org"


def _enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_TOKEN")) and bool(os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str, log=print) -> bool:
    """Sends a message to the configured chat. Returns True on success.

    Never raises: a Telegram outage or bad config must not break the bot's
    actual work (lineup/bids). Logs the failure and moves on.
    """
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False  # not configured — nothing to do, not an error

    url = f"{API_BASE}/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        log(f"[telegram] HTTP {e.code} sending message: {detail}")
    except urllib.error.URLError as e:
        log(f"[telegram] Network error sending message: {e.reason}")
    except Exception as e:
        log(f"[telegram] Unexpected error sending message: {e}")
    return False


def _fmt_money(n) -> str:
    return f"{n:,}".replace(",", ".")


def notify_review(rep: dict, log=print) -> None:
    """Sends the review summary: balance, flips, gaps, lineup status."""
    if not _enabled():
        return
    lines = ["📋 <b>Revisión del equipo</b>"]

    ev = rep.get("events", {})
    if ev.get("first_run"):
        lines.append("· Primera revisión (guardando estado de referencia).")
    else:
        if ev.get("removed"):
            lines.append(f"⚠️ Salieron del equipo: {ev['removed']}")
        if ev.get("added"):
            lines.append(f"✅ Nuevos en el equipo: {ev['added']}")
        if ev.get("money_delta"):
            lines.append(f"💰 Cambio de balance: {ev['money_delta']:+,}".replace(",", "."))

    lines.append(f"\n💰 Balance: {_fmt_money(rep.get('money', 0))}")

    lu = rep.get("lineup", {})
    if lu.get("formation"):
        d, m, f = lu["formation"]
        tag = " (conviene cambiar)" if lu.get("changed") else " (ya es la óptima)"
        lines.append(f"⚽ Alineación óptima: {d}-{m}-{f}{tag}")

    flips = rep.get("flips") or []
    if flips:
        lines.append("\n📈 <b>Oportunidades de flip:</b>")
        for o in flips[:5]:
            lines.append(f"  • {o['nombre']}: {o['via']} {_fmt_money(o['buy_price'])} "
                        f"→ +{_fmt_money(o['margin'])} ({o['margin_pct']}%)")

    gaps = rep.get("gaps")
    if gaps:
        lines.append(f"\n🕳️ Huecos en la plantilla: {gaps}")

    tasks = rep.get("tasks") or []
    if tasks:
        lines.append(f"\n📌 Tareas pendientes: {len(tasks)}")

    send("\n".join(lines), log=log)


def notify_execute(result: dict, dry_run: bool, log=print) -> None:
    """Sends what the agent actually did (or would do, in dry-run)."""
    if not _enabled():
        return
    verbo = "PLAN (dry-run)" if dry_run else "EJECUTADO"
    lines = [f"🤖 <b>Acciones del agente [{verbo}]</b>"]

    lu = result.get("lineup", {})
    if lu.get("changed"):
        d, m, f = lu["formation"]
        estado = "✓ aplicada" if lu.get("applied") else "(se aplicaría)"
        lines.append(f"⚽ Alineación → {d}-{m}-{f} {estado}")
        if lu.get("error"):
            lines.append(f"  ⚠️ Error: {lu['error']}")
    else:
        lines.append("⚽ Alineación: ya era la óptima.")

    bd = result.get("bids", {})
    placed = bd.get("placed") or []
    if placed:
        lines.append("\n💸 Pujas:")
        for b in placed:
            estado = "✓" if bd.get("applied") else "(plan)"
            lines.append(f"  • {_fmt_money(b['amount'])} por {b['nombre']} "
                        f"({b['margin_pct']}%) {estado}")
    else:
        lines.append("\n💸 Pujas: ninguna oportunidad rentable ahora mismo.")

    if bd.get("cancelled"):
        lines.append(f"\n🚫 Pujas canceladas (ya no rentables): {bd['cancelled']}")

    errors = bd.get("errors") or []
    if errors:
        lines.append("\n⚠️ <b>Errores al pujar:</b>")
        for e in errors:
            lines.append(f"  • {e['nombre']}: {e['error']}")

    already = bd.get("already_bidding") or []
    if already:
        lines.append(f"\nℹ️ Ya había puja en curso (de una ejecución anterior): {', '.join(already)}")

    sl = result.get("sells", {})
    listed = sl.get("listed") or []
    if listed:
        lines.append("\n🏷️ <b>Puestos en venta:</b>")
        for c in listed:
            estado = "✓" if sl.get("applied") else "(plan)"
            lines.append(f"  • {c['nombre']} por {_fmt_money(c['sale_price'])} "
                        f"({c['reason']}) {estado}")

    sell_errors = sl.get("errors") or []
    if sell_errors:
        lines.append("\n⚠️ <b>Errores al vender:</b>")
        for e in sell_errors:
            lines.append(f"  • {e['nombre']}: {e['error']}")

    send("\n".join(lines), log=log)


def notify_error(context: str, error: Exception, log=print) -> None:
    """Sends a failure alert. Used from the top-level error handler so a
    crashed run is never silent."""
    if not _enabled():
        return
    text = f"🔴 <b>Error en fantasybot</b>\n{context}\n\n<code>{error}</code>"
    send(text, log=log)
