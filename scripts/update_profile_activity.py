#!/usr/bin/env python3
"""Render profile activity from public GitHub metadata; Python standard library only."""
import argparse
from collections import Counter
from datetime import date, datetime, timezone
from html import escape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

USER = 'Pratham2511'
CALENDAR_URL = f'https://github.com/users/{USER}/contributions'
PR_FIELDS = 'number title url repository { nameWithOwner url owner { login } }'
CONNECTION = 'issueCount pageInfo { hasNextPage endCursor } nodes { ... on PullRequest { %s } }' % PR_FIELDS
MERGED_QUERY = f'is:pr is:merged is:public author:{USER} -user:{USER} sort:updated-desc'
OPEN_QUERY = f'is:pr is:open is:public author:{USER} -user:{USER} sort:updated-desc'
INITIAL = '''query($mergedQuery:String!,$openQuery:String!){
 user(login:"Pratham2511"){login}
 merged:search(query:$mergedQuery,type:ISSUE,first:100){%s}
 opened:search(query:$openQuery,type:ISSUE,first:100){%s}}
''' % (CONNECTION, CONNECTION)
PAGE = 'query($query:String!,$cursor:String!){result:search(query:$query,type:ISSUE,first:100,after:$cursor){%s}}' % CONNECTION


class ProfileCalendar(HTMLParser):
    """Read the visible calendar, including publicly shared anonymous private counts."""

    def __init__(self):
        super().__init__()
        self.cells = {}
        self.tooltips = {}
        self.heading = []
        self.capture = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'ContributionCalendar-day' in attrs.get('class', '').split() and 'data-date' in attrs:
            identity = attrs.get('id')
            day = attrs['data-date']
            if not identity or identity in self.cells or day in self.cells.values():
                raise ValueError('Duplicate or missing calendar-day identity.')
            date.fromisoformat(day)
            self.cells[identity] = day
        if tag == 'h2' and attrs.get('id') == 'js-contribution-activity-description':
            self.capture = ('h2', 'heading')
            self.text = []
        elif tag == 'tool-tip' and attrs.get('for'):
            self.capture = ('tool-tip', attrs['for'])
            self.text = []

    def handle_data(self, text):
        if self.capture:
            self.text.append(text)

    def handle_endtag(self, tag):
        if self.capture and tag == self.capture[0]:
            _, identity = self.capture
            text = ' '.join(''.join(self.text).split())
            if identity == 'heading':
                self.heading.append(text)
            else:
                if identity in self.tooltips:
                    raise ValueError('Duplicate calendar-day tooltip.')
                self.tooltips[identity] = text
            self.capture = None

    def calendar(self):
        if len(self.heading) != 1 or not 365 <= len(self.cells) <= 371:
            raise ValueError('Missing or incomplete past-year profile calendar.')
        total = re.fullmatch(r'([\d,]+) contributions? in the last year', self.heading[0])
        if not total:
            raise ValueError('Unexpected profile calendar heading.')
        days = []
        for identity, day in self.cells.items():
            tooltip = self.tooltips.get(identity, '')
            count = re.fullmatch(r'([\d,]+) contributions? on .+\.', tooltip)
            if re.fullmatch(r'No contributions on .+\.', tooltip):
                value = 0
            elif count:
                value = int(count[1].replace(',', ''))
            else:
                raise ValueError('Missing or unexpected calendar-day count.')
            days.append({'date': day, 'contributionCount': value})
        days.sort(key=lambda day: day['date'])
        dates = [date.fromisoformat(day['date']) for day in days]
        if any((b - a).days != 1 for a, b in zip(dates, dates[1:])):
            raise ValueError('Profile calendar days are not consecutive.')
        total = int(total[1].replace(',', ''))
        if sum(day['contributionCount'] for day in days) != total:
            raise ValueError('Profile calendar heading and daily totals disagree.')
        return {'totalContributions': total, 'weeks': [{'contributionDays': days}]}


def fetch_calendar():
    # Public page only: no browser session, Authorization header or personal token.
    request = urllib.request.Request(CALENDAR_URL, headers={
        'User-Agent': 'Pratham2511-profile-activity', 'Accept-Language': 'en-US,en;q=0.9'})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                document = response.read(2_000_001)
            if len(document) > 2_000_000:
                raise ValueError('Unexpectedly large profile calendar response.')
            break
        except urllib.error.HTTPError as error:
            if error.code in (502, 503, 504) and attempt == 0:
                time.sleep(2)
                continue
            raise RuntimeError(f'Profile calendar returned HTTP {error.code}; previous cards retained.') from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 0:
                time.sleep(2)
                continue
            raise RuntimeError('Profile calendar unavailable; previous cards retained.') from None
    parser = ProfileCalendar()
    parser.feed(document.decode('utf-8'))
    parser.close()
    return parser.calendar()


def api(query, variables, token):
    request = urllib.request.Request(
        'https://api.github.com/graphql',
        data=json.dumps({'query': query, 'variables': variables}).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                 'User-Agent': 'Pratham2511-profile-activity'},
        method='POST',
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if error.code in (502, 503, 504) and attempt == 0:
                time.sleep(2)
                continue
            raise RuntimeError(f'GitHub returned HTTP {error.code}; previous cards retained.') from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 0:
                time.sleep(2)
                continue
            raise RuntimeError('GitHub request unavailable; previous cards retained.') from None
    if payload.get('errors') or not payload.get('data'):
        raise RuntimeError('GitHub GraphQL did not return complete data; previous cards retained.')
    return payload['data']


def collect():
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise RuntimeError('GH_TOKEN or GITHUB_TOKEN is required; never put credentials in files.')
    data = api(INITIAL, {'mergedQuery': MERGED_QUERY, 'openQuery': OPEN_QUERY}, token)
    for key, query in [('merged', MERGED_QUERY), ('opened', OPEN_QUERY)]:
        connection = data[key]
        if connection['issueCount'] > 1000:
            raise RuntimeError('Search exceeds GitHub’s 1,000-result limit; no truncated stats published.')
        cursors = set()
        while connection['pageInfo']['hasNextPage']:
            cursor = connection['pageInfo']['endCursor']
            if not cursor or cursor in cursors:
                raise RuntimeError('Invalid pagination; previous cards retained.')
            cursors.add(cursor)
            page = api(PAGE, {'query': query, 'cursor': cursor}, token)['result']
            if page['issueCount'] != connection['issueCount']:
                raise RuntimeError('Search changed during pagination; retry on the next scheduled run.')
            connection['nodes'].extend(page['nodes'])
            connection['pageInfo'] = page['pageInfo']
    data['user']['contributionsCollection'] = {'contributionCalendar': fetch_calendar()}
    return data


def summarize(data, generated_at):
    if data['user']['login'].casefold() != USER.casefold():
        raise ValueError('Unexpected GitHub account.')
    calendar = data['user']['contributionsCollection']['contributionCalendar']
    days = [dict(x) for week in calendar['weeks'] for x in week['contributionDays']]
    dates = [date.fromisoformat(x['date']) for x in days]
    if not days or len(set(dates)) != len(dates):
        raise ValueError('Missing or duplicate contribution-calendar dates.')
    if any((b - a).days != 1 for a, b in zip(dates, dates[1:])):
        raise ValueError('Contribution calendar must be consecutive.')
    if any(type(x['contributionCount']) is not int or x['contributionCount'] < 0 for x in days):
        raise ValueError('Invalid contribution count.')
    if sum(x['contributionCount'] for x in days) != calendar['totalContributions']:
        raise ValueError('Contribution total does not match its displayed period.')
    merged, opened = [], []
    for key, result in [('merged', merged), ('opened', opened)]:
        connection = data[key]
        nodes = connection['nodes']
        if connection['pageInfo']['hasNextPage'] or len(nodes) != connection['issueCount']:
            raise ValueError('Incomplete PR search; do not publish partial totals.')
        seen = set()
        for item in nodes:
            repo = item['repository']['nameWithOwner']
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
                raise ValueError('Unexpected repository identity.')
            if item['repository']['owner']['login'].casefold() == USER.casefold():
                raise ValueError('Owned repositories are excluded from external PR metrics.')
            if item['url'] != f'https://github.com/{repo}/pull/{item["number"]}' or item['url'] in seen:
                raise ValueError('Unexpected or duplicate PR identity.')
            seen.add(item['url'])
            result.append({'repository': repo, 'number': item['number'], 'url': item['url'], 'title': item['title']})
    # A contribution today extends the streak; a still-empty today does not erase yesterday’s streak.
    end = len(days) - 1
    if not days[end]['contributionCount']:
        end -= 1
    streak = 0
    while end >= 0 and days[end]['contributionCount']:
        streak += 1
        end -= 1
    longest = run = 0
    for day in days:
        run = run + 1 if day['contributionCount'] else 0
        longest = max(longest, run)
    counts = Counter(x['repository'] for x in merged)
    ranking = sorted(counts.items(), key=lambda x: (-x[1], x[0].casefold()))
    return {'user': USER, 'updatedAtUTC': generated_at, 'calendarSource': CALENDAR_URL, 'windowStart': days[0]['date'],
            'windowEnd': days[-1]['date'], 'contributions': calendar['totalContributions'],
            'currentStreakDays': streak, 'longestStreakInWindowDays': longest, 'mergedAuthoredPRs': len(merged),
            'externalRepositoriesWithMergedPRs': len(counts),
            'topRepositories': [{'repository': repo, 'mergedPRs': count} for repo, count in ranking[:5]],
            'openPRCount': len(opened), 'openPRs': opened, 'days': days}


class SVG:
    def __init__(self, width, height, light):
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title description">',
                      '<title id="title">Pratham2511 GitHub activity</title>',
                      '<desc id="description">Past-year contributions, current and longest daily streaks, ranked external repositories, a large authored merged-PR total and open-PR count. No calendar or individual open-PR list.</desc>']
        self.ink = '#1F2328' if light else '#E6EDF3'
        self.muted = '#59636E' if light else '#9DA7B3'
        self.accent = '#6E40C9' if light else '#B59BFF'
        self.track = '#EAE7F0' if light else '#21262D'
        self.levels = ['#ECEAF1', '#DCCCF8', '#B695EE', '#8C62D6', '#6435AD'] if light else ['#21262D', '#382B52', '#5D4282', '#8960BA', '#BE91F5']

    def text(self, x, y, value, size=17, weight=400, color=None, anchor='start', family='Arial, Helvetica, sans-serif'):
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{escape(family, quote=True)}" font-size="{size}" font-weight="{weight}" fill="{color or self.ink}" text-anchor="{anchor}">{escape(str(value))}</text>')

    def rect(self, x, y, w, h, color, radius=3):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{color}"/>')

    def link(self, url, label, x, y, size=17, color=None):
        self.parts.append(f'<a href="{escape(url, quote=True)}">')
        self.text(x, y, label, size, color=color or self.accent)
        self.parts.append('</a>')

    def done(self):
        return '\n'.join(self.parts + ['</svg>', ''])


def render(stats, mobile=False, light=False):
    width, height = (420, 820) if mobile else (1100, 560)
    svg = SVG(width, height, light)
    # Transparent graphics inherit GitHub's normal page background.
    x0 = 18 if mobile else 24
    svg.text(x0, 34, 'GitHub activity', 25, 700)
    svg.text(x0, 61, 'Pratham2511 · public contribution profile', 14, color=svg.muted)
    metrics = [(f'{stats["contributions"]:,}', 'Contributions · past year'),
               (f'{stats["currentStreakDays"]} days', 'Current daily streak'),
               (f'{stats["longestStreakInWindowDays"]} days', 'Longest streak · past year')]
    for n, (value, label) in enumerate(metrics):
        x = 18 + n * 134 if mobile else 24 + n * 365
        svg.text(x, 125 if mobile else 135, value, 28 if mobile else 44, 700)
        if mobile:
            top, bottom = [('Contributions', 'Past year'), ('Current streak', 'Daily'), ('Longest streak', 'Past year')][n]
            svg.text(x, 151, top, 12, color=svg.muted)
            svg.text(x, 170, bottom, 11, color=svg.muted)
        else:
            svg.text(x, 166, label, 17, color=svg.muted)
    top_y = 234 if mobile else 244
    svg.text(x0, top_y, 'Top external repositories', 20, 700)
    svg.text(x0, top_y + 25, 'Ranked by your authored merged PRs', 13, color=svg.muted)
    maximum = max((x['mergedPRs'] for x in stats['topRepositories']), default=1)
    for n, item in enumerate(stats['topRepositories']):
        y = top_y + 62 + n * 41
        svg.link('https://github.com/' + item['repository'], item['repository'], x0, y, 14 if mobile else 16)
        svg.text(400 if mobile else 510, y, str(item['mergedPRs']), 16, 600, anchor='end')
        svg.rect(x0, y + 11, 382 if mobile else 486, 5, svg.track, 2)
        svg.rect(x0, y + 11, round((382 if mobile else 486) * item['mergedPRs'] / maximum, 1), 5, svg.accent, 2)
    center = 210 if mobile else 832
    svg.text(center, 660 if mobile else 372, stats['mergedAuthoredPRs'], 104 if mobile else 132,
             800, svg.accent, 'middle', 'Arial, Helvetica, sans-serif')
    svg.text(center, 701 if mobile else 418, 'Authored merged PRs', 22, 600, anchor='middle')
    svg.text(center, 735 if mobile else 455, f'{stats["openPRCount"]} open PRs', 17,
             color=svg.muted, anchor='middle')
    stamp = datetime.fromisoformat(stats['updatedAtUTC'].replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Kolkata')).strftime('%b %d, %Y %H:%M IST')
    svg.text(x0, height - 34, 'Public PR metrics exclude owned repos and peer-review-only work.', 10 if mobile else 12, color=svg.muted)
    svg.text(x0, height - 14, f'Updated {stamp} · streaks use GitHub calendar dates', 10 if mobile else 12, color=svg.muted)
    return svg.done()


def write_assets(stats, output):
    rendered = {}
    for mobile, suffix in [(False, ''), (True, '-mobile')]:
        for light, theme in [(False, ''), (True, '-light')]:
            rendered['profile-stats' + suffix + theme + '.svg'] = render(stats, mobile, light)
    # Keep only aggregate/public display data; no token, raw API data or private repository details.
    rendered['profile-activity.json'] = json.dumps({k: v for k, v in stats.items() if k not in ('days', 'openPRs')}, indent=2) + '\n'
    output.mkdir(parents=True, exist_ok=True)
    for name, text in rendered.items():
        fd, temporary = tempfile.mkstemp(prefix='.profile-', dir=output)
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(text)
            os.chmod(temporary, 0o644)
            os.replace(temporary, output / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-json', type=Path, help='Use a captured public response for local preview/testing.')
    parser.add_argument('--output-dir', type=Path, default=Path('assets'))
    parser.add_argument('--updated-at', help='Explicit ISO timestamp for a reproducible local preview.')
    args = parser.parse_args()
    data = json.loads(args.input_json.read_text()) if args.input_json else collect()
    stamp = args.updated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    stats = summarize(data, stamp)
    write_assets(stats, args.output_dir)
    print(f'Rendered public activity for {USER}: {stats["mergedAuthoredPRs"]} merged authored PRs, {stats["externalRepositoriesWithMergedPRs"]} external repositories.')


if __name__ == '__main__':
    try:
        main()
    except (KeyError, TypeError, ValueError, RuntimeError, OSError) as error:
        print(f'Activity update failed: {error}', file=sys.stderr)
        raise SystemExit(1)
