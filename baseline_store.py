#!/usr/bin/env python3
"""Keep only baseline state and referenced images on a dedicated Git branch."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

BRANCH = 'screenshot-state'


def git(*args, cwd=None, check=True):
    return subprocess.run(['git', *args], cwd=cwd, check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def image_paths(root):
    state = json.loads((root / 'state.json').read_text())
    paths = []
    for value in state.values():
        path = Path(value['image'])
        if path.is_absolute() or '..' in path.parts or path.parts[0] != 'screenshots':
            raise ValueError('Unsafe baseline image path')
        source = root / path
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root.resolve()):
            raise ValueError('Missing or unsafe baseline image')
        paths.append(path)
    return paths


def transfer(mode, output, remote=None):
    output = Path(output)
    config = None
    if remote is None:
        config = str(Path(git('rev-parse', '--git-path', 'config').stdout.strip()).resolve())
        remote = git('remote', 'get-url', 'origin').stdout.strip()
    # Fail on access/network errors; only an absent branch establishes a new baseline.
    probe = git('ls-remote', '--exit-code', '--heads', remote, BRANCH, check=False)
    if probe.returncode not in (0, 2):
        raise RuntimeError('Unable to read baseline branch')
    with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as folder:
        repo = Path(folder)
        git('init', '-b', BRANCH, cwd=repo)
        git('remote', 'add', 'origin', remote, cwd=repo)
        if config:
            # Reuse checkout's scoped HTTP authentication without copying secrets.
            git('config', 'include.path', config, cwd=repo)
        if probe.returncode == 0:
            git('fetch', '--depth=1', 'origin', 'refs/heads/' + BRANCH, cwd=repo)
            git('checkout', '-B', BRANCH, 'FETCH_HEAD', cwd=repo)
        baseline = repo / 'baseline'
        if mode == 'restore':
            if probe.returncode == 2:
                print('No saved baseline yet; first successful captures establish it.')
                return
            paths = image_paths(baseline)
            output.mkdir(parents=True, exist_ok=True)
            for path in paths:
                target = output / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(baseline / path, target)
            shutil.copyfile(baseline / 'state.json', output / 'state.json')
            print('Restored baseline from ' + BRANCH)
            return
        if not (output / 'state.json').exists():
            print('No state produced; existing branch left untouched.')
            return
        paths = image_paths(output)
        if baseline.exists():
            shutil.rmtree(baseline)
        baseline.mkdir()
        shutil.copyfile(output / 'state.json', baseline / 'state.json')
        for path in paths:
            target = baseline / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output / path, target)
        git('add', '-A', cwd=repo)
        if git('diff', '--cached', '--quiet', cwd=repo, check=False).returncode == 0:
            print('Baseline unchanged.')
            return
        git('-c', 'user.name=github-actions[bot]', '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'commit', '-m', 'Save screenshot baselines\n\nCo-Authored-By: Paperclip <noreply@paperclip.ing>', cwd=repo)
        # No force push: concurrent external changes fail visibly rather than being lost.
        git('push', 'origin', 'HEAD:refs/heads/' + BRANCH, cwd=repo)
        print('Saved baseline to ' + BRANCH)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['restore', 'save'])
    parser.add_argument('--output', default='monitor-data')
    args = parser.parse_args()
    transfer(args.mode, args.output)
