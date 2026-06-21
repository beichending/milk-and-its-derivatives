# SGX Butter Futures daily collector

This tool collects every field returned for every active SGX-NZX Global Butter
Futures (`BTR`) contract, stores daily history in SQLite, exports JSON/CSV, and
runs historical anomaly checks.

## Data source

- Product page: <https://www.sgx.com/derivatives/products/dairy?cc=BTR>
- Current contracts: `https://api.sgx.com/derivatives/v1.0/contract-code/BTR?category=futures`
- Per-symbol history: `https://api.sgx.com/derivatives/v1.0/history/symbol/{symbol}?category=futures&days=1y`

The collector keeps the complete SGX response in `raw_json` and also writes
every returned key into `contract_fields`, so newly introduced SGX fields are
not silently lost.

## Run

```powershell
Copy-Item .\config.example.json .\config.json
.\run_daily.ps1
```

First run backfills up to one year for every currently listed delivery month.
Subsequent runs skip symbols that already have enough history.

Check status:

```powershell
& "C:\Users\dbcmi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  .\sgx_butter_collector.py --config .\config.json status
```

## Daily scheduling

Install a Windows Scheduled Task at 19:30 local time:

```powershell
.\install_task.ps1 -RunAt "19:30"
```

SGX's `base-date` is authoritative. Re-running the task on the same date is
idempotent and updates that date's snapshot rather than duplicating rows.

## Alerts

Default checks:

- Missing/non-positive settlement price
- Large daily settlement return
- Unusual volume spike
- Unusual open-interest change
- Unusual adjacent-month curve spread
- Preliminary/final settlement discrepancy
- New or removed contract symbols
- Stale SGX business dates
- Sudden contract-count drops and duplicate symbols
- Added/removed API fields, so upstream schema changes are visible

Statistical checks use median absolute deviation (MAD), which is more stable
than mean/standard deviation for sparse futures data. Constant-history
tolerances prevent low-activity contracts from hiding genuine volume, OI,
price, or curve changes. Thresholds are in `config.json`.

All alerts are saved to SQLite and `data/alerts.jsonl`. Optional delivery:

- Set `alert.webhook_url` for a generic JSON webhook.
- Set SMTP fields and place the password in the environment variable named by
  `smtp_password_env`.

## Files

- `data/sgx_butter_YYYY-MM-DD.json`: unmodified API response
- `data/sgx_butter_YYYY-MM-DD.csv`: all fields for all active contracts
- `data/sgx_butter.sqlite3`: snapshots, history, all fields, alerts, run audit
- `dashboard.html`: self-contained business dashboard, refreshed after every collection
- `logs/collector-YYYY-MM-DD.log`: collector result log

## Dashboard

Open `dashboard.html` directly in a browser. It presents:

- A historical-date dropdown that rolls the front month and exact +6M contract
- Daily settlement-price changes for the nearest six delivery months
- The contract exactly six calendar months after the front month and its settlement
- Daily `total-volume` for the nearest six contracts; open interest is shown separately
- Daily calendar spread: six-month distant settlement minus front-month settlement
- Rule-based alerts and a direct 1–2 week best-estimate conclusion

The dashboard has no external JavaScript or CSS dependencies and is rebuilt by
the scheduled collector after every successful SGX data update. The first
bootstrap also downloads expired monthly BTR contracts so retrospective dates
use the contracts that were actually near and six months forward at that time.

## Online deployment

The repository includes `.github/workflows/pages.yml` for GitHub Pages:

- It refreshes SGX data at 19:45 China/Singapore time on weekdays.
- It persists the public SQLite market-data history in the repository.
- It publishes `dashboard.html` as the site's `index.html`.
- It can also be run manually from the GitHub Actions page.

Keep `config.json` private. The workflow copies `config.example.json`, which
contains no webhook, SMTP password, or recipient details.

This uses SGX's public website API. SGX may change access rules or fields, so
monitor failed runs and review applicable SGX market-data terms before using
the data commercially or redistributing it.
