"""fantasybot command-line interface.

    python -m fantasybot login                 # step 1: generate the login URL
    python -m fantasybot login "authredirect://...?code=..."   # step 2: redeem
    python -m fantasybot refresh               # renew the token
    python -m fantasybot me | leagues | team | market | lineup
    python -m fantasybot trends                # who's rising/falling in value
    python -m fantasybot onces real-madrid     # a team's likely starting XI
    python -m fantasybot flip [--horizon N]    # resale opportunities
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone

from . import auth
from .api import FantasyClient, FantasyError
from .sources import lineups as ff_lineups
from .sources.market_trends import market_trends
from .sources import value_history
from .strategy import flip
from .strategy import lineup as lineup_opt
from .strategy import needs as needs_mod
from . import agent as agent_mod
from . import execute as execute_mod
from . import events
from . import notify as notify_mod
from . import state


def _print_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_login(args):
    if args.redirect:
        tokens = auth.finish_login(args.redirect)
        print("[OK] Tokens saved.")
        print(f"  refresh_token: {'yes' if tokens.get('refresh_token') else 'NO'}")
    else:
        url = auth.start_login()
        print("Open this URL, log in with Google, and capture the 'authredirect://' URL")
        print("from the '(canceled)' request in DevTools > Network (Copy link address).\n")
        print(url)
        print("\nThen: python -m fantasybot login \"authredirect://...?code=...\"")


def cmd_refresh(args):
    auth.refresh(auth.load_tokens())
    print("[OK] Token refreshed.")


def cmd_me(args):
    _print_json(FantasyClient().me())


def cmd_leagues(args):
    _print_json(FantasyClient().leagues())


def cmd_team(args):
    fc = FantasyClient()
    lid, tid = fc.default_ids()
    _print_json(fc.team(lid, tid))


def cmd_lineup(args):
    fc = FantasyClient()
    _, tid = fc.default_ids()
    _print_json(fc.lineup(tid))


def cmd_market(args):
    fc = FantasyClient()
    lid, _ = fc.default_ids()
    _print_json(fc.market(lid))


def cmd_trends(args):
    players = [p for p in market_trends() if p.get("tendencia") is not None]
    up = sorted(players, key=lambda p: -p["tendencia"])[:10]
    down = sorted(players, key=lambda p: p["tendencia"])[:10]
    print("RISING:")
    for p in up:
        print(f"  {p['nombre']:<24} value={p['valor']:>12,}  trend={p['tendencia']}")
    print("FALLING:")
    for p in down:
        print(f"  {p['nombre']:<24} value={p['valor']:>12,}  trend={p['tendencia']}")


def cmd_value_snapshot(args):
    """Print today's OFFICIAL market-value snapshot as JSON: {playerMasterId: {v, s}} from
    LaLiga's own all_players() — the same data agent.review() banks daily under
    .state/value_history/ so `flip` can eventually cross-check against futbolfantasy."""
    fc = FantasyClient()
    try:
        players = fc.all_players()
    except Exception as e:  # a data-source hiccup must never crash the snapshot
        print(f"Could not fetch players: {e}")
        return
    snap = value_history.snapshot_from_players(players)
    state.save_value_snapshot(date.today().isoformat(), snap)
    print(json.dumps(snap, ensure_ascii=False))


def cmd_onces(args):
    players = sorted(ff_lineups.team_lineup(args.team), key=lambda p: -(p["prob"] or 0))
    print(f"Likely starting XI {args.team} ({len(players)} players):")
    for p in players:
        estado = " ".join(s for s, on in [("INJURED", p["lesionado"]),
                           ("SUSPENDED", p["sancionado"]),
                           ("UNAVAILABLE", not p["disponible"])] if on)
        print(f"  {(p['prob'] or 0):>3}%  {p['nombre']:<26} {estado}")


def cmd_flip(args):
    fc = FantasyClient()
    lid, tid = fc.default_ids()
    team = fc.team(lid, tid)
    counts = {}
    for p in team["players"]:
        pos = flip.POS.get(p["playerMaster"]["positionId"], "?")
        counts[pos] = counts.get(pos, 0) + 1
    ops = flip.opportunities(fc, lid, args.horizon)
    if args.json:
        return _print_json({"money": team["teamMoney"], "squad": counts,
                            "horizon": args.horizon, "opportunities": ops})
    print(f"Balance: {team['teamMoney']:,} | Squad: {counts} | "
          f"Horizon: {args.horizon}d\n")
    print(f"{'PLAYER':<20}{'POS':<5}{'VIA':<9}{'BUY':>12}{'PROJ':>12}"
          f"{'MARGIN':>12}{'%':>7}  TREND")
    print("-" * 92)
    for r in ops:
        flag = " <=" if r["margin_pct"] >= 5 and r["buy_price"] <= team["teamMoney"] else ""
        print(f"{r['nombre']:<20}{r['pos']:<5}{r['via']:<9}{r['buy_price']:>12,}"
              f"{r['proyeccion']:>12,}{r['margin']:>12,}{r['margin_pct']:>6}%  "
              f"{r['tendencia']}{flag}")


def cmd_optimize(args):
    fc = FantasyClient()
    lid, tid = fc.default_ids()
    team = fc.team(lid, tid)
    premium = agent_mod.league_allows_premium_formations(fc, lid)
    fixture_difficulty = agent_mod.captain_fixture_difficulty(fc) if premium else None
    try:
        best = lineup_opt.optimize(team, premium=premium, fixture_difficulty=fixture_difficulty)
    except ValueError as e:
        # Incomplete squad (no goalkeeper / fewer than 11): can't field a valid XI.
        print(f"Can't build a lineup: {e}")
        return
    if args.json and not args.apply:
        return _print_json(best)
    d, m, f = best["formation"]
    print(f"Best formation: {d}-{m}-{f}  (score {best['total']})\n")
    for pos in ("goalkeeper", "defender", "midfield", "striker"):
        for e in ([best[pos]] if pos == "goalkeeper" else best[pos]):
            prob = f"{e['prob']}%" if e["prob"] is not None else "?"
            disp = "" if e["disponible"] else "  NOT AVAILABLE"
            print(f"  {pos[:3].upper()}  {e['nombre']:<20} prob={prob:<5} "
                  f"score={e['score']}{disp}")

    # Normalise both sides to str: payload_ids may hold ints, and the left side is
    # str()-wrapped, so an int/str mismatch would mark every player a substitute.
    xi = {str(i) for i in lineup_opt.payload_ids(best)}
    banked = [p["playerMaster"].get("nickname") for p in team["players"]
              if str(p.get("playerTeamId") or p["playerMaster"]["id"]) not in xi]
    print(f"\nSubstitutes (outside the XI): {banked}")

    if args.apply:
        current_ids, cur_coach, cur_captain = agent_mod._current_lineup(fc, tid)
        execute_mod.apply_lineup(fc, tid, best, current_ids, dry_run=False,
                                 current_coach=cur_coach, current_captain=cur_captain)
        print("\n[OK] Lineup applied.")
    else:
        print("\n(Proposal only. Add --apply to save it to your team.)")


def cmd_needs(args):
    fc = FantasyClient()
    lid, tid = fc.default_ids()
    team = fc.team(lid, tid)
    rep = needs_mod.advise(fc, lid, team, days_to_matchday=args.days)
    if args.json:
        return _print_json(rep)
    if not rep["gaps"]:
        print("Balanced squad: no gaps below the minimum.")
        return
    print(f"Gaps: {rep['gaps']}  |  urgency factor: {rep['urgency_multiplier']}\n")
    for pos, cands in rep["suggestions"].items():
        print(f"== {pos}: candidates on the market ==")
        for c in cands[:6]:
            prob = f"{c['prob']}%" if c["prob"] is not None else "?"
            aff = "" if c["affordable"] else "  (can't afford)"
            print(f"  {c['nombre']:<20} {c['via']:<9} price={c['price']:>12,} "
                  f"maxBid={c['max_bid']:>12,} prob={prob}{aff}")
        print()


def cmd_agent(args):
    fc = FantasyClient()
    rep = agent_mod.review(fc, days_to_matchday=args.days)

    ev = rep["events"]
    cambios = "first review" if ev.get("first_run") else (
        ", ".join(filter(None, [
            f"out {ev['removed']}" if ev.get("removed") else "",
            f"in {ev['added']}" if ev.get("added") else "",
            f"balance {ev['money_delta']:+,}" if ev.get("money_delta") else "",
        ])) or "no changes")
    events.emit("review", f"Review: {cambios}",
                detail={"balance": f"{rep['money']:,}",
                        "flips": len(rep.get("flips", [])),
                        "lineup": "change" if rep["lineup"]["changed"] else "already optimal"})

    if args.json:
        return _print_json(rep)

    print("=" * 60)
    print("AGENT REVIEW")
    print("=" * 60)

    ev = rep["events"]
    if ev.get("first_run"):
        print("· First review (saving reference state).")
    else:
        if ev["removed"]:
            print(f"· ⚠ Players left your squad: {ev['removed']} "
                  f"(sale or buyout clause against you?)")
        if ev["added"]:
            print(f"· New in your squad: {ev['added']}")
        if ev["money_delta"]:
            print(f"· Balance change: {ev['money_delta']:+,}")
        if not (ev["removed"] or ev["added"] or ev["money_delta"]):
            print("· No changes since the last review.")

    print(f"\nBalance: {rep['money']:,}")
    md = rep.get("matchday") or {}
    if md.get("kickoff"):
        dias = md.get("days")
        print(f"Next matchday: {md['kickoff']}"
              + (f"  ({dias:.1f} days left)" if dias is not None else ""))
    lu = rep["lineup"]
    if lu["formation"]:
        d, m, f = lu["formation"]
        tag = "  (WORTH CHANGING)" if lu["changed"] else "  (already the best)"
        print(f"Optimal lineup: {d}-{m}-{f}{tag}")
    else:
        print(f"Optimal lineup: can't build one — {lu.get('note', 'incomplete squad')}")
    for w in lu.get("watch", []):
        print(f"  ⚠ {w['nombre']} ({w['valor']:,}) outside the likely XI "
              f"— transfer/injury? watch (sell?)")

    if rep["flips"]:
        print("\nFlip opportunities (buy to resell):")
        for o in rep["flips"]:
            print(f"  {o['nombre']:<18} {o['via']:<8} buy {o['buy_price']:>11,} "
                  f"→ +{o['margin']:,} ({o['margin_pct']}%)")

    if rep.get("sells"):
        print("\nRecommended sales:")
        for s in rep["sells"]:
            print(f"  {s['nombre']:<18} {s['pos']}  ~{s['sale_price']:,}  — {s['reason']}")

    if rep["gaps"]:
        print(f"\nSquad gaps: {rep['gaps']}")

    if rep["clause_targets"]:
        print("\nBuyout clause targets:")
        for t in rep["clause_targets"]:
            print(f"  {t['nombre']:<18} {t['pos']}  clause {t['clause']:,}  "
                  f"opens {t['unlock']}")

    if rep["reminders"]:
        print("\nScheduled reminders:")
        for r in rep["reminders"]:
            print(f"  [{r['fire_at']}] {r['message']}")

    if rep["tasks"]:
        print("\nPending tasks:")
        for t in rep["tasks"]:
            due = f" (before {t['due']})" if t.get("due") else ""
            print(f"  #{t['id']} {t['text']}{due}")

    # --- autonomous execution (line up + bid; buyout clauses NOT) ---
    lid, tid = fc.default_ids()
    team = fc.team(lid, tid)
    premium = agent_mod.league_allows_premium_formations(fc, lid)
    fixture_difficulty = agent_mod.captain_fixture_difficulty(fc) if premium else None
    try:
        best = lineup_opt.optimize(team, premium=premium, fixture_difficulty=fixture_difficulty)
    except ValueError as e:
        # Incomplete squad (e.g. no goalkeeper): can't field a valid XI, so there's
        # nothing safe to auto-apply. Surface it and skip acting, don't crash the run.
        print(f"\n--- AUTONOMOUS ACTIONS [skipped] ---")
        print(f"· Can't act: incomplete squad ({e}). Sign the missing position first.")
        league_name = os.environ.get("FANTASY_LEAGUE_NAME") or os.environ.get("FANTASY_STATE_DIR")
        notify_mod.send_report(
            rep, league_name=league_name,
            action_summary=[f"Skipped: incomplete squad ({e})."])
        return
    current, cur_coach, cur_captain = agent_mod._current_lineup(fc, tid)
    result = execute_mod.act(fc, lid, tid, team, best, current,
                             dry_run=not args.execute,
                             current_coach=cur_coach, current_captain=cur_captain)
    verbo = "EXECUTED" if args.execute else "PLAN (use --execute to act)"
    print(f"\n--- AUTONOMOUS ACTIONS [{verbo}] ---")
    lu = result["lineup"]
    action_lines = []
    if lu["changed"]:
        d, m, f = lu["formation"]
        applied = lu.get("applied")
        print(f"· Lineup → {d}-{m}-{f}"
              + ("  ✓ applied" if applied else "  (would apply)"))
        action_lines.append(f"Lineup → {d}-{m}-{f}"
                            + (" ✓ applied" if applied else " (plan only)"))
    else:
        print("· Lineup: already optimal, nothing to do.")
    bd = result["bids"]
    for b in bd["placed"]:
        applied = bd.get("applied")
        print(f"· Bid {b['amount']:,} for {b['nombre']} ({b['margin_pct']}%)"
              + ("  ✓" if applied else "  (would bid)"))
        action_lines.append(f"Bid {b['amount']:,} for {b['nombre']}"
                            + (" ✓" if applied else " (plan only)"))
    if not bd["placed"]:
        print("· Bids: no profitable flip opportunity right now.")
    if bd["cancelled"]:
        print(f"· Bids cancelled (no longer profitable): {bd['cancelled']}")
        action_lines.append(f"Bids cancelled: {bd['cancelled']}")

    # --- Telegram notification ---
    league_name = os.environ.get("FANTASY_LEAGUE_NAME") or os.environ.get("FANTASY_STATE_DIR")
    notify_mod.send_report(rep, league_name=league_name, action_summary=action_lines)


def cmd_sell(args):
    fc = FantasyClient()
    lid, _ = fc.default_ids()
    resp = fc.sell_player(lid, args.player_id, args.price)
    events.emit("sell", f"Sale: {args.player_id} at {args.price:,}")
    print(f"[OK] {args.player_id} listed for sale at {args.price:,}.")
    _print_json(resp)


def cmd_bid(args):
    fc = FantasyClient()
    lid, _ = fc.default_ids()
    resp = fc.make_bid(lid, args.market_id, args.amount)
    events.emit("bid", f"Bid {args.amount:,} on {args.market_id}")
    _print_json(resp)


def cmd_cancel_bid(args):
    fc = FantasyClient()
    lid, _ = fc.default_ids()
    resp = fc.cancel_bid(lid, args.market_id, args.bid_id)
    events.emit("cancel", f"Bid cancelled: {args.market_id}")
    _print_json(resp)


def cmd_clause(args):
    fc = FantasyClient()
    lid, _ = fc.default_ids()
    resp = fc.pay_buyout_clause(lid, args.player_id, args.amount)
    events.emit("clause", f"Buyout clause: {args.player_id} for {args.amount:,}")
    _print_json(resp)


def cmd_bid_now(args):
    from . import bidding
    lid, _ = FantasyClient().default_ids()
    bidding.last_minute_bid(lid, args.market_id, args.max, dry_run=args.dry_run)


def cmd_bid_plan(args):
    if args.clear:
        state.clear_bid_plan()
        print("Bid plan cleared.")
        return
    if args.market_id and args.max is not None:
        state.add_bid_target(args.market_id, args.max)
        events.emit("bid-plan", f"Last-minute bid scheduled: {args.market_id}",
                    detail={"max": f"{args.max:,}"}, status="plan")
    _print_json(state.load_bid_plan())


def cmd_bid_run(args):
    from . import bidding
    lid, _ = FantasyClient().default_ids()
    bidding.run_bid_plan(lid, dry_run=args.dry_run)


def cmd_due(args):
    """Fire due reminders. Meant for a scheduler (every minute)."""
    now = datetime.now(timezone.utc)
    due = state.due_reminders(now)
    if not due:
        return  # nothing to fire; silent for cron
    for r in due:
        print(f"🔔 {r['message']}  (event: {r['event_at']})")
        state.mark_reminder_fired(r["key"])


def cmd_watch(args):
    """Open the monitoring UI ('mission control') and, optionally, trigger a run.

    - `--run`      triggers the deterministic agent (fantasybot agent --execute).
    - `--hermes`   triggers the Hermes brain with the skill (LLM), if installed.
    Without flags, it just monitors: you'll see the next cron cycle live.
    """
    import subprocess
    import threading
    import webbrowser
    from . import monitor

    srv, url = monitor.serve(args.host, args.port, background=True,
                             run_mode="hermes" if args.hermes else "agent")
    print(f"Mission Control at {url}")
    if args.host in ("127.0.0.1", "localhost"):
        print(f"  (remote VPS: tunnel  ssh -L {args.port}:127.0.0.1:{args.port} <server>)")

    def fire():
        time.sleep(1.2)  # let the UI connect the stream before starting
        if args.hermes:
            subprocess.run(["hermes", "-z", monitor.HERMES_PROMPT,
                            "--skill", "fantasy-manager"])
        elif args.run:
            subprocess.run([sys.executable, "-m", "fantasybot", "agent", "--execute"])

    if args.run or args.hermes:
        threading.Thread(target=fire, daemon=True).start()
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    print("Ctrl+C to exit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()
        print("\nSee you later.")


def cmd_tasks(args):
    if args.done is not None:
        state.complete_task(args.done)
        print(f"Task #{args.done} marked as done.")
        return
    pend = state.pending_tasks()
    if not pend:
        print("No pending tasks.")
    for t in pend:
        due = f" (before {t['due']})" if t.get("due") else ""
        print(f"  #{t['id']} {t['text']}{due}")


def cmd_rivals(args):
    """Analyze all rivals in the active league: estimated cash, market activity, clauses."""
    from .strategy import rivals as rivals_mod
    fc = FantasyClient()
    lid, _ = fc.default_ids()
    rivals = rivals_mod.analyze_rivals(fc, lid, initial_budget=args.initial_budget)

    if args.json:
        _print_json(rivals)
        return

    if not rivals:
        print("No rival data yet.")
        return

    # Check if querying a specific manager
    if args.manager:
        q = args.manager.strip().lower()
        matched = None
        for r in rivals:
            if (q in (r.get("manager_name") or "").lower()
                    or q in (str(r.get("manager_id")), str(r.get("team_id")),
                             f"#{r.get('position')}", str(r.get("position")))
                    or (q in ("me", "yo") and r.get("is_me"))):
                matched = r
                break

        if not matched:
            print(f"No rival found matching '{args.manager}'.")
            return

        r = matched
        is_me = " (YOU)" if r.get("is_me") else ""
        tp = r.get("top_protected") or {}
        print(f"\n--- RIVAL DETAILS: {r.get('manager_name')}{is_me} ---")
        print(f"Rank: #{r.get('position')} | Points: {r.get('points')} | Squad: {r.get('players_count')} players")
        line = (f"Team Value: {r.get('team_value', 0):,} € | "
                f"Estimated Cash: ~{r.get('estimated_balance', 0):,} €")
        if r.get("known_balance") is not None:
            line += f" | Real Cash: {r.get('known_balance'):,} €"
        print(line)
        top = f"{tp.get('name')} (+{tp.get('invested', 0):,} € invested)" if tp.get("name") else "None"
        print(f"Total Clause Value: {r.get('total_clause', 0):,} € | Top Protected: {top}")
        print(f"Purchases: {r.get('purchases', 0):,} € | Sales: {r.get('sales', 0):,} € | "
              f"Net P&L: {r.get('net_profit', 0):+,} €\n")

        players = r.get("players") or []
        if players:
            print("Squad Players (by market value):")
            print(f"{'PLAYER':<20} {'POS':<6} {'BOUGHT AT':>14} {'CUR. VALUE':>13} "
                  f"{'PROFIT / LOSS':>18} {'CLAUSE':>13} {'PROTECT':>12}")
            print("-" * 100)
            for p in players:
                b_str = f"{p['bought_price']:,} €" if p.get("bought_price") else "(Initial)"
                diff_str = (f"{p.get('diff', 0):+,} € ({p.get('diff_pct', 0):+.1f}%)"
                            if p.get("bought_price") else "-")
                prot = p.get("protection", 0) or 0
                prot_str = f"+{prot:,} €" if prot > 0 else "-"
                print(f"{(p.get('name') or '')[:19]:<20} {p.get('pos', '?'):<6} {b_str:>14} "
                      f"{p.get('market_value', 0):>11,} € {diff_str:>18} "
                      f"{p.get('buyout_clause', 0):>11,} € {prot_str:>12}")
        print()
        return

    events = rivals[0].get("tracked_events_count", 0)
    print("\n--- LEAGUE RIVALS: FINANCIAL & CLAUSE AUDIT ---")
    print(f"Tracked {events} transfer events from {rivals[0].get('tracked_from_date')} "
          f"to {rivals[0].get('tracked_to_date')}.\n")
    print(f"{'#':<3} {'MANAGER':<20} {'TEAM VALUE':>14} {'EST. CASH':>14} {'PURCHASES':>13} "
          f"{'SALES':>13} {'NET P&L':>14} {'TOP PROTECTED':<22}")
    print("-" * 122)
    for r in rivals:
        is_me = " (YOU)" if r.get("is_me") else ""
        m_name = ((r.get("manager_name") or "Unknown") + is_me)[:19]
        top_prot = ((r.get("top_protected") or {}).get("name") or "-")[:21]
        print(f"{r.get('position', 0):<3} {m_name:<20} {r.get('team_value', 0):>12,} € "
              f"~{r.get('estimated_balance', 0):>12,} € {r.get('purchases', 0):>11,} € "
              f"{r.get('sales', 0):>11,} € {r.get('net_profit', 0):>+12,} € {top_prot:<22}")

    mine = next((r for r in rivals if r.get("is_me")), None)
    if mine and mine.get("known_balance") is not None:
        diff = mine["known_balance"] - mine.get("estimated_balance", 0)
        print(f"\n* REALITY CHECK (your account): Real {mine['known_balance']:,} € vs "
              f"Estimated ~{mine.get('estimated_balance', 0):,} € "
              f"(diff {diff:+,} € — daily-ad bonus / unknowns)\n")


def cmd_history(args):
    """Analyze speculation trading and flip ROI history."""
    from .strategy import history as history_mod
    fc = FantasyClient()
    lid, _ = fc.default_ids()
    rep = history_mod.analyze_league_trading_history(fc, lid)

    if args.json:
        _print_json(rep)
        return

    # Check if querying specific manager
    if args.manager:
        query = args.manager.strip()
        matched = None
        for m in rep["managers"]:
            if query.lower() in (m.get("manager_name") or "").lower():
                matched = m
                break
            if query.lower() in (m.get("team_name") or "").lower():
                matched = m
                break
            if query in (str(m.get("manager_id")), str(m.get("team_id")), f"#{m.get('position')}", str(m.get('position'))):
                matched = m
                break
            if query.lower() in ("me", "yo") and m.get("is_me"):
                matched = m
                break

        if not matched:
            print(f"No manager found matching '{args.manager}'.")
            return

        m = matched
        is_me = " (YOU)" if m.get("is_me") else ""
        print(f"\n--- TRADING & FLIP HISTORY: {m.get('manager_name')}{is_me} ---")
        print(f"Rank: #{m.get('position')} | Points: {m.get('points')} | Total P&L: {m.get('total_pnl'):+,} €")
        print(f"Realized Flips Profit: {m.get('realized_profit'):+,} € ({m.get('total_trades', 0)} flips, {m.get('win_rate_pct'):.1f}% Win Rate, Avg ROI: {m.get('avg_roi_pct'):+.1f}%)")
        initial_rev = sum(s.get("sell_price", 0) for s in m.get("initial_sales", []))
        print(f"Unrealized Profit in Squad: {m.get('unrealized_profit'):+,} € | Initial Squad Sales: {initial_rev:,} €\n")

        print("Closed Flips (Realized Profit):")
        if m.get("completed_flips"):
            print(f"{'PLAYER':<18} {'POS':<10} {'BOUGHT':>12} {'SOLD':>12} {'PROFIT':>14} {'ROI %':>8} {'DAYS':>6}")
            print("-" * 85)
            for f in m.get("completed_flips", []):
                print(f"{f['name'][:17]:<18} {f['pos']:<10} {f['buy_price']:>10,} € {f['sell_price']:>10,} € {f['profit']:>+12,} € {f['roi_pct']:>+7.1f}% {f['holding_days']:>6}")
        else:
            print("  (No closed flips yet)")

        print("\nOpen Purchased Holdings (Unrealized Profit):")
        if m.get("open_holdings"):
            print(f"{'PLAYER':<18} {'POS':<10} {'BOUGHT':>12} {'CURRENT':>12} {'UNREALIZED':>14} {'ROI %':>8}")
            print("-" * 78)
            for o in m.get("open_holdings", []):
                print(f"{o['name'][:17]:<18} {o['pos']:<10} {o['buy_price']:>10,} € {o['market_value']:>10,} € {o['unrealized_profit']:>+12,} € {o['roi_pct']:>+7.1f}%")
        else:
            print("  (No open purchased holdings)")
        print()
        return

    print(f"\n--- LEAGUE SPECULATION & FLIP PROFITABILITY LEADERBOARD ---")
    print(f"Tracked {rep.get('tracked_events', 0)} transfer events from {rep.get('tracked_from')} to {rep.get('tracked_to')}.\n")
    print(f"{'#':<3} {'MANAGER':<20} {'TOTAL P&L':>14} {'REALIZED':>13} {'UNREALIZED':>13} {'FLIPS':>7} {'WIN %':>8} {'AVG ROI':>9}")
    print("-" * 95)
    for m in rep["managers"]:
        is_me = " (YOU)" if m.get("is_me") else ""
        m_name = (m.get("manager_name") or "Unknown") + is_me
        print(f"{m.get('position', 0):<3} {m_name[:19]:<20} {m.get('total_pnl', 0):>+12,} € {m.get('realized_profit', 0):>+11,} € {m.get('unrealized_profit', 0):>+11,} € {m.get('total_trades', 0):>7} {m.get('win_rate_pct', 0):>7.1f}% {m.get('avg_roi_pct', 0):>+8.1f}%")
    print()


def cmd_scout(args):
    """Scout a player or your entire squad."""
    from .api import FantasyClient
    from .strategy import scouting as scouting_mod
    c = FantasyClient()

    if getattr(args, "team", False) or not args.player:
        lid, tid = c.default_ids()
        team_data = c.team(lid, tid)
        ts = scouting_mod.analyze_team_squad(team_data)
        print(f"\n--- SQUAD SCOUTING AUDIT: {ts['team_name']} ---")
        print(f"Squad: {ts['total_players']} players | Value: {ts['total_val']:,} € | Money: {ts['team_money']:,} €")
        print(f"Past Season Total: {ts['total_last_pts']} pts (Avg: {ts['avg_last_pts']} pts/player)\n")

        headers = {1: "GOALKEEPERS", 2: "DEFENDERS", 3: "MIDFIELDERS", 4: "FORWARDS"}
        for pid in (1, 2, 3, 4):
            plist = ts["by_pos"].get(pid, [])
            if plist:
                print(f"[{headers[pid]}]")
                for r in plist:
                    last_p = r['last_season_points']
                    p_badge = f"{last_p} pts" if last_p > 0 else "New"
                    prob_s = f"{r['starting_prob']}%" if r['starting_prob'] is not None else "?"
                    print(f"  • {r['name']}: {p_badge} last yr | XI prob: {prob_s} | {r['physical_status']} | {r['verdict']}")
                print()
        return

    all_p = c.all_players() or []
    pm = scouting_mod.search_player_in_list(args.player, all_p)
    if not pm:
        print(f"No player found matching '{args.player}'.")
        return
    s = scouting_mod.analyze_player_profile(pm)
    print(f"\n--- SCOUTING REPORT: {s['name']} ({s['pos']}) ---")
    print(f"Team: {s['team']} | Value: {s['market_value']:,} €")
    print(f"Past Season: {s['last_season_points']} pts (~{s['last_season_avg']} pts/gw) | {s['tier_badge']}")
    print(f"Current: {s['current_points']} pts ({s['current_avg']:.1f} pts/gw) | {s['evolution']}")
    print(f"Lineup Role: {s['starter_status']} ({s['role_shift']})")
    print(f"Fitness & Availability: {s['physical_status']}")
    print(f"Verdict: {s['verdict']}\n")


def build_parser():
    p = argparse.ArgumentParser(prog="fantasybot", description="LALIGA Fantasy agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("login", help="OAuth login (2 steps)")
    lp.add_argument("redirect", nargs="?", help="authredirect:// URL from step 2")
    lp.set_defaults(func=cmd_login)

    for name, func in [("refresh", cmd_refresh), ("me", cmd_me),
                       ("leagues", cmd_leagues), ("team", cmd_team),
                       ("lineup", cmd_lineup), ("market", cmd_market),
                       ("trends", cmd_trends)]:
        sub.add_parser(name, help=name).set_defaults(func=func)

    vs = sub.add_parser("value-snapshot",
                        help="print today's official market-value snapshot (JSON)")
    vs.set_defaults(func=cmd_value_snapshot)

    op = sub.add_parser("onces", help="a team's likely starting XI")
    op.add_argument("team", help="team slug, e.g. real-madrid")
    op.set_defaults(func=cmd_onces)

    fp = sub.add_parser("flip", help="resale opportunities")
    fp.add_argument("--horizon", type=int, default=flip.DEFAULT_HORIZON)
    fp.add_argument("--json", action="store_true", help="JSON output")
    fp.set_defaults(func=cmd_flip)

    opt = sub.add_parser("optimize", help="best lineup (--apply to save)")
    opt.add_argument("--apply", action="store_true", help="save the lineup")
    opt.add_argument("--json", action="store_true", help="JSON output (incl. payload)")
    opt.set_defaults(func=cmd_optimize)

    nd = sub.add_parser("needs", help="squad gaps and suggested signings")
    nd.add_argument("--days", type=int, default=None,
                    help="days until the matchday (for urgency)")
    nd.add_argument("--json", action="store_true", help="JSON output")
    nd.set_defaults(func=cmd_needs)

    ag = sub.add_parser("agent", help="full agent review (like a human)")
    ag.add_argument("--days", type=int, default=None,
                    help="days until the matchday (for urgency)")
    ag.add_argument("--execute", action="store_true",
                    help="actually ACT (line up and bid); without it, only plans")
    ag.add_argument("--json", action="store_true",
                    help="JSON output of the report (for Hermes)")
    ag.set_defaults(func=cmd_agent)

    rv = sub.add_parser("rivals", help="analyze rival balances, clauses and acquisitions")
    rv.add_argument("manager", nargs="?", default=None, help="manager name, team name, rank (#1), or 'me'")
    rv.add_argument("--initial-budget", type=int, default=None, help="override initial starting budget (default: 50,000,000)")
    rv.add_argument("--json", action="store_true", help="JSON output")
    rv.set_defaults(func=cmd_rivals)

    hi = sub.add_parser("history", help="analyze speculation trading and flip ROI history")
    hi.add_argument("manager", nargs="?", default=None, help="manager name, team name, rank (#1), or 'me'")
    hi.add_argument("--json", action="store_true", help="JSON output")
    hi.set_defaults(func=cmd_history)

    sc = sub.add_parser("scout", help="multi-season scouting report for a player or squad")
    sc.add_argument("player", nargs="?", default=None, help="player name, nickname or ID")
    sc.add_argument("--team", action="store_true", help="scout your entire squad")
    sc.set_defaults(func=cmd_scout)

    wt = sub.add_parser("watch", help="live monitoring UI (mission control)")
    wt.add_argument("--run", action="store_true",
                    help="trigger the deterministic agent (agent --execute)")
    wt.add_argument("--hermes", action="store_true",
                    help="trigger the Hermes brain with the skill (LLM)")
    wt.add_argument("--port", type=int, default=9137)
    wt.add_argument("--host", default="127.0.0.1")
    wt.add_argument("--no-open", action="store_true", help="don't open the browser")
    wt.set_defaults(func=cmd_watch)

    tk = sub.add_parser("tasks", help="pending task list")
    tk.add_argument("--done", type=int, default=None, metavar="ID",
                    help="mark a task as done")
    tk.set_defaults(func=cmd_tasks)

    sub.add_parser("due", help="fire due reminders (for cron)").set_defaults(func=cmd_due)

    sl = sub.add_parser("sell", help="list a player for sale (playerId price)")
    sl.add_argument("player_id")
    sl.add_argument("price", type=int)
    sl.set_defaults(func=cmd_sell)

    bd = sub.add_parser("bid", help="bid on a market player (marketId amount)")
    bd.add_argument("market_id")
    bd.add_argument("amount", type=int)
    bd.set_defaults(func=cmd_bid)

    cb = sub.add_parser("cancel-bid", help="cancel a bid (marketId bidId)")
    cb.add_argument("market_id")
    cb.add_argument("bid_id")
    cb.set_defaults(func=cmd_cancel_bid)

    cl = sub.add_parser("clause", help="pay a player's buyout clause (playerId amount)")
    cl.add_argument("player_id")
    cl.add_argument("amount", type=int)
    cl.set_defaults(func=cmd_clause)

    sn = sub.add_parser("bid-now", help="one-off last-minute bid (marketId max)")
    sn.add_argument("market_id")
    sn.add_argument("max", type=int, help="bid cap")
    sn.add_argument("--dry-run", action="store_true")
    sn.set_defaults(func=cmd_bid_now)

    sp = sub.add_parser("bid-plan", help="schedule last-minute bids (marketId cap)")
    sp.add_argument("market_id", nargs="?")
    sp.add_argument("max", type=int, nargs="?", help="bid cap")
    sp.add_argument("--clear", action="store_true")
    sp.set_defaults(func=cmd_bid_plan)

    sr = sub.add_parser("bid-run", help="run the bid plan at close (for cron)")
    sr.add_argument("--dry-run", action="store_true")
    sr.set_defaults(func=cmd_bid_run)

    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (FantasyError, auth.AuthError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
