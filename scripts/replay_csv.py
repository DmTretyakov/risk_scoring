"""Replays the supplied CSV export through the API, one HTTP request per row.

Acts as the source adapter: maps the export's null tokens ("", "null", "NA",
"n/a") to JSON null and leaves every other value as sent. Standard library only.

    python3 scripts/replay_csv.py --url http://127.0.0.1:8010 data/raw_applications.csv
"""
import argparse
import collections
import csv
import json
import time
import urllib.error
import urllib.request

NULL_TOKENS = {"", "null", "na", "n/a"}


def row_to_payload(row: dict) -> dict:
    return {key: None if value.strip().lower() in NULL_TOKENS else value.strip() for key, value in row.items()}


def post(url: str, payload: dict, waits: collections.Counter) -> tuple[int, dict]:
    """POST one application; on 429/503 waits Retry-After and resends (safe: requests are idempotent)."""
    request = urllib.request.Request(f"{url}/v1/applications", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    for _ in range(20):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            code, body = error.code, json.load(error)
            if code not in (429, 503):
                return code, body
            waits[code] += 1
            time.sleep(float(error.headers.get("Retry-After", "1")))
    return code, body   # still 429/503 after 20 attempts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_path")
    parser.add_argument("--url", default="http://127.0.0.1:8010")
    args = parser.parse_args()

    http, status, risk, issues, waits = (collections.Counter() for _ in range(5))
    with open(args.csv_path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            code, body = post(args.url, row_to_payload(row), waits)
            http[code] += 1
            if code == 201:                       # each application once, not its retries
                status[body["status"]] += 1
                if body["decision"]:
                    risk[body["decision"]["risk_band"]] += 1
                issues.update(f'{i["severity"]}:{i["code"]}' for i in body["issues"])

    print("HTTP codes    ", dict(http), " (201 new, 200 idempotent retry)")
    print("Backoffs      ", dict(waits), " (429 rate limit / 503 scorer down, then resent)")
    print("New apps      ", dict(status))
    print("Risk bands    ", dict(risk))
    print("Issues        ")
    for name, count in issues.most_common():
        print(f"  {count:4d}  {name}")


if __name__ == "__main__":
    main()
