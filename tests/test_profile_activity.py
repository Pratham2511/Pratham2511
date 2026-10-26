"""Regression checks for truthful aggregation, pagination and safe SVG output."""
import copy
from datetime import date, timedelta
import importlib.util
import io
import json
import urllib.error
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/update_profile_activity.py'
spec = importlib.util.spec_from_file_location('profile_activity', SOURCE)
activity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activity)


def pr(repo, number, title='A bounded fix'):
    return {'number': number, 'title': title, 'url': f'https://github.com/{repo}/pull/{number}',
            'repository': {'nameWithOwner': repo, 'owner': {'login': repo.split('/')[0]}}}


def fixture():
    return {'user': {'login': 'Pratham2511', 'contributionsCollection': {'contributionCalendar': {
        'totalContributions': 3, 'weeks': [{'contributionDays': [
            {'date': '2026-10-01', 'contributionCount': 1},
            {'date': '2026-10-02', 'contributionCount': 2},
            {'date': '2026-10-03', 'contributionCount': 0}]}]}}},
        'merged': {'issueCount': 3, 'pageInfo': {'hasNextPage': False, 'endCursor': None},
                   'nodes': [pr('team/alpha', 1), pr('team/alpha', 2), pr('other/beta', 3)]},
        'opened': {'issueCount': 1, 'pageInfo': {'hasNextPage': False, 'endCursor': None},
                   'nodes': [pr('other/beta', 4)]}}


def calendar_html():
    """Synthetic profile HTML; order follows GitHub's weekday rows, not dates."""
    cells, tips = [], []
    for n in range(365):
        day = date(2025, 1, 1) + timedelta(days=n)
        count = {0: 1, 364: 2}.get(n, 0)
        cells.append(f'<td class="ContributionCalendar-day" data-date="{day}" id="day-{n}"></td>')
        words = f'{count} contribution' + ('s' if count != 1 else '') if count else 'No contributions'
        tips.append(f'<tool-tip for="day-{n}">{words} on {day:%B} {day.day}th.</tool-tip>')
    heading = '<h2 id="js-contribution-activity-description">3\n contributions\n in the last year</h2>'
    return heading + ''.join(reversed(cells)) + ''.join(tips)


def parse_calendar(document):
    parser = activity.ProfileCalendar()
    parser.feed(document)
    parser.close()
    return parser.calendar()


class ActivityTests(unittest.TestCase):
    def test_visible_calendar_includes_all_shared_counts_and_sorts_weekday_rows(self):
        calendar = parse_calendar(calendar_html())
        days = calendar['weeks'][0]['contributionDays']
        self.assertEqual(calendar['totalContributions'], 3)
        self.assertEqual(days[0], {'date': '2025-01-01', 'contributionCount': 1})
        self.assertEqual(days[-1], {'date': '2025-12-31', 'contributionCount': 2})
        self.assertEqual(sum(day['contributionCount'] for day in days), 3)

    def test_calendar_rejects_missing_changed_duplicate_or_inconsistent_content(self):
        document = calendar_html()
        variants = [document.replace('day-0', 'day-1'),
                    document.replace('2025-01-01', '2025-01-02'),
                    document.replace('data-date="2025-01-01"', 'data-date="2024-12-31"'),
                    document.replace('<tool-tip for="day-0">', '<tool-tip for="missing">'),
                    document.replace('No contributions on January 2th.', 'Count unavailable'),
                    document.replace('3\n contributions', '4\n contributions'),
                    document.replace('in the last year', 'in 2025'),
                    '<html>Sign in to continue</html>']
        for variant in variants:
            with self.subTest(variant=variant[:100]):
                with self.assertRaises(ValueError):
                    parse_calendar(variant)

    def test_public_calendar_request_does_not_send_api_credentials(self):
        response = io.BytesIO(calendar_html().encode())
        with patch.dict(activity.os.environ, {'GH_TOKEN': 'synthetic-test-only'}), patch.object(activity.urllib.request, 'urlopen', return_value=response) as request:
            self.assertEqual(activity.fetch_calendar()['totalContributions'], 3)
        actual = request.call_args.args[0]
        self.assertEqual(actual.full_url, activity.CALENDAR_URL)
        self.assertIsNone(actual.get_header('Authorization'))
        self.assertIsNone(actual.get_header('Cookie'))

    def test_collector_uses_visible_calendar_instead_of_lower_api_count(self):
        data = fixture()
        visible = parse_calendar(calendar_html())
        data['user']['contributionsCollection']['contributionCalendar']['totalContributions'] = 1
        with patch.dict(activity.os.environ, {'GH_TOKEN': 'synthetic-test-only'}), patch.object(activity, 'api', return_value=data), patch.object(activity, 'fetch_calendar', return_value=visible):
            result = activity.summarize(activity.collect(), '2026-01-01T00:00:00Z')
        self.assertEqual(result['contributions'], 3)
        self.assertEqual(result['calendarSource'], activity.CALENDAR_URL)

    def test_streak_and_external_repository_ranking_use_different_denominators(self):
        result = activity.summarize(fixture(), '2026-10-03T12:00:00+00:00')
        self.assertEqual(result['longestStreakInWindowDays'], 2)
        self.assertEqual(result['currentStreakDays'], 2)  # Empty today preserves yesterday.
        self.assertEqual(result['contributions'], 3)
        self.assertEqual(result['mergedAuthoredPRs'], 3)
        self.assertEqual(result['externalRepositoriesWithMergedPRs'], 2)
        self.assertEqual(result['topRepositories'][0], {'repository': 'team/alpha', 'mergedPRs': 2})
        self.assertEqual(result['openPRCount'], 1)

    def test_contribution_today_extends_and_gap_breaks_streak(self):
        data = fixture()
        data['user']['contributionsCollection']['contributionCalendar']['weeks'][0]['contributionDays'][-1]['contributionCount'] = 1
        data['user']['contributionsCollection']['contributionCalendar']['totalContributions'] = 4
        self.assertEqual(activity.summarize(data, '2026-10-03T12:00:00+00:00')['currentStreakDays'], 3)
        data['user']['contributionsCollection']['contributionCalendar']['weeks'][0]['contributionDays'][1]['contributionCount'] = 0
        data['user']['contributionsCollection']['contributionCalendar']['totalContributions'] = 2
        self.assertEqual(activity.summarize(data, '2026-10-03T12:00:00+00:00')['currentStreakDays'], 1)

    def test_longest_streak_survives_a_gap_and_is_limited_to_returned_window(self):
        data = fixture()
        counts = [1, 1, 1, 1, 0, 1, 1, 0]
        calendar = data['user']['contributionsCollection']['contributionCalendar']
        calendar['totalContributions'] = sum(counts)
        calendar['weeks'] = [{'contributionDays': [
            {'date': f'2026-10-{i+1:02}', 'contributionCount': count}
            for i, count in enumerate(counts)]}]
        result = activity.summarize(data, '2026-10-08T12:00:00+00:00')
        self.assertEqual(result['currentStreakDays'], 2)
        self.assertEqual(result['longestStreakInWindowDays'], 4)

    def test_partial_duplicate_owned_and_inconsistent_inputs_fail(self):
        mutations = [lambda d: d['merged']['pageInfo'].update(hasNextPage=True),
                     lambda d: d['merged']['nodes'].__setitem__(1, copy.deepcopy(d['merged']['nodes'][0])),
                     lambda d: d['merged']['nodes'].__setitem__(0, pr('Pratham2511/private-or-owned', 1)),
                     lambda d: d['user']['contributionsCollection']['contributionCalendar'].update(totalContributions=100)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                data = fixture()
                mutate(data)
                with self.assertRaises(ValueError):
                    activity.summarize(data, '2026-10-03T12:00:00+00:00')

    def test_collect_follows_pagination_instead_of_truncating(self):
        data = fixture()
        remainder = data['merged']['nodes'][1:]
        data['merged']['nodes'] = data['merged']['nodes'][:1]
        data['merged']['pageInfo'] = {'hasNextPage': True, 'endCursor': 'next'}
        page = {'result': {'issueCount': 3, 'nodes': remainder, 'pageInfo': {'hasNextPage': False, 'endCursor': 'end'}}}
        calendar = fixture()['user']['contributionsCollection']['contributionCalendar']
        with patch.dict(activity.os.environ, {'GH_TOKEN': 'synthetic-test-only'}), patch.object(activity, 'api', side_effect=[data, page]) as api, patch.object(activity, 'fetch_calendar', return_value=calendar):
            result = activity.collect()
        self.assertEqual(len(result['merged']['nodes']), 3)
        self.assertEqual(api.call_count, 2)

    def test_oversized_search_fails_before_any_assets_are_written(self):
        data = fixture()
        data['merged']['issueCount'] = 1001
        with patch.dict(activity.os.environ, {'GH_TOKEN': 'synthetic-test-only'}), patch.object(activity, 'api', return_value=data), patch.object(activity, 'write_assets') as writer:
            with self.assertRaises(RuntimeError):
                activity.collect()
            writer.assert_not_called()

    def test_api_retries_one_temporary_server_error(self):
        response = io.StringIO(json.dumps({'data': {'complete': True}}))
        error = urllib.error.HTTPError('https://api.github.com/graphql', 502, 'temporary', {}, None)
        with patch.object(activity.urllib.request, 'urlopen', side_effect=[error, response]) as request, patch.object(activity.time, 'sleep'):
            self.assertEqual(activity.api('query{}', {}, 'synthetic-test-only'), {'complete': True})
            self.assertEqual(request.call_count, 2)

    def test_api_does_not_retry_permission_failures_or_echo_credentials(self):
        error = urllib.error.HTTPError('https://api.github.com/graphql', 403, 'permission', {}, None)
        with patch.object(activity.urllib.request, 'urlopen', side_effect=error) as request:
            with self.assertRaises(RuntimeError) as failure:
                activity.api('query{}', {}, 'synthetic-test-only')
            self.assertNotIn('synthetic-test-only', str(failure.exception))
            self.assertEqual(request.call_count, 1)

    def test_activity_assets_omit_calendar_and_open_details_and_have_no_external_resources(self):
        data = fixture()
        data['opened']['nodes'][0]['title'] = '<script>alert("synthetic")</script>'
        result = activity.summarize(data, '2026-10-03T12:00:00+00:00')
        with tempfile.TemporaryDirectory() as temp:
            activity.write_assets(result, Path(temp))
            self.assertEqual(len(list(Path(temp).glob('*.svg'))), 4)
            for path in Path(temp).glob('*.svg'):
                root = ET.fromstring(path.read_text())
                self.assertNotIn('<script>', path.read_text())
                self.assertFalse(any(e.tag.split('}')[-1] in ['image', 'script', 'foreignObject'] for e in root.iter()))
                self.assertFalse(any(e.tag.split('}')[-1] == 'rect' and e.attrib.get('x') == '0' and e.attrib.get('y') == '0' for e in root.iter()))
                self.assertNotIn('A bounded fix', path.read_text())
                self.assertNotIn('Contribution calendar · past year', path.read_text())
                self.assertIn('Authored merged PRs', path.read_text())
                self.assertIn('Longest streak', path.read_text())
                self.assertIn('1 open PRs', path.read_text())


if __name__ == '__main__':
    unittest.main()
