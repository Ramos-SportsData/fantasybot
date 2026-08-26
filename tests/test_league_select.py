"""default_ids() league selection: default to the first league, or pin one via
the FANTASY_LEAGUE_ID or FANTASYBOT_LEAGUE env var (how the hosted service drives
multi-league accounts). No network — leagues() is stubbed.
"""

import os
import unittest

from fantasybot.api import FantasyClient, FantasyError


_LEAGUES = [
    {"id": "017756520", "name": "Los chavalee 5.0", "team": {"id": 37101688}},
    {"id": "017896981", "name": "STACK ATTACK", "team": {"id": 42000001}},
]


def _client(leagues=_LEAGUES):
    # Build without __init__ so we don't touch tokens.json; stub the API read.
    fc = FantasyClient.__new__(FantasyClient)
    fc.leagues = lambda: leagues
    return fc


class LeagueSelect(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FANTASY_LEAGUE_ID", None)
        os.environ.pop("FANTASYBOT_LEAGUE", None)

    def tearDown(self):
        os.environ.pop("FANTASY_LEAGUE_ID", None)
        os.environ.pop("FANTASYBOT_LEAGUE", None)

    def test_defaults_to_first_league(self):
        self.assertEqual(_client().default_ids(), ("017756520", "37101688"))

    def test_new_env_var_pins_a_specific_league(self):
        os.environ["FANTASY_LEAGUE_ID"] = "017896981"
        self.assertEqual(_client().default_ids(), ("017896981", "42000001"))

    def test_old_env_var_still_works(self):
        os.environ["FANTASYBOT_LEAGUE"] = "017896981"
        self.assertEqual(_client().default_ids(), ("017896981", "42000001"))

    def test_new_env_var_takes_precedence_over_old(self):
        os.environ["FANTASY_LEAGUE_ID"] = "017756520"
        os.environ["FANTASYBOT_LEAGUE"] = "017896981"
        self.assertEqual(_client().default_ids(), ("017756520", "37101688"))

    def test_env_var_matches_regardless_of_type(self):
        # league ids can come through as int or str; match must be tolerant.
        os.environ["FANTASY_LEAGUE_ID"] = "017756520"
        fc = _client([{"id": 17756520, "team": {"id": 1}},
                      {"id": "017756520", "team": {"id": 2}}])
        # exact string match wins; "017756520" != str(17756520) so it picks the 2nd
        self.assertEqual(fc.default_ids(), ("017756520", "2"))

    def test_unknown_league_raises(self):
        os.environ["FANTASY_LEAGUE_ID"] = "999999999"
        with self.assertRaises(FantasyError):
            _client().default_ids()

    def test_no_leagues_raises(self):
        with self.assertRaises(FantasyError):
            _client([]).default_ids()


if __name__ == "__main__":
    unittest.main()
