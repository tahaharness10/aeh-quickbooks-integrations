import os
import re
import json
import random
import time

import requests
import gspread
from datetime import datetime, date
from collections import defaultdict
from dotenv import load_dotenv
from intuitlib.client import AuthClient
from quickbooks import QuickBooks
from quickbooks.objects.account import Account
from google.oauth2.service_account import Credentials

load_dotenv()


# Google's Sheets/Drive APIs intermittently return 503/429 under load. A single
# blip used to abort the whole daily run, so transient failures are retried with
# exponential backoff + jitter before we give up.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


def _is_retryable(err):
    if isinstance(err, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    # APIError.code comes from the JSON body and is -1 when Google returns an
    # HTML error page, so key off the real HTTP status instead.
    if isinstance(err, gspread.exceptions.APIError):
        return err.response.status_code in RETRYABLE_STATUS
    return False


def retry_api(description, fn, *args, **kwargs):
    """Run a Google API call, retrying transient failures."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as err:
            if not _is_retryable(err) or attempt == MAX_ATTEMPTS:
                raise
            delay = min(2 ** (attempt - 1), 16) + random.uniform(0, 1)
            print(f"{description} failed ({err}); retrying in {delay:.1f}s "
                  f"[attempt {attempt}/{MAX_ATTEMPTS - 1}]", flush=True)
            time.sleep(delay)

QB_BASE_URL = 'https://quickbooks.api.intuit.com'
DEFAULT_CATEGORIES = 'Sabil ul khair,Faizul Mawaidil Burhaniya'
DEFAULT_SHEET_NAME = 'Najmi_Contributions'
NO_YEAR_LABEL = 'Year Not Available'

# Regex per category (matched on substring, case-insensitive) to extract a
# Hijri-year bucket from the transaction memo. Both patterns match either a
# single year (1447) or a year range (1447-48). For Sabil we prefer the single
# year; for Faiz we prefer the range but fall back to the single year.
YEAR_RE = re.compile(r'(14\d{2})(?:\s*[-–—]\s*(\d{2}))?')

# Map Arabic-Indic and Extended Arabic-Indic digits → Latin digits so the same
# regex works regardless of memo encoding.
_ARABIC_DIGIT_MAP = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')


def normalize_memo(memo):
    return (memo or '').translate(_ARABIC_DIGIT_MAP)


def extract_year_bucket(memo, category_name):
    """Return a normalized year-bucket string (e.g. '1447' or '1447-48') or None."""
    normalized = normalize_memo(memo)
    m = YEAR_RE.search(normalized)
    if not m:
        return None
    year, tail = m.group(1), m.group(2)
    lower = category_name.lower()
    if 'faiz' in lower and tail:
        return f"{year}-{tail}"
    # Sabil (or default): use single year even if a range was written
    return year


def build_qb_client():
    auth_client = AuthClient(
        client_id=os.environ['QUICKBOOKS_CLIENT_ID'],
        client_secret=os.environ['QUICKBOOKS_CLIENT_SECRET'],
        environment='prod',
        redirect_uri='https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl',
    )
    # Explicit refresh — we need access_token for direct Reports API calls
    auth_client.refresh(refresh_token=os.environ['QUICKBOOKS_REFRESH_TOKEN'])
    qb = QuickBooks(
        auth_client=auth_client,
        refresh_token=auth_client.refresh_token,
        company_id=os.environ['QUICKBOOKS_COMPANY_ID'],
    )
    return qb, auth_client


def find_account_family(qb, category_name):
    """Return Accounts matching the category (case-insensitive) — the account itself plus any sub-accounts."""
    esc = category_name.replace("'", "\\'")
    candidates = Account.query(
        f"SELECT Id, Name, FullyQualifiedName, AccountType FROM Account "
        f"WHERE FullyQualifiedName LIKE '{esc}%'",
        qb=qb,
    )
    target_lower = category_name.lower()
    matches = []
    for a in candidates:
        fqn = (a.FullyQualifiedName or '').lower()
        if fqn == target_lower or fqn.startswith(f"{target_lower}:"):
            matches.append(a)
    return matches


def list_income_accounts(qb):
    """List all Income accounts to help the user find the right names."""
    return Account.query(
        "SELECT Id, Name, FullyQualifiedName, AccountType, Classification FROM Account "
        "WHERE Classification = 'Revenue'",
        qb=qb,
    )


def fetch_general_ledger(access_token, company_id, account_ids, start_date, end_date):
    url = f"{QB_BASE_URL}/v3/company/{company_id}/reports/GeneralLedger"
    params = {
        'account': ','.join(account_ids),
        'start_date': start_date,
        'end_date': end_date,
        'columns': 'tx_date,txn_type,doc_num,name,memo,account_name,subt_nat_amount',
    }
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json',
    }
    r = requests.get(url, headers=headers, params=params)
    r.raise_for_status()
    return r.json()


def _column_index(report_json, col_title):
    columns = report_json.get('Columns', {}).get('Column', [])
    for i, c in enumerate(columns):
        if c.get('ColTitle', '').strip().lower() == col_title.strip().lower():
            return i
    return None


def _walk_data_rows(rows):
    for r in rows:
        if r.get('type') == 'Data' and 'ColData' in r:
            yield r
        nested = r.get('Rows')
        if nested:
            yield from _walk_data_rows(nested.get('Row', []))


def aggregate_by_customer_and_year(report_json, category_name):
    """Returns (totals, years_seen, row_count).
    totals: {customer: {year_bucket: {'total': float, 'count': int}}}
    years_seen: set of year buckets encountered (year strings + possibly NO_YEAR_LABEL)
    """
    name_idx = _column_index(report_json, 'Name')
    amount_idx = _column_index(report_json, 'Amount')
    memo_idx = _column_index(report_json, 'Memo/Description')
    if memo_idx is None:
        memo_idx = _column_index(report_json, 'Memo')

    totals = defaultdict(lambda: defaultdict(lambda: {'total': 0.0, 'count': 0}))
    years_seen = set()
    all_rows = list(_walk_data_rows(report_json.get('Rows', {}).get('Row', [])))
    for row in all_rows:
        col_data = row.get('ColData', [])
        name = ''
        if name_idx is not None and name_idx < len(col_data):
            name = (col_data[name_idx].get('value') or '').strip()
        name = name or '(No Customer)'

        memo = ''
        if memo_idx is not None and memo_idx < len(col_data):
            memo = (col_data[memo_idx].get('value') or '').strip()

        year_bucket = extract_year_bucket(memo, category_name) or NO_YEAR_LABEL

        amount_raw = ''
        if amount_idx is not None and amount_idx < len(col_data):
            amount_raw = col_data[amount_idx].get('value') or ''
        try:
            amount = float(amount_raw) if amount_raw else 0.0
        except ValueError:
            amount = 0.0

        totals[name][year_bucket]['total'] += amount
        totals[name][year_bucket]['count'] += 1
        years_seen.add(year_bucket)
    return totals, years_seen, len(all_rows)


def ordered_year_columns(years_seen):
    """Sort year buckets chronologically; put NO_YEAR_LABEL at the end."""
    years = sorted(y for y in years_seen if y != NO_YEAR_LABEL)
    if NO_YEAR_LABEL in years_seen:
        years.append(NO_YEAR_LABEL)
    return years


def build_sheets_client():
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]
    service_account_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
    if service_account_json:
        creds = Credentials.from_service_account_info(json.loads(service_account_json), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(
            os.environ['GOOGLE_SERVICE_ACCOUNT_FILE'], scopes=scopes
        )
    return gspread.authorize(creds)


def ensure_worksheet(spreadsheet, title, rows=200, cols=5):
    try:
        return retry_api(f'open worksheet {title!r}', spreadsheet.worksheet, title)
    except gspread.WorksheetNotFound:
        return retry_api(
            f'create worksheet {title!r}',
            spreadsheet.add_worksheet, title=title, rows=rows, cols=cols,
        )


def write_sheet(worksheet, header, rows, footer_lines):
    retry_api('clear worksheet', worksheet.clear)
    retry_api('write header', worksheet.append_row, header)
    if rows:
        retry_api('write rows', worksheet.append_rows, rows)
    retry_api('write spacer', worksheet.append_row, [])
    for line in footer_lines:
        retry_api('write footer', worksheet.append_row, [line])


def emit_github_output(key, value):
    output_file = os.getenv('GITHUB_OUTPUT')
    if output_file:
        with open(output_file, 'a') as f:
            f.write(f"{key}={value}\n")


def main():
    original_token = os.environ['QUICKBOOKS_REFRESH_TOKEN']
    qb, auth_client = build_qb_client()

    new_token = getattr(auth_client, 'refresh_token', None)
    if new_token and new_token != original_token:
        if os.getenv('GITHUB_OUTPUT'):
            print("Refresh token rotated — emitting new token to workflow output")
            emit_github_output('new_refresh_token', new_token)
        else:
            print("\n" + "=" * 70)
            print("REFRESH TOKEN ROTATED (local run) — update both places below:")
            print(f"  NEW TOKEN: {new_token}")
            print("\n  1. Local .env — replace QUICKBOOKS_REFRESH_TOKEN with the value above")
            print("  2. GitHub Variable — run:")
            print(f"     gh variable set QUICKBOOKS_REFRESH_TOKEN --body '{new_token}'")
            print("=" * 70 + "\n")
    else:
        emit_github_output('new_refresh_token', '')

    categories = [
        c.strip()
        for c in os.environ.get('CONTRIBUTION_ACCOUNTS', DEFAULT_CATEGORIES).split(',')
        if c.strip()
    ]

    today = date.today()
    start_date = os.environ.get('CONTRIBUTIONS_START_DATE') or f"{today.year}-01-01"
    end_date = os.environ.get('CONTRIBUTIONS_END_DATE') or today.isoformat()

    gc = build_sheets_client()
    spreadsheet = retry_api(
        'open spreadsheet',
        gc.open,
        os.environ.get('GOOGLE_SHEET_NAME', DEFAULT_SHEET_NAME),
    )
    generated_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

    all_income = None
    for category in categories:
        print(f"\nProcessing: {category}")
        accounts = find_account_family(qb, category)
        if not accounts:
            if all_income is None:
                all_income = list_income_accounts(qb)
                print(f"\n  --- Available income (Revenue) accounts in QB ({len(all_income)} total) ---")
                for a in sorted(all_income, key=lambda x: x.FullyQualifiedName or ''):
                    print(f"    {a.FullyQualifiedName}")
                print("  --- end list ---\n")
            print(f"  WARNING: No accounts matched '{category}' — see list above for actual names")
            continue
        account_ids = [a.Id for a in accounts]
        print(f"  Matched accounts: {[a.FullyQualifiedName for a in accounts]}")

        report = fetch_general_ledger(
            auth_client.access_token,
            os.environ['QUICKBOOKS_COMPANY_ID'],
            account_ids,
            start_date,
            end_date,
        )
        totals, years_seen, row_count = aggregate_by_customer_and_year(report, category)
        year_cols = ordered_year_columns(years_seen)
        print(f"  {row_count} transaction lines, {len(totals)} distinct customers, "
              f"{len(year_cols)} year bucket(s): {year_cols}")

        # Build per-customer rows with per-year totals + a grand-total column for sorting
        customer_rows = []
        for name, year_totals in totals.items():
            per_year = {y: year_totals.get(y, {'total': 0.0, 'count': 0})['total'] for y in year_cols}
            cust_total = sum(per_year.values())
            cust_count = sum(year_totals.get(y, {'total': 0.0, 'count': 0})['count'] for y in year_cols)
            customer_rows.append((name, per_year, cust_total, cust_count))
        customer_rows.sort(key=lambda r: r[2], reverse=True)
        grand_total = round(sum(r[2] for r in customer_rows), 2)
        per_year_totals = {y: round(sum(r[1][y] for r in customer_rows), 2) for y in year_cols}

        # --- Terminal print (pivot) ---
        name_col_width = max((len(name) for name, _, _, _ in customer_rows), default=8)
        name_col_width = min(max(name_col_width, 8), 32)
        year_col_width = 12
        header_cells = [f"{y:>{year_col_width}}" for y in year_cols] + [f"{'Total':>{year_col_width}}", f"{'Txns':>5}"]
        print(f"  {'Customer'.ljust(name_col_width)}  " + '  '.join(header_cells))
        divider = f"  {'-' * name_col_width}  " + '  '.join('-' * year_col_width for _ in year_cols) + f"  {'-' * year_col_width}  {'-' * 5}"
        print(divider)
        for name, per_year, cust_total, cust_count in customer_rows:
            display_name = name if len(name) <= name_col_width else name[:name_col_width - 1] + '…'
            cells = [f"{per_year[y]:>{year_col_width},.2f}" if per_year[y] else f"{'':>{year_col_width}}" for y in year_cols]
            cells.append(f"{round(cust_total, 2):>{year_col_width},.2f}")
            cells.append(f"{cust_count:>5}")
            print(f"  {display_name.ljust(name_col_width)}  " + '  '.join(cells))
        print(divider)
        total_cells = [f"{per_year_totals[y]:>{year_col_width},.2f}" for y in year_cols]
        total_cells.append(f"{grand_total:>{year_col_width},.2f}")
        print(f"  {'GRAND TOTAL'.ljust(name_col_width)}  " + '  '.join(total_cells))

        # --- Sheet write (pivot) ---
        sheet_header = ['Customer'] + year_cols + ['Total', 'Txn Count']
        sheet_rows = []
        for name, per_year, cust_total, cust_count in customer_rows:
            row = [name] + [round(per_year[y], 2) if per_year[y] else '' for y in year_cols]
            row += [round(cust_total, 2), cust_count]
            sheet_rows.append(row)
        # Totals row
        sheet_rows.append(['TOTAL'] + [per_year_totals[y] for y in year_cols] + [grand_total, ''])

        tab_title = category[:99]
        ws = ensure_worksheet(spreadsheet, tab_title, rows=max(len(sheet_rows) + 10, 100), cols=max(len(sheet_header) + 1, 5))
        write_sheet(
            ws,
            sheet_header,
            sheet_rows,
            footer_lines=[
                f'Date range: {start_date} to {end_date}',
                f'Source accounts: {", ".join(a.FullyQualifiedName for a in accounts)}',
                f'Year detection pattern: {YEAR_RE.pattern} (Faiz keeps year-range, Sabil/default keeps year only)',
                f'Generated: {generated_at}',
            ],
        )

    print("\nDone")


if __name__ == '__main__':
    main()
