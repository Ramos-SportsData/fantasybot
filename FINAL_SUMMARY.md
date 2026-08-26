# Summary of Changes

## Goal
Automate two LaLiga Fantasy leagues (HUGO LEAGUE 1 and CUEVA + 27) by allowing the user to specify which league and state directory to use via environment variables.

## Changes Made

### 1. League Selection (`fantasybot/api.py`)
- Modified `default_ids()` to check for `FANTASY_LEAGUE_ID` environment variable first.
- Falls back to `FANTASYBOT_LEAGUE` for backward compatibility.
- If neither is set, defaults to the user's first league (existing behavior).

### 2. State Directory (`fantasybot/state.py`)
- Changed `STATE_DIR` to use the value of `FANTASY_STATE_DIR` environment variable if set.
- Otherwise, defaults to `.state` under the project root (existing behavior).

### 3. Test Updates (`tests/test_league_select.py`)
- Updated tests to validate the new `FANTASY_LEAGUE_ID` environment variable.
- Ensured backward compatibility with `FANTASYBOT_LEAGUE`.
- Added test for precedence when both variables are set (new variable takes priority).

## Usage
To run automation for a specific league and state directory:

```bash
# For HUGO LEAGUE 1
set FANTASY_LEAGUE_ID=017817326
set FANTASY_STATE_DIR=.state_hugo_league_1
python -m fantasybot agent --execute

# For CUEVA + 27
set FANTASY_LEAGUE_ID=018199965
set FANTASY_STATE_DIR=.state_cueva_plus_27
python -m fantasybot agent --execute
```

## Backward Compatibility
- Existing usage of `FANTASYBOT_LEAGUE` continues to work.
- If no environment variables are set, the bot uses the first league and `.state` directory as before.

## Verification
All tests in `tests/test_league_select.py` pass, confirming the changes work correctly.