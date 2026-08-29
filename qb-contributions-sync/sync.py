import os
import json
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

QB_BASE_URL = 'https://quickbooks.api.intuit.com'
DEFAULT_CATEGORIES = 'Sabil ul khair,Faizul Mawaidil Burhaniya'
DEFAULT_SHEET_NAME = 'Najmi_Contributions'


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
    """Return Accounts matching the category — the account itself plus any sub-accounts."""
    esc = category_name.replace("'", "\\'")
    candidates = Account.query(
        f"SELECT Id, Name, FullyQualifiedName, AccountType FROM Account "
        f"WHERE FullyQualifiedName LIKE '{esc}%'",
        qb=qb,
    )
    matches = []
    for a in candidates:
        fqn = a.FullyQualifiedName or ''
        if fqn == category_name or fqn.startswith(f"{category_name}:"):
            matches.append(a)
    return matches


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


def aggregate_by_customer(report_json):
    name_idx = _column_index(report_json, 'Name')
    amount_idx = _column_index(report_json, 'Amount')
    if name_idx is None or amount_idx is None:
        # Fall back to common defaults from Columns metadata if titles vary
        columns = report_json.get('Columns', {}).get('Column', [])
        col_types = [c.get('ColType', '') for c in columns]
        if name_idx is None:
            for i, t in enumerate(col_types):
                if t in ('String',) and columns[i].get('ColTitle', '').lower() in ('name', 'customer'):
                    name_idx = i
                    break
        if amount_idx is None:
            for i, t in enumerate(col_types):
                if t == 'Money':
                    amount_idx = i
                    break

    totals = defaultdict(lambda: {'total': 0.0, 'count': 0})
    all_rows = list(_walk_data_rows(report_json.get('Rows', {}).get('Row', [])))
    for row in all_rows:
        col_data = row.get('ColData', [])
        name = ''
        if name_idx is not None and name_idx < len(col_data):
            name = (col_data[name_idx].get('value') or '').strip()
        name = name or '(No Customer)'
        amount_raw = ''
        if amount_idx is not None and amount_idx < len(col_data):
            amount_raw = col_data[amount_idx].get('value') or ''
        try:
            amount = float(amount_raw) if amount_raw else 0.0
        except ValueError:
            amount = 0.0
        totals[name]['total'] += amount
        totals[name]['count'] += 1
    return totals, len(all_rows)


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
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def write_sheet(worksheet, header, rows, footer_lines):
    worksheet.clear()
    worksheet.append_row(header)
    if rows:
        worksheet.append_rows(rows)
    worksheet.append_row([])
    for line in footer_lines:
        worksheet.append_row([line])


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
        print("Refresh token rotated — emitting new token to workflow output")
        emit_github_output('new_refresh_token', new_token)
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
    spreadsheet = gc.open(os.environ.get('GOOGLE_SHEET_NAME', DEFAULT_SHEET_NAME))
    generated_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

    for category in categories:
        print(f"\nProcessing: {category}")
        accounts = find_account_family(qb, category)
        if not accounts:
            print(f"  WARNING: No accounts matched '{category}' — skipping")
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
        totals, row_count = aggregate_by_customer(report)
        print(f"  {row_count} transaction lines, {len(totals)} distinct customers")

        sorted_customers = sorted(totals.items(), key=lambda kv: kv[1]['total'], reverse=True)
        sheet_rows = [
            [name, round(v['total'], 2), v['count']]
            for name, v in sorted_customers
        ]
        grand_total = round(sum(v['total'] for v in totals.values()), 2)

        tab_title = category[:99]
        ws = ensure_worksheet(spreadsheet, tab_title, rows=max(len(sheet_rows) + 10, 100))
        write_sheet(
            ws,
            ['Customer', 'Total Contribution', 'Transaction Count'],
            sheet_rows,
            footer_lines=[
                f'Date range: {start_date} to {end_date}',
                f'Grand total: {grand_total}',
                f'Source accounts: {", ".join(a.FullyQualifiedName for a in accounts)}',
                f'Generated: {generated_at}',
            ],
        )
        print(f"  Grand total: {grand_total}")

    print("\nDone")


if __name__ == '__main__':
    main()
