# GitHub Actions Workflow for LaLiga Fantasy Automation

## Overview
This workflow automates two LaLiga Fantasy leagues using GitHub Actions:
- HUGO LEAGUE 1 (ID: 017817326) with state in `.state_hugo_league_1/`
- CUEVA + 27 (ID: 018199965) with state in `.state_cueva_plus_27/`

## Workflow File: `.github/workflows/fantasy.yml`

### Schedule
Runs at 09:00, 17:00, and 18:00 Spanish time (approximately 07:00, 15:00, and 16:00 UTC)
- Cron expression: `0 7,15,16 * * *`
- Also supports manual triggers via `workflow_dispatch`

### Jobs
1. **fantasybot-hugo**: Processes HUGO LEAGUE 1
2. **fantasybot-cueva**: Processes CUEVA + 27

### Each Job Performs:
1. Checkout repository with persist-credentials enabled
2. Set up Python 3.12
3. Install dependencies (`pip install -e .`)
4. Run FantasyBot agent: `python -m fantasybot agent --execute`
5. Configure Git for commits
6. Check for state changes and commit/push if any exist

### State Persistence
- Modified `.gitignore` to track specific state directories:
  - `!.state_hugo_league_1/`
  - `!.state_cueva_plus_27/`
- While keeping general `.state/` directory ignored

### Environment Variables Used
- `FANTASY_LEAGUE_ID`: Specifies which league to operate on
- `FANTASY_STATE_DIR`: Specifies which state directory to use

### Notes on Time Zone
The workflow uses fixed UTC times (07:00, 15:00, 16:00) which approximately match:
- 09:00, 17:00, 18:00 CEST (summer time, UTC+2)
- Will be 1 hour off during CET (winter time, UTC+1)

For precise time zone handling, consider adjusting the cron twice yearly or using a timezone-aware scheduling service.

## Manual Testing
To test locally:
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