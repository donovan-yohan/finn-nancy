"""Generated GitHub event/PR boundaries, no network or credentials."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AutomaticReleaseTests(unittest.TestCase):
    def test_public_receipt_is_allowlisted_and_requires_verified_publication(self):
        path = ROOT / 'scripts/release_auto.py'
        spec = importlib.util.spec_from_file_location('release_auto', path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, 'public_receipt'), 'public receipt projection missing')
        source = {'success': True, 'readback_verified': True, 'logout_verified': True,
                  'auth_directory_removed': True, 'push_state': 'completed_readback_verified',
                  'repository': 'example/finance', 'revision': 'a' * 40, 'tree': 'b' * 40,
                  'platform': 'linux/amd64',
                  'immutable_reference': 'ghcr.io/example/finance@sha256:' + 'd' * 64,
                  'review_attestation': {'run_id': '123', 'run_attempt': '1', 'pr': 7,
                                         'trust': 'owner_merge_and_successful_main_ci'},
                  'private_unrelated': 'must not be published'}
        result = module.public_receipt(source, 'example/finance', 'a' * 40, '123', '1')
        self.assertNotIn('private_unrelated', result)
        self.assertEqual(result['image'], source['immutable_reference'])
        for key in ('success', 'readback_verified', 'logout_verified', 'auth_directory_removed'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                module.public_receipt(dict(source, **{key: False}), 'example/finance', 'a' * 40, '123', '1')

    def test_announcement_executes_with_the_actual_download_asset_name(self):
        import json
        import os
        import re
        import subprocess
        import tempfile
        workflow = (ROOT/'.github/workflows/release-image.yml').read_text()
        body = re.findall(r'^        run: \|\n((?:          [^\n]*\n)+)', workflow, re.MULTILINE)[-1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'receipts').mkdir(); (root/'bin').mkdir()
            publication = {'success': True, 'readback_verified': True, 'logout_verified': True,
                           'auth_directory_removed': True, 'push_state': 'completed_readback_verified',
                           'repository': 'example/finance', 'revision': 'a'*40, 'tree': 'b'*40,
                           'platform': 'linux/amd64',
                           'immutable_reference': 'ghcr.io/example/finance@sha256:'+'d'*64,
                           'review_attestation': {'run_id': '123', 'run_attempt': '1', 'pr': 7,
                                                  'trust': 'owner_merge_and_successful_main_ci'}}
            (root/'receipts/publication.json').write_text(json.dumps(publication))
            shim = root/'bin/gh'
            shim.write_text('#!/usr/bin/env python3\nimport sys,pathlib,json\na=sys.argv[1:]\nassert a[:2]==["release","create"]\np=pathlib.Path(a[-1])\nassert p.name=="release.json" and p.is_file()\nd=json.loads(p.read_text())\nassert d["source_commit"]=="a"*40\nprint("generated announcement accepted")\n')
            shim.chmod(0o700)
            env = dict(os.environ, PATH=str(root/'bin')+os.pathsep+os.environ['PATH'],
                       RUNNER_TEMP=str(root), GITHUB_REPOSITORY='example/finance', SOURCE_SHA='a'*40,
                       GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1')
            shell = 'set -eu\n'+'\n'.join(line[10:] for line in body.splitlines())
            result = subprocess.run(['bash','-c',shell],cwd=ROOT,env=env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('generated announcement accepted',result.stdout)

    def test_successful_main_ci_is_bound_to_owner_merged_pr(self):
        path = ROOT / 'scripts/release_auto.py'
        self.assertTrue(path.exists(), 'automatic release gate is not implemented')
        spec = importlib.util.spec_from_file_location('release_auto', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sha, tree, head = 'a' * 40, 'b' * 40, 'c' * 40
        run = {'id': 123, 'head_sha': sha, 'head_branch': 'main', 'event': 'push',
               'status': 'completed', 'conclusion': 'success', 'path': '.github/workflows/ci.yml',
               'head_repository': {'full_name': 'example/finance'}}
        pr = {'number': 7, 'merged': True, 'merge_commit_sha': sha,
              'base': {'ref': 'main', 'repo': {'full_name': 'example/finance'}},
              'head': {'sha': head}, 'merged_by': {'login': 'example'}}
        calls = []
        def api(endpoint):
            calls.append(endpoint)
            return {'actions/runs/123': run, 'pulls/7': pr,
                    'git/commits/' + head: {'sha': head, 'tree': {'sha': tree}}}[endpoint]
        result = module.owner_merge_evidence({'workflow_run': run}, 'example/finance', sha, tree,
                                             [{'number': 7}], api)
        self.assertEqual(result['head'], head)
        self.assertEqual(result['tree'], tree)
        self.assertEqual(result['trust'], 'owner_merge_and_successful_main_ci')
        self.assertIn('actions/runs/123', calls)
        for field, bad in [('event', 'pull_request'), ('conclusion', 'failure'),
                           ('head_branch', 'feature'), ('path', '.github/workflows/untrusted.yml')]:
            altered = dict(run, **{field: bad})
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.owner_merge_evidence({'workflow_run': altered}, 'example/finance', sha, tree,
                                             [{'number': 7}], api)
        pr['merged_by']['login'] = 'someone-else'
        with self.assertRaises(ValueError):
            module.owner_merge_evidence({'workflow_run': run}, 'example/finance', sha, tree,
                                         [{'number': 7}], api)


if __name__ == '__main__':
    unittest.main()
