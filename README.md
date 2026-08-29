# Setup Instructions

(Please note that I have used a third party library for cofiguring quickbooks client along with intuit-oauth client. I have used them and modified them for my use case and don't own any copyrights for the same.)

1. To Clone the Repo:
   `git clone git@github.com:tahaharness10/aeh-quickbooks-integrations.git`
2. Cd into the root directory:
   `cd aeh-quickbooks-integrations`
3. Setup the virtual Enviroment:
   `python -m venv quickbooksVenv`
4. Launch Virtual Enviroment
   For Linux:
   `source quickbooksVenv/bin/activate`
   For Windows:
   `quickbooksVenv/Scripts/activate`
5. Install [requirements.txt](requirements.txt)
   `pip install -r requirements.txt`
6. Create your local credentials file from the template:
   `cp constants.py.example constants.py`
   Then get your quickbooks credentials using the oAuth 2.0 playground. Follow this [YT video](https://youtu.be/8ZFZhe2HoMY) for reference, and plug those values into `constants.py`.
   `constants.py` is gitignored — keep real credentials out of version control and only ever edit the local copy.
7. You may now run [main.py](main.py) to see it in action. There are two functions to demonstrate fetching of customers and payments data. Note that Quickbooks API allows you to perform tasks beyond that as well.
8. In case you're looking for auto renewal of `refresh_token`, you may comment down in my YT video or raise a request here. I would love to put in a script for the same as well with full instructions.

Note: Please don't forget to update redirect_uri and environment in `constants.py` in case you're shipping your app in production.

```
client_secrets = {
    "client_id": "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    "client_secret": "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    "redirect_uri": "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl",
    "environment": "sandbox"
}
```


## Scheduled syncs

Two GitHub Actions run daily against production QuickBooks and write to Google Sheets:

| Workflow | Schedule (UTC) | Writes to |
| --- | --- | --- |
| `daily_sync.yml` | 06:00 | `GOOGLE_SHEET_NAME` — vendor & customer balances |
| `contributions_sync.yml` | 07:30 | `CONTRIBUTIONS_SHEET_NAME` — per-customer income rollups |

Credentials come from repo secrets/variables, not `constants.py`. QuickBooks rotates
the refresh token, so each workflow writes the new one back to the
`QUICKBOOKS_REFRESH_TOKEN` variable; the schedules are staggered ~1.5h apart to avoid
racing each other for it.

`keepalive.yml` runs weekly. GitHub disables scheduled workflows after 60 days of
repository inactivity — which silently stopped both syncs for 12 days in August 2026 —
so it pushes a no-op commit whenever the repo has been quiet for over 40 days, and
re-enables the sync workflows if they were ever disabled.

Both syncs retry transient Google API failures (503/429/5xx, connection errors) with
exponential backoff, so a single Sheets blip no longer fails the run.
