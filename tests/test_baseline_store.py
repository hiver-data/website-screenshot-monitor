import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import baseline_store as store
import monitor
from test_monitor import OfflineClient, entry, png, GOOD


class BaselineStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        store.git('init', '--bare', str(self.remote))
        self.output = self.root / 'monitor-data'

    def capture(self, stamp, client=None):
        return monitor.run([entry()], 1, .1, self.output, client or OfflineClient(), stamp=stamp)

    def test_baseline_survives_eight_day_gap_and_deleted_runner(self):
        self.capture('2026-10-10/first')
        store.transfer('save', self.output, str(self.remote))
        shutil.rmtree(self.output)  # Entire ephemeral runner/cache lost.
        store.transfer('restore', self.output, str(self.remote))
        rows, _ = self.capture('2026-10-18/second')
        self.assertEqual(rows[0]['outcome'], 'unchanged')
        self.assertEqual(rows[0]['pixelDiffPercent'], 0)
        rows, _ = self.capture('2026-10-26/third', OfflineClient(image=png('black')))
        self.assertEqual(rows[0]['pixelDiffPercent'], 100)
        self.assertNotEqual(rows[0]['message'], 'New baseline')

    def test_failed_url_preserves_good_baseline_and_compacts_tree(self):
        self.capture('2026-10-10/first')
        self.capture('2026-10-18/second')
        before = (self.output / 'state.json').read_bytes()
        self.capture('2026-10-26/failed', OfflineClient([dict(GOOD, status='failed')]))
        store.transfer('save', self.output, str(self.remote))
        files = store.git('--git-dir', str(self.remote), 'ls-tree', '-r', '--name-only', store.BRANCH).stdout.splitlines()
        self.assertEqual(len(files), 2)
        self.assertTrue(any('/second/' in f for f in files))
        shutil.rmtree(self.output)
        store.transfer('restore', self.output, str(self.remote))
        self.assertEqual((self.output / 'state.json').read_bytes(), before)

    def test_missing_branch_bootstraps_and_unchanged_save_no_commit(self):
        store.transfer('restore', self.output, str(self.remote))
        self.assertFalse(self.output.exists())
        self.capture('2026-10-10/first')
        store.transfer('save', self.output, str(self.remote))
        sha = store.git('--git-dir', str(self.remote), 'rev-parse', store.BRANCH).stdout
        store.transfer('save', self.output, str(self.remote))
        self.assertEqual(store.git('--git-dir', str(self.remote), 'rev-parse', store.BRANCH).stdout, sha)

    def test_remote_error_is_not_fresh_baseline(self):
        with self.assertRaises(RuntimeError):
            store.transfer('restore', self.output, str(self.root / 'missing.git'))
        self.assertFalse(self.output.exists())

    def test_missing_or_unsafe_image_never_pushed(self):
        self.output.mkdir()
        for path in ('../secret.png', '/secret.png', 'screenshots/missing.png'):
            (self.output / 'state.json').write_text(json.dumps({'x': {'image': path}}))
            with self.subTest(path=path), self.assertRaises(ValueError):
                store.transfer('save', self.output, str(self.remote))
        self.assertEqual(store.git('ls-remote', '--heads', str(self.remote)).stdout, '')

    def test_workflow_restores_before_capture_and_guards_save(self):
        text = (Path(__file__).parents[1] / 'workflow-template/website-screenshots.yml').read_text()
        self.assertNotIn('actions/cache', text)
        self.assertIn('contents: write', text)
        self.assertIn("steps.restore.outcome == 'success'", text)
        self.assertLess(text.index('python baseline_store.py restore'), text.index('run: python monitor.py'))
        self.assertIn('python baseline_store.py save', text)

    def test_checkout_auth_config_included_for_temp_repo(self):
        checkout = self.root / 'checkout'
        store.git('init', str(checkout))
        store.git('remote', 'add', 'origin', str(self.remote), cwd=checkout)
        self.capture('2026-10-10/first')
        original = store.git
        calls = []
        def invoke(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)
        cwd = Path.cwd()
        try:
            os.chdir(checkout)
            with patch.object(store, 'git', side_effect=invoke):
                store.transfer('save', self.output)
        finally:
            os.chdir(cwd)
        self.assertIn(('config', 'include.path', str(checkout / '.git/config')), calls)
