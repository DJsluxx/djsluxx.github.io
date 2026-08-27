#!/usr/bin/env python3
"""Submit every URL on djsluxx.github.io to IndexNow (Bing / Seznam / Naver).

WHY: the host root now carries an IndexNow key that scopes to the whole
domain, and a host-root sitemap.xml (sitemap index) lists all five
property sitemaps. This script walks that index, collects every URL on
the host, live-checks each one, and pushes the survivors in a single
IndexNow call.

ENDPOINT CHOICE (binding, per WARDEN ruling 2026-08-27): we submit to
Bing's own endpoint (https://www.bing.com/indexnow), NOT the shared
api.indexnow.org aggregator. Bing already propagates accepted URLs to
DuckDuckGo, Ecosia and Copilot; Seznam and Naver also participate in the
IndexNow protocol directly. The aggregator's precise fan-out to a
Russian-operated endpoint (Yandex) was judged an unpriced data/
reputational trade for zero traffic upside, so it is deliberately
excluded here.

RATE POLICY (binding, next-run inherits this): a full-estate submission
like this one runs AT MOST ONCE. Every run after this one must submit
ONLY changed/new URLs, capped at MAX_PER_RUN (20) per invocation, and
must never resubmit a URL that was already submitted within
RESUBMIT_COOLDOWN_DAYS (7) days. `--full` overrides both limits for a
one-time full-estate catch-up and must not become the default habit.
State is tracked in indexnow_log.json (url -> last-submitted ISO date),
committed alongside this script so the policy survives across runs.

USAGE:
    python submit_indexnow.py --dry-run     # verify URLs, print payload, send nothing
    python submit_indexnow.py --full        # one-time: ignore cap/cooldown, submit everything live
    python submit_indexnow.py               # normal run: cap 20/day, skip URLs sent in last 7 days

Stdlib only (urllib) — no extra dependencies on the GitHub Pages build box.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOST = "djsluxx.github.io"
SITEMAP_INDEX = f"https://{HOST}/sitemap.xml"
KEY = "4a56ffb341b533e7cc9bcdcb9f0c1a8e"  # already live at host root, verified 200
KEY_LOCATION = f"https://{HOST}/{KEY}.txt"
ENDPOINT = "https://www.bing.com/indexnow"
LOG_FILE = Path(__file__).resolve().parent / "indexnow_log.json"
SM_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
MAX_URLS = 10_000  # IndexNow's hard cap per request, far above our estate size
MAX_PER_RUN = 20  # non-`--full` runs: submit at most this many URLs
RESUBMIT_COOLDOWN_DAYS = 7  # non-`--full` runs: skip a URL sent within this window


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


def _collect_sitemap_urls() -> list[str]:
    """Read the host-root sitemap index, then every sub-sitemap it lists."""
    index_root = ET.fromstring(_fetch(SITEMAP_INDEX))
    sub_sitemaps = [
        loc.text.strip()
        for loc in index_root.findall(".//sm:sitemap/sm:loc", SM_NS)
        if loc.text
    ]
    urls: list[str] = []
    for sm_url in sub_sitemaps:
        page_root = ET.fromstring(_fetch(sm_url))
        urls += [
            loc.text.strip()
            for loc in page_root.findall(".//sm:url/sm:loc", SM_NS)
            if loc.text
        ]
    return urls


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a redirect into a raised HTTPError instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl}", headers, fp)


def _verify_urls(urls: list[str]) -> tuple[list[str], dict[str, int]]:
    """Keep only URLs that return a direct HTTP 200 (no redirect, no error)."""
    opener = urllib.request.build_opener(_NoRedirect)
    live: list[str] = []
    rejected: dict[str, int] = {}
    for url in urls:
        try:
            resp = opener.open(urllib.request.Request(url, method="HEAD"), timeout=15)
            if resp.status == 200:
                live.append(url)
            else:
                rejected[url] = resp.status
        except urllib.error.HTTPError as e:
            rejected[url] = e.code
        except urllib.error.URLError:
            rejected[url] = 0
    return live, rejected


def _load_log() -> dict[str, str]:
    if not LOG_FILE.exists():
        return {}
    try:
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _apply_rate_policy(urls: list[str], log: dict[str, str], full: bool) -> list[str]:
    if full:
        return urls
    cutoff = datetime.now(timezone.utc) - timedelta(days=RESUBMIT_COOLDOWN_DAYS)
    eligible = [
        u for u in urls
        if u not in log or datetime.fromisoformat(log[u]) < cutoff
    ]
    return eligible[:MAX_PER_RUN]


def _save_log(log: dict[str, str], submitted: list[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for u in submitted:
        log[u] = now
    LOG_FILE.write_text(json.dumps(log, indent=2, sort_keys=True), encoding="utf-8")


def submit(urls: list[str], dry_run: bool) -> int:
    payload = {"host": HOST, "key": KEY, "keyLocation": KEY_LOCATION, "urlList": urls}
    if dry_run:
        print(json.dumps(payload, indent=2))
        print(f"\n[dry-run] would submit {len(urls)} URL(s) to {ENDPOINT}")
        return 0

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code, body = resp.getcode(), resp.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read().decode("utf-8", "replace").strip()
    except urllib.error.URLError as e:
        print(f"ERROR: network failure contacting IndexNow: {e.reason}")
        return 1

    if code in (200, 202):
        print(f"OK: IndexNow (Bing) accepted {len(urls)} URL(s) (HTTP {code})")
        return 0
    print(f"ERROR: IndexNow returned HTTP {code}: {body or '(no body)'}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit estate-wide URLs to IndexNow via Bing.")
    parser.add_argument("--dry-run", action="store_true", help="verify + print payload, send nothing")
    parser.add_argument("--full", action="store_true", help="one-time: bypass cap/cooldown, send everything live")
    args = parser.parse_args()

    print(f"Reading sitemap index: {SITEMAP_INDEX}")
    all_urls = _collect_sitemap_urls()
    if len(all_urls) > MAX_URLS:
        sys.exit(f"ERROR: {len(all_urls)} URLs exceeds IndexNow's {MAX_URLS} cap")
    print(f"Found {len(all_urls)} URL(s) across the sitemap index. Verifying liveness...")

    live_urls, rejected = _verify_urls(all_urls)
    for url, code in rejected.items():
        print(f"DROP ({code or 'network error'}): {url}")
    if not live_urls:
        print("Nothing to submit — no URL passed the liveness check.")
        return 0

    log = _load_log()
    to_submit = _apply_rate_policy(live_urls, log, args.full)
    skipped = len(live_urls) - len(to_submit)
    if skipped:
        print(f"Rate policy: skipping {skipped} URL(s) (cooldown/cap); use --full to override once.")
    if not to_submit:
        print("Nothing to submit — all live URLs are within the resubmit cooldown.")
        return 0

    print(f"Submitting {len(to_submit)} URL(s) to {ENDPOINT} ...")
    result = submit(to_submit, args.dry_run)
    if result == 0 and not args.dry_run:
        _save_log(log, to_submit)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
