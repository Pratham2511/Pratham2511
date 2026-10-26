"""Regression checks for truthful aggregation, pagination and safe SVG output."""
import copy
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


class ActivityTests(unittest.TestCase):
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
        with patch.dict(activity.os.environ, {'GH_TOKEN': 'synthetic-test-only'}), patch.object(activity, 'api', side_effect=[data, page]) as api:
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
