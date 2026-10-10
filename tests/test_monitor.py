import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from PIL import Image
import monitor

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/bench-0.1.10.json').read_text())
GOOD = FIXTURE['samples'][0]['row']
DEAD = next(s['row'] for s in FIXTURE['samples'] if s['row']['status'] == 'failed')
CAT = next(s['row'] for s in FIXTURE['samples'] if s['case'] == 'full-page-traps')


def png(color='white', size=(10, 10)):
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, 'PNG')
    return out.getvalue()


def entry(url=GOOD['url'], **kw):
    return dict(url=url, fullPage=True, device='desktop', format='png', **kw)


class OfflineClient:
    def __init__(self, rows=None, image=None, status='SUCCEEDED'):
        self.rows = [copy.deepcopy(GOOD)] if rows is None else rows
        self.image = image or png()
        self.status = status
        self.calls = []

    def capture(self, entries, cap):
        self.calls.append((entries, cap))
        return self.rows, self.status

    def download(self, url):
        return self.image


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.count = 0

    def run_capture(self, client=None, entries=None, threshold=1):
        self.count += 1
        return monitor.run(entries or [entry()], threshold, .1, self.root,
                           client or OfflineClient(), stamp=f'2026-10-10/test-{self.count}')

    def test_first_capture_is_baseline(self):
        rows, text = self.run_capture()
        self.assertEqual(rows[0]['outcome'], 'changed')
        self.assertIsNone(rows[0]['pixelDiffPercent'])
        self.assertIn('New baseline', text)
        self.assertTrue((self.root / rows[0]['image']).exists())

    def test_identical_is_unchanged(self):
        self.run_capture()
        rows, _ = self.run_capture()
        self.assertEqual(rows[0]['outcome'], 'unchanged')
        self.assertEqual(rows[0]['pixelDiffPercent'], 0)

    def test_changed_pixels_flagged(self):
        self.run_capture()
        rows, _ = self.run_capture(OfflineClient(image=png('black')))
        self.assertEqual(rows[0]['outcome'], 'changed')
        self.assertEqual(rows[0]['pixelDiffPercent'], 100)

    def test_threshold_is_pixel_percentage(self):
        self.run_capture()
        out = io.BytesIO()
        img = Image.new('RGB', (10, 10), 'white')
        img.putpixel((0, 0), (0, 0, 0))
        img.save(out, 'PNG')
        rows, _ = self.run_capture(OfflineClient(image=out.getvalue()), threshold=1)
        self.assertEqual(rows[0]['pixelDiffPercent'], 1)
        self.assertEqual(rows[0]['outcome'], 'unchanged')

    def test_height_change_even_identical_pixels(self):
        self.run_capture()
        row = dict(GOOD, pageHeight=GOOD['pageHeight'] + 1)
        rows, _ = self.run_capture(OfflineClient([row]))
        self.assertEqual(rows[0]['outcome'], 'changed')
        self.assertTrue(rows[0]['pageHeightChanged'])

    def test_dimension_change_not_resized_away(self):
        self.run_capture()
        rows, _ = self.run_capture(OfflineClient(image=png(size=(10, 11))))
        self.assertEqual(rows[0]['outcome'], 'changed')
        self.assertEqual(rows[0]['pixelDiffPercent'], 100)

    def test_real_dead_url_not_dropped(self):
        rows, text = self.run_capture(OfflineClient([DEAD]), [entry(DEAD['url'])])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['outcome'], 'failed')
        self.assertIsNone(rows[0]['httpStatus'])
        self.assertIn(DEAD['url'], text)

    def test_403_is_failure_even_when_image_exists(self):
        # Fault injection into a real bench row, NOT a measured 403 sample.
        row = dict(GOOD, status='http_error', httpStatus=403)
        client = OfflineClient([row])
        client.download = Mock(side_effect=AssertionError('Must not compare error page'))
        rows, text = self.run_capture(client)
        self.assertEqual(rows[0]['outcome'], 'failed')
        self.assertIn('403', text)
        client.download.assert_not_called()

    def test_failed_capture_does_not_advance_baseline(self):
        self.run_capture()
        before = (self.root / 'state.json').read_text()
        self.run_capture(OfflineClient([dict(GOOD, status='failed')]))
        self.assertEqual((self.root / 'state.json').read_text(), before)
        rows, _ = self.run_capture()
        self.assertEqual(rows[0]['outcome'], 'unchanged')

    def test_missing_dataset_row_reported(self):
        rows, text = self.run_capture(OfflineClient([], status='TIMED-OUT'))
        self.assertEqual(rows[0]['outcome'], 'failed')
        self.assertIn('TIMED-OUT', text)

    def test_api_failure_reported_for_every_url(self):
        client = OfflineClient()
        client.capture = Mock(side_effect=RuntimeError('SECRET_TOKEN'))
        rows, text = self.run_capture(client, [entry(), entry(DEAD['url'])])
        self.assertEqual([r['outcome'] for r in rows], ['failed', 'failed'])
        self.assertNotIn('SECRET_TOKEN', text)

    def test_partial_failed_run_keeps_good_and_missing_rows(self):
        rows, _ = self.run_capture(OfflineClient(status='FAILED'), [entry(), entry(DEAD['url'])])
        self.assertEqual([r['outcome'] for r in rows], ['changed', 'failed'])

    def test_missing_image_url_is_failed(self):
        rows, _ = self.run_capture(OfflineClient([dict(GOOD, imageUrl=None)]))
        self.assertEqual(rows[0]['outcome'], 'failed')

    def test_invalid_image_is_failed(self):
        rows, _ = self.run_capture(OfflineClient(image=b'not an image'))
        self.assertEqual(rows[0]['outcome'], 'failed')
        self.assertEqual(json.loads((self.root / 'state.json').read_text()), {})

    def test_truncation_and_pending_fields_preserved(self):
        rows, text = self.run_capture(OfflineClient([CAT]), [entry(CAT['url'])])
        self.assertEqual(rows[0]['truncatedAt'], 16000)
        self.assertEqual(rows[0]['imagesPending'], 0)
        self.assertEqual(rows[0]['pageHeight'], CAT['pageHeight'])
        self.assertIn('16000', text)

    def test_variants_have_separate_baselines_and_runs(self):
        a = entry()
        b = dict(a, device='mobile')
        client = OfflineClient()
        rows, _ = self.run_capture(client, [a, b])
        self.assertEqual(len(client.calls), 2)
        self.assertNotEqual(rows[0]['image'], rows[1]['image'])
        self.assertEqual(len(json.loads((self.root / 'state.json').read_text())), 2)

    def test_matching_configuration_batched(self):
        client = OfflineClient([GOOD, DEAD])
        self.run_capture(client, [entry(), entry(DEAD['url'])])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(client.calls[0][0]), 2)

    def test_artifacts_do_not_store_signed_urls(self):
        self.run_capture()
        for path in self.root.rglob('*.json'):
            self.assertNotIn('imageUrl', path.read_text())
            self.assertNotIn('images.example.test', path.read_text())

    def test_config_example_valid(self):
        entries, threshold, cap = monitor.load_config(Path(__file__).parents[1] / 'urls.yaml')
        self.assertEqual(len(entries), 1)
        self.assertEqual(threshold, 1)
        self.assertEqual(cap, .1)

    def test_reject_pdf_and_bad_config(self):
        for extra in ('format: pdf', 'device: custom', 'fullPage: yesplease', 'url: ftp://example.com'):
            path = self.root / 'bad.yaml'
            path.write_text('urls:\n  - url: https://example.com\n    ' + extra + '\n')
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                monitor.load_config(path)

    def test_slack_webhook_payload(self):
        session = Mock()
        monitor.notify('digest', 'https://hooks.example.test/slack', 'slack', session)
        self.assertEqual(session.post.call_args.kwargs['json'], {'text': 'digest'})

    def test_discord_no_mentions_and_chunk_bound(self):
        session = Mock()
        monitor.notify('x' * 4000, 'https://hooks.example.test/discord', 'discord', session)
        self.assertEqual(session.post.call_count, 3)
        for call in session.post.call_args_list:
            payload = call.kwargs['json']
            self.assertLessEqual(len(payload['content']), 1800)
            self.assertEqual(payload['allowed_mentions'], {'parse': []})

    def test_mock_api_real_rows_and_payload(self):
        session = Mock()
        responses = []
        for body in ({'data': {'id': 'mock-run', 'status': 'RUNNING'}},
                     {'data': {'id': 'mock-run', 'status': 'SUCCEEDED', 'defaultDatasetId': 'mock-dataset'}},
                     [GOOD, DEAD]):
            response = Mock()
            response.json.return_value = body
            responses.append(response)
        session.post.return_value = responses[0]
        session.get.side_effect = responses[1:]
        client = monitor.Apify('not-a-real-token', session)
        client.download = Mock(return_value=png())
        rows, _ = self.run_capture(client, [entry(), entry(DEAD['url'])])
        self.assertEqual([r['outcome'] for r in rows], ['changed', 'failed'])
        args = session.post.call_args
        self.assertEqual(args.kwargs['json']['urls'], [GOOD['url'], DEAD['url']])
        self.assertEqual(args.kwargs['params']['maxTotalChargeUsd'], .1)
        self.assertEqual(session.get.call_args.kwargs['params']['clean'], 'false')

    def test_cli_failure_exit_and_saved_report(self):
        config = Path(__file__).parents[1] / 'urls.yaml'
        with patch.dict(os.environ, {'APIFY_TOKEN': 'offline', 'SCREENSHOT_WEBHOOK_URL': ''}), \
             patch('sys.argv', ['monitor.py', '--config', str(config), '--output', str(self.root)]), \
             patch('monitor.Apify', return_value=OfflineClient([dict(GOOD, httpStatus=403, status='http_error')])), \
             patch('builtins.print'):
            self.assertEqual(monitor.main(), 1)
        self.assertIn('403', (self.root / 'report.md').read_text())

    def test_cli_unchanged_exit_zero(self):
        self.run_capture()
        config = Path(__file__).parents[1] / 'urls.yaml'
        with patch.dict(os.environ, {'APIFY_TOKEN': 'offline', 'SCREENSHOT_WEBHOOK_URL': ''}), \
             patch('sys.argv', ['monitor.py', '--config', str(config), '--output', str(self.root)]), \
             patch('monitor.Apify', return_value=OfflineClient()), patch('builtins.print'):
            self.assertEqual(monitor.main(), 0)
        self.assertIn('unchanged', (self.root / 'report.md').read_text())

    def test_cli_webhook_failure_preserves_artifacts(self):
        config = Path(__file__).parents[1] / 'urls.yaml'
        with patch.dict(os.environ, {'APIFY_TOKEN': 'offline', 'SCREENSHOT_WEBHOOK_URL': 'https://mock.test'}), \
             patch('sys.argv', ['monitor.py', '--config', str(config), '--output', str(self.root)]), \
             patch('monitor.Apify', return_value=OfflineClient()), \
             patch('monitor.notify', side_effect=RuntimeError('secret-url')), patch('builtins.print'):
            self.assertEqual(monitor.main(), 2)
        self.assertTrue((self.root / 'state.json').exists())
        self.assertTrue((self.root / 'report.md').exists())

    def test_bounded_wait(self):
        session = Mock()
        session.post.return_value.json.return_value = {'data': {'id': 'mock', 'status': 'RUNNING'}}
        with self.assertRaises(TimeoutError):
            monitor.Apify('offline', session, deadline_seconds=-1).capture([entry()], .1)
        session.get.assert_not_called()

    def test_jpeg_capture_supported(self):
        out = io.BytesIO()
        Image.new('RGB', (10, 10), 'white').save(out, 'JPEG')
        e = dict(entry(), format='jpeg')
        row = dict(GOOD, format='jpeg')
        self.run_capture(OfflineClient([row], out.getvalue()), [e])
        results, _ = self.run_capture(OfflineClient([row], out.getvalue()), [e])
        self.assertEqual(results[0]['outcome'], 'unchanged')
        self.assertTrue(results[0]['image'].endswith('.jpg'))

    def test_dataset_pagination(self):
        session = Mock()
        session.post.return_value.json.return_value = {'data': {'id': 'mock', 'status': 'SUCCEEDED', 'defaultDatasetId': 'dataset'}}
        first, second = Mock(), Mock()
        first.json.return_value = [GOOD] * 1000
        second.json.return_value = [DEAD]
        session.get.side_effect = [first, second]
        rows, status = monitor.Apify('offline', session).capture([entry()], .1)
        self.assertEqual(len(rows), 1001)
        self.assertEqual(session.get.call_args.kwargs['params']['offset'], 1000)

    def test_start_failure_is_not_retried(self):
        session = Mock()
        session.post.side_effect = TimeoutError('ambiguous')
        with self.assertRaises(TimeoutError):
            monitor.Apify('test', session).capture([entry()], .1)
        self.assertEqual(session.post.call_count, 1)

    def test_download_does_not_send_auth_header(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.iter_content.return_value = [png()]
        monitor.Apify('test', session).download('https://images.example.test/a.png')
        self.assertNotIn('headers', session.get.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
