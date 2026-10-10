#!/usr/bin/env python3
"""Scheduled screenshots with durable per-configuration baselines."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
import yaml
from PIL import Image, ImageChops

ACTOR = 'hiver~bulk-website-screenshots'
API = 'https://api.apify.com/v2'


def load_config(path):
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get('urls'), list) or not data['urls']:
        raise ValueError('urls must be a nonempty list')
    entries = []
    for raw in data['urls']:
        if not isinstance(raw, dict):
            raise ValueError('Each URL entry must be a mapping')
        entry = {'url': raw.get('url'), 'fullPage': raw.get('fullPage', True),
                 'device': raw.get('device', 'desktop'), 'format': raw.get('format', 'png')}
        u = urlsplit(entry['url'] or '')
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            raise ValueError('Use an explicit HTTP(S) URL without credentials')
        if not isinstance(entry['fullPage'], bool):
            raise ValueError('fullPage must be a boolean')
        if entry['device'] not in ('desktop', 'laptop', 'tablet', 'mobile'):
            raise ValueError('device must be desktop, laptop, tablet or mobile')
        if entry['format'] not in ('png', 'jpeg'):
            raise ValueError('Pixel comparison supports png or jpeg, not PDF')
        if entry in entries:
            raise ValueError('Duplicate URL/configuration')
        entries.append(entry)
    threshold = data.get('pixelDiffThreshold', 1.0)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 100:
        raise ValueError('pixelDiffThreshold must be between 0 and 100')
    cap = data.get('maxTotalChargeUsd', 0.1)
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 < cap <= 10:
        raise ValueError('maxTotalChargeUsd must be > 0 and <= 10 per configuration group')
    return entries, float(threshold), float(cap)


def key(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:20]


def pixel_diff(previous, current):
    """Percentage of pixels with any different RGB channel; no resize/crop."""
    with Image.open(previous) as a, Image.open(current) as b:
        a, b = a.convert('RGB'), b.convert('RGB')
        if a.size != b.size:
            return 100.0
        mask = ImageChops.difference(a, b)
        r, g, blue = mask.split()
        hist = ImageChops.lighter(ImageChops.lighter(r, g), blue).histogram()
        return 100.0 * (a.width * a.height - hist[0]) / (a.width * a.height)


class Apify:
    def __init__(self, token, session=None, deadline_seconds=600):
        self.session = session or requests.Session()
        self.headers = {'Authorization': f'Bearer {token}'}
        self.deadline_seconds = deadline_seconds

    def capture(self, entries, cap):
        first = entries[0]
        payload = {name: first[name] for name in ('fullPage', 'device', 'format')}
        payload.update(urls=[e['url'] for e in entries], maxConcurrency=2,
                       hideCookieBanners=True, blockAds=True)
        # Never retry a start POST: ambiguous failures could create duplicate paid runs.
        response = self.session.post(f'{API}/acts/{ACTOR}/runs', headers=self.headers,
                                     params={'timeout': 600, 'memory': 1024, 'maxTotalChargeUsd': cap},
                                     json=payload, timeout=30)
        response.raise_for_status()
        run = response.json()['data']
        deadline = time.monotonic() + self.deadline_seconds
        while run['status'] not in ('SUCCEEDED', 'FAILED', 'TIMED-OUT', 'ABORTED'):
            if time.monotonic() >= deadline:
                raise TimeoutError('Run wait timed out; inspect Apify Console before retrying')
            response = self.session.get(f"{API}/actor-runs/{run['id']}", headers=self.headers,
                                        params={'waitForFinish': 30}, timeout=40)
            response.raise_for_status()
            run = response.json()['data']
        rows, offset = [], 0
        if run.get('defaultDatasetId'):
            while True:
                response = self.session.get(f"{API}/datasets/{run['defaultDatasetId']}/items",
                                            headers=self.headers,
                                            params={'format': 'json', 'clean': 'false', 'offset': offset, 'limit': 1000},
                                            timeout=30)
                response.raise_for_status()
                batch = response.json()
                rows.extend(batch)
                if len(batch) < 1000:
                    break
                offset += len(batch)
        # Even partial/failed runs return their rows; missing URLs are synthesized below.
        return rows, run['status']

    def download(self, url):
        # No token header on image downloads (signed imageUrl is already sufficient).
        if urlsplit(url).scheme != 'https':
            raise ValueError('Image URL must use HTTPS')
        with self.session.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 64 * 1024 * 1024:
                    raise ValueError('Image exceeds 64 MiB download bound')
                chunks.append(chunk)
            return b''.join(chunks)


def failed(entry, message, row=None):
    row = row or {}
    return {'url': entry['url'], 'device': entry['device'], 'format': entry['format'],
            'fullPage': entry['fullPage'], 'outcome': 'failed', 'httpStatus': row.get('httpStatus'),
            'actorStatus': row.get('status'), 'pageHeight': row.get('pageHeight'),
            'truncatedAt': row.get('truncatedAt'), 'imagesPending': row.get('imagesPending'),
            'message': message}


def process(entry, row, state, root, destination, threshold, download):
    status = row.get('httpStatus')
    if row.get('status') != 'ok' or (isinstance(status, int) and status >= 400):
        return failed(entry, 'Actor reported an HTTP or capture failure', row)
    if not row.get('imageUrl'):
        return failed(entry, 'Actor returned no imageUrl', row)
    ident = key(entry)
    target = destination / (ident + ('.jpg' if entry['format'] == 'jpeg' else '.png'))
    try:
        content = download(row['imageUrl'])
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        target.write_bytes(content)
        old = state.get(ident)
        diff = pixel_diff(root / old['image'], target) if old else None
    except Exception:
        # Avoid leaking signed URLs, tokens or webhook URLs through exception text.
        return failed(entry, 'Image download, decode or baseline comparison failed', row)
    height_changed = bool(old and old.get('pageHeight') != row.get('pageHeight'))
    outcome = 'changed' if old is None or height_changed or diff > threshold else 'unchanged'
    result = {**failed(entry, '', row), 'outcome': outcome, 'pixelDiffPercent': diff,
              'pageHeightChanged': height_changed, 'image': str(target.relative_to(root)),
              'message': 'New baseline' if old is None else 'Compared with last successful capture'}
    state[ident] = {'image': result['image'], 'pageHeight': row.get('pageHeight')}
    return result


def cell(value):
    return str(value if value is not None else 'n/a').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ').replace('<', '&lt;').replace('>', '&gt;')


def report(results, stamp):
    lines = [f'# Website screenshot monitor — {stamp}', '',
             'A first capture is changed (new baseline). Failures never advance the baseline.', '',
             '| URL | Device / full page / format | Result | httpStatus | Pixel diff % | pageHeight | Height changed | truncatedAt | imagesPending | Notes |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for row in results:
        diff = row.get('pixelDiffPercent')
        values = [row['url'], f"{row['device']} / {row['fullPage']} / {row['format']}", row['outcome'],
                  row.get('httpStatus'), f'{diff:.4f}' if diff is not None else None,
                  row.get('pageHeight'), row.get('pageHeightChanged'), row.get('truncatedAt'),
                  row.get('imagesPending'), row['message']]
        lines.append('| ' + ' | '.join(cell(v) for v in values) + ' |')
    return '\n'.join(lines) + '\n'


def run(entries, threshold, cap, root, client, stamp=None):
    stamp = stamp or datetime.now(timezone.utc).strftime('%Y-%m-%d/%H%M%S-%fZ')
    destination = root / 'screenshots' / stamp
    destination.mkdir(parents=True, exist_ok=False)
    statefile = root / 'state.json'
    state = json.loads(statefile.read_text()) if statefile.exists() else {}
    groups = {}
    for entry in entries:
        groups.setdefault((entry['fullPage'], entry['device'], entry['format']), []).append(entry)
    results = []
    for group in groups.values():
        try:
            rows, run_status = client.capture(group, cap)
        except Exception:
            results.extend(failed(e, 'Apify API/start/wait/dataset failure; inspect Console before retrying') for e in group)
            continue
        by_url = {row.get('url'): row for row in rows}
        for entry in group:
            row = by_url.get(entry['url'])
            if row is None:
                results.append(failed(entry, f'No dataset row (run {run_status})'))
            else:
                results.append(process(entry, row, state, root, destination, threshold, client.download))
    text = report(results, stamp)
    (destination / 'report.md').write_text(text)
    (root / 'report.md').write_text(text)
    # Do not persist signed imageUrl values or tokens in results.
    (destination / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    temporary = root / 'state.json.new'
    temporary.write_text(json.dumps(state, indent=2) + '\n')
    temporary.replace(statefile)
    return results, text


def notify(text, webhook, kind, session=None):
    if kind not in ('slack', 'discord'):
        raise ValueError('Webhook kind must be slack or discord')
    if urlsplit(webhook).scheme != 'https':
        raise ValueError('Webhook must use HTTPS')
    session = session or requests.Session()
    # Plain digest with no mass mentions; split to stay inside Discord limits.
    limit = 1800
    for offset in range(0, len(text), limit):
        payload = {'text': text[offset:offset+limit]} if kind == 'slack' else {
            'content': text[offset:offset+limit], 'allowed_mentions': {'parse': []}}
        response = session.post(webhook, json=payload, timeout=20)
        response.raise_for_status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='urls.yaml')
    parser.add_argument('--output', default='monitor-data')
    args = parser.parse_args()
    try:
        entries, threshold, cap = load_config(args.config)
        token = os.environ.get('APIFY_TOKEN')
        if not token:
            raise ValueError('Set APIFY_TOKEN to your own Apify token')
        results, text = run(entries, threshold, cap, Path(args.output), Apify(token))
        print(text)
        webhook = os.environ.get('SCREENSHOT_WEBHOOK_URL')
        if webhook:
            try:
                notify(text, webhook, os.environ.get('SCREENSHOT_WEBHOOK_KIND', 'slack'))
            except Exception:
                print('Webhook delivery failed; report and baselines are saved. No automatic retry.')
                return 2
        return 1 if any(r['outcome'] == 'failed' for r in results) else 0
    except ValueError as exc:
        print(f'Configuration error: {exc}')
        return 2
    except Exception:
        print('Monitor failed; check configuration and local state. Secrets are not logged.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
