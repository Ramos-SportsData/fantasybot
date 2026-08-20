"""LALIGA Fantasy API client.

Manages the token (auto-refreshes it before expiry and retries on 401) and
exposes read and write methods. The rest of the project builds on this.
"""

import json
import os
import time
import urllib.error
import urllib.request

from . import auth, config


class FantasyError(Exception):
    pass


class FantasyClient:
    def __init__(self):
        self.tokens = auth.load_tokens()

    # --- token ---
    def _bearer(self) -> str:
        return auth.bearer_token(self.tokens)

    def _is_expiring(self) -> bool:
        exp = auth.jwt_exp(self._bearer())
        if exp is None:
            return False  # if we don't know, the retry on 401 covers us
        return time.time() > (exp - config.TOKEN_EXPIRY_MARGIN)

    def refresh(self):
        self.tokens = auth.refresh(self.tokens)

    # --- requests ---
    def _request(self, method: str, path: str, body=None):
        if self._is_expiring():
            self.refresh()
        return self._do(method, path, body, retry_on_401=True)

    def _do(self, method, path, body, retry_on_401):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._bearer()}",
            "Accept": "application/json",
            "x-lang": "es",
            "User-Agent": config.USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(config.API_BASE + path, data=data,
                                     headers=headers, method=method)
        
        max_retries = 3
        backoff_factor = 2

        for attempt in range(max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None

            except urllib.error.HTTPError as e:
                # Manejo de reintentos para errores temporales del servidor (500, 502, 503, 504)
                if e.code in (500, 502, 503, 504) and attempt < max_retries:
                    sleep_time = backoff_factor ** (attempt + 1)
                    time.sleep(sleep_time)
                    continue

                # Refresh de token si da 401
                if e.code == 401 and retry_on_401:
                    self.refresh()
                    return self._do(method, path, body, retry_on_401=False)

                detail = e.read().decode("utf-8", "replace")[:400]
                raise FantasyError(f"{method} {path} -> {e.code}: {detail}")
            
            except urllib.error.URLError as e:
                if attempt < max_retries:
                    time.sleep(backoff_factor ** (attempt + 1))
                    continue
                raise FantasyError(f"{method} {path} -> Network Error: {e.reason}")

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, body=None):
        return self._request("POST", path, body)

    def put(self, path, body=None):
        return self._request("PUT", path, body)

    def delete(self, path):
        return self._request("DELETE", path)

    # --- path helpers ---
    def _cmp(self, tail):
        return f"/v1/competition/{config.COMPETITION_ID}{tail}"

    # --- reads ---
    def me(self):
        return self.get("/v4/user/me?x-lang=es")

    def leagues(self):
        return self.get(self._cmp("/leagues?x-lang=es"))

    def default_ids(self):
        """(league_id, team_id) of the league to operate on.

        Defaults to the user's first league, so single-league setups (the OSS self-host
        case) work with no config. Set FANTASYBOT_LEAGUE=<id> to pin a specific league —
        that's how the hosted service drives an account that has several leagues: it runs
        the agent once per league, exporting this var each time. Every command resolves
        ids through here, so one env var steers them all without touching call sites.
        """
        leagues = self.leagues()
        if not leagues:
            raise FantasyError("The user has no leagues.")
        want = os.environ.get("FANTASYBOT_LEAGUE")
        if want:
            for lg in leagues:
                if str(lg["id"]) == str(want):
                    return lg["id"], str(lg["team"]["id"])
            raise FantasyError(f"League {want} is not in this account.")
        lg = leagues[0]
        return lg["id"], str(lg["team"]["id"])

    def team(self, league_id, team_id):
        return self.get(self._cmp(f"/leagues/{league_id}/teams/{team_id}?x-lang=es"))

    def lineup(self, team_id):
        return self.get(self._cmp(f"/teams/{team_id}/lineup?x-lang=es"))

    def market(self, league_id):
        return self.get(self._cmp(f"/league/{league_id}/market?x-lang=es"))

    # --- writes: market ---
    def make_bid(self, league_id, market_id, money):
        return self.post(self._cmp(
            f"/league/{league_id}/market/{market_id}/bid?x-lang=es"), {"money": money})

    def modify_bid(self, league_id, market_id, bid_id, money):
        return self.put(self._cmp(
            f"/league/{league_id}/market/{market_id}/bid/{bid_id}?x-lang=es"),
            {"money": money})

    def cancel_bid(self, league_id, market_id, bid_id):
        return self.delete(self._cmp(
            f"/league/{league_id}/market/{market_id}/bid/{bid_id}/cancel?x-lang=es"))

    def sell_player(self, league_id, player_id, sale_price):
        return self.post(self._cmp(f"/league/{league_id}/market/sell?x-lang=es"),
                         {"playerId": player_id, "salePrice": sale_price})

    def accept_offer(self, league_id, market_id, offer_id, money):
        return self.post(self._cmp(
            f"/league/{league_id}/market/{market_id}/offer/{offer_id}/accept?x-lang=es"),
            {"offerMoney": money})

    def decline_offer(self, league_id, market_id, offer_id):
        return self.post(self._cmp(
            f"/league/{league_id}/market/{market_id}/offer/{offer_id}/reject?x-lang=es"))

    # --- writes: buyout clauses ---
    def pay_buyout_clause(self, league_id, player_id, amount):
        """Buyout: pays the release clause of another manager's player."""
        return self.post(self._cmp(
            f"/league/{league_id}/buyout/{player_id}/pay?x-lang=es"),
            {"buyoutClauseToPay": amount})

    def increase_buyout_clause(self, league_id, player_id, amount):
        """Raises the clause of one of your players to protect them."""
        return self.post(self._cmp(
            f"/league/{league_id}/buyout/{player_id}/increase?x-lang=es"),
            {"buyoutClause": amount})

    # --- writes: lineup ---
    def update_lineup(self, team_id, lineup_data):
        return self.put(self._cmp(f"/teams/{team_id}/lineup?x-lang=es"), lineup_data)