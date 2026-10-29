"""Owner-merged PR authority for automatic publication; no independent-review claim."""
import hashlib
import json
import re


def owner_merge_evidence(event, repository, revision, tree, candidates, api):
    def check_run(run):
        if not (type(run.get('id')) is int and run['id'] > 0
                and run.get('head_sha') == revision and run.get('head_branch') == 'main'
                and run.get('event') == 'push' and run.get('status') == 'completed'
                and run.get('conclusion') == 'success'
                and run.get('path') == '.github/workflows/ci.yml'
                and run.get('head_repository', {}).get('full_name') == repository):
            raise ValueError('untrusted main CI')
    run = event['workflow_run']
    check_run(run)
    check_run(api('actions/runs/' + str(run['id'])))
    matches = []
    for candidate in candidates:
        number = candidate['number']
        if type(number) is not int or number < 1:
            raise ValueError('invalid PR number')
        pr = api('pulls/' + str(number))
        if (pr.get('merged') is True and pr.get('merge_commit_sha') == revision
                and pr.get('base', {}).get('ref') == 'main'
                and pr.get('base', {}).get('repo', {}).get('full_name') == repository
                and pr.get('merged_by', {}).get('login') == repository.split('/')[0]):
            head = pr['head']['sha']
            if not re.fullmatch('[0-9a-f]{40}', head):
                raise ValueError('invalid head')
            commit = api('git/commits/' + head)
            if commit['sha'] == head and commit['tree']['sha'] == tree:
                matches.append({'pr': number, 'head': head, 'tree': tree,
                                'merged_by': pr['merged_by']['login'], 'ci_run_id': run['id'],
                                'trust': 'owner_merge_and_successful_main_ci'})
    if len(matches) != 1:
        raise ValueError('exact owner-merged PR missing or ambiguous')
    evidence = matches[0]
    evidence['receipt_sha256'] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return evidence


def public_receipt(publication, repository, revision, run_id, attempt):
    """Publish only environment-neutral identity, never raw build/registry receipts."""
    p = publication
    a = p['review_attestation']
    if not (all(p.get(k) is True for k in ('success', 'readback_verified', 'logout_verified',
                                          'auth_directory_removed'))
            and p.get('push_state') == 'completed_readback_verified'
            and p.get('repository') == repository and p.get('revision') == revision
            and p.get('platform') == 'linux/amd64'
            and a.get('run_id') == run_id and a.get('run_attempt') == attempt
            and a.get('trust') == 'owner_merge_and_successful_main_ci'
            and re.fullmatch('[0-9a-f]{40}', revision)
            and re.fullmatch('[0-9a-f]{40}', p['tree'])
            and re.fullmatch(re.escape('ghcr.io/' + repository.lower() + '@sha256:') + '[0-9a-f]{64}',
                             p['immutable_reference'])):
        raise ValueError('publication not qualified for automatic deployment')
    return {'schema': 1, 'repository': repository, 'source_commit': revision,
            'source_tree': p['tree'], 'platform': p['platform'], 'image': p['immutable_reference'],
            'run_id': int(run_id), 'run_attempt': int(attempt), 'pr': a['pr'],
            'policy': a['trust']}


if __name__ == '__main__':
    import os
    import sys
    from pathlib import Path
    result = public_receipt(json.loads(Path(sys.argv[1]).read_text()), os.environ['GITHUB_REPOSITORY'],
                            os.environ['SOURCE_SHA'], os.environ['GITHUB_RUN_ID'],
                            os.environ['GITHUB_RUN_ATTEMPT'])
    Path(sys.argv[2]).write_text(json.dumps(result, sort_keys=True) + '\n')
