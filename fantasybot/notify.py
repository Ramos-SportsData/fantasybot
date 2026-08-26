"""Sends the agent's review() report to Telegram.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the environment. If either is
missing (e.g. running locally without them configured), send_report() just
prints a notice and returns False -- it never raises, so a missing/misconfigured
Telegram setup must never crash `agent --execute`.

Uses only the standard library (urllib) so it doesn't add a new dependency.
"""

import json
import os
import urllib.parse
import urllib.request

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3800  # Telegram's hard cap is 4096 chars; leave margin for safety


def _send(token, chat_id, text):
    url = TELEGRAM_API.format(token=token)
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                print(f"[telegram] API returned an error: {body}")
                return False
            return True
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        print(f"[telegram] HTTP {e.code} sending message: {err_body}")
        return False
    except Exception as e:
        print(f"[telegram] send failed: {e}")
        return False


def _chunks(text, size=MAX_LEN):
    """Splits text into Telegram-sized chunks without cutting a line in half."""
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > size:
            if chunk:
                yield chunk
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        yield chunk


def _fmt_report(rep, league_name=None, action_summary=None):
    lines = [f"⚽ <b>{league_name or 'FantasyBot Review'}</b>"]

    ev = rep.get("events", {}) or {}
    if ev.get("first_run"):
        lines.append("• First review (saving reference state).")
    else:
        if ev.get("removed"):
            lines.append(f"⚠️ Players left the squad: {ev['removed']}")
        if ev.get("added"):
            lines.append(f"➕ New in squad: {ev['added']}")
        if ev.get("money_delta"):
            lines.append(f"💰 Balance change: {ev['money_delta']:+,}")

    lines.append(f"\n💰 Balance: {rep.get('money', 0):,}")

    md = rep.get("matchday") or {}
    if md.get("kickoff"):
        dias = md.get("days")
        extra = f" ({dias:.1f} days left)" if dias is not None else ""
        lines.append(f"🗓 Next matchday: {md['kickoff']}{extra}")

    lu = rep.get("lineup") or {}
    if lu.get("formation"):
        d, m, f = lu["formation"]
        tag = " (WORTH CHANGING)" if lu.get("changed") else " (already optimal)"
        lines.append(f"⚽ Lineup: {d}-{m}-{f}{tag}")
    else:
        lines.append(f"⚽ Lineup: can't build one — {lu.get('note', 'incomplete squad')}")
    for w in lu.get("watch", []):
        lines.append(f"  ⚠ {w['nombre']} outside likely XI — watch (sell?)")

    if rep.get("flips"):
        lines.append("\n📈 <b>Flip opportunities:</b>")
        for o in rep["flips"][:5]:
            lines.append(f"  {o['nombre']}: buy {o['buy_price']:,} → "
                        f"+{o['margin']:,} ({o['margin_pct']}%)")

    if rep.get("sells"):
        lines.append("\n💸 <b>Recommended sales:</b>")
        for s in rep["sells"][:5]:
            lines.append(f"  {s['nombre']}: ~{s['sale_price']:,} — {s['reason']}")

    if rep.get("gaps"):
        lines.append(f"\n🕳 Squad gaps: {rep['gaps']}")

    tasks = rep.get("tasks") or []
    if tasks:
        lines.append("\n📋 <b>Pending tasks:</b>")
        for t in tasks[:10]:
            due = f" (before {t['due']})" if t.get("due") else ""
            lines.append(f"  #{t['id']} {t['text']}{due}")
        if len(tasks) > 10:
            lines.append(f"  … and {len(tasks) - 10} more")

    if action_summary:
        lines.append("\n🤖 <b>Autonomous actions:</b>")
        lines.extend(action_summary)

    return "\n".join(lines)


def send_report(rep, league_name=None, action_summary=None):
    """Formats `rep` (the dict returned by agent.review()) and sends it to Telegram.

    `action_summary`: optional list of short strings describing what the
    autonomous execution step did (lineup applied, bids placed...), appended
    at the end of the message.

    Returns True on success, False if not configured or if the send failed.
    Never raises -- a notification hiccup must never crash `agent --execute`.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — skipping notification.")
        return False

    text = _fmt_report(rep, league_name=league_name, action_summary=action_summary)
    ok = True
    for chunk in _chunks(text):
        ok = _send(token, chat_id, chunk) and ok
    if ok:
        print("[telegram] Report sent.")
    return ok
