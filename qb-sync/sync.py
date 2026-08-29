import os
import json
import random
import time

import gspread
import requests
from dotenv import load_dotenv
from intuitlib.client import AuthClient
from quickbooks import QuickBooks
from quickbooks.objects.vendor import Vendor
from quickbooks.objects.customer import Customer
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


def build_qb_client():
    auth_client = AuthClient(
        client_id=os.environ['QUICKBOOKS_CLIENT_ID'],
        client_secret=os.environ['QUICKBOOKS_CLIENT_SECRET'],
        environment='prod',
        redirect_uri='https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl',
    )
    qb = QuickBooks(
        auth_client=auth_client,
        refresh_token=os.environ['QUICKBOOKS_REFRESH_TOKEN'],
        company_id=os.environ['QUICKBOOKS_COMPANY_ID'],
    )
    return qb, auth_client


def fetch_all(model_class, qb):
    records, start, page_size = [], 1, 1000
    while True:
        batch = model_class.query(
            f"SELECT * FROM {model_class.__name__} STARTPOSITION {start} MAXRESULTS {page_size}",
            qb=qb,
        )
        if not batch:
            break
        records.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return records


def build_sheets_client():
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]
    service_account_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
    if service_account_json:
        creds = Credentials.from_service_account_info(json.loads(service_account_json), scopes=scopes)
    else:
        # Fallback for local dev using a file path
        creds = Credentials.from_service_account_file(
            os.environ['GOOGLE_SERVICE_ACCOUNT_FILE'], scopes=scopes
        )
    return gspread.authorize(creds)


def write_sheet(worksheet, header, rows):
    retry_api('clear worksheet', worksheet.clear)
    retry_api('write header', worksheet.append_row, header)
    if rows:
        retry_api('write rows', worksheet.append_rows, rows)


def emit_github_output(key, value):
    """Write a step output so the workflow can read it (no-op when running locally)."""
    output_file = os.getenv('GITHUB_OUTPUT')
    if output_file:
        with open(output_file, 'a') as f:
            f.write(f"{key}={value}\n")


def main():
    original_token = os.environ['QUICKBOOKS_REFRESH_TOKEN']

    qb, auth_client = build_qb_client()

    # Emit the rotated token immediately after auth so it's persisted even if
    # later steps (data fetch, Sheets write) fail.
    new_token = getattr(auth_client, 'refresh_token', None)
    if new_token and new_token != original_token:
        print("Refresh token rotated — emitting new token to workflow output")
        emit_github_output('new_refresh_token', new_token)
    else:
        emit_github_output('new_refresh_token', '')

    vendors = fetch_all(Vendor, qb)
    customers = fetch_all(Customer, qb)

    gc = build_sheets_client()
    spreadsheet = retry_api(
        'open spreadsheet',
        gc.open,
        os.environ.get('GOOGLE_SHEET_NAME', 'Najmi_QH_Balances'),
    )

    write_sheet(
        retry_api('open Sheet1', spreadsheet.worksheet, 'Sheet1'),
        ['ID', 'Name', 'Balance'],
        [[v.Id, v.DisplayName, v.Balance] for v in vendors],
    )
    write_sheet(
        retry_api('open Sheet2', spreadsheet.worksheet, 'Sheet2'),
        ['ID', 'Name', 'Balance'],
        [[c.Id, c.DisplayName, c.Balance] for c in customers],
    )

    print(f"Done: {len(vendors)} vendors, {len(customers)} customers")


if __name__ == '__main__':
    main()
