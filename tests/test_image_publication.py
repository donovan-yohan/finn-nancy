"""Run the release entrypoint with real Git/build-image and generated API/Docker boundaries."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE_ID = "sha256:" + "1" * 64
DIGEST = "sha256:" + "2" * 64


@pytest.fixture
def publication(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-qb", "main")
    for name in ("Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "LICENSE", "NOTICE",
                 "THIRD_PARTY_NOTICES.md", "app/config.py", "migrations/001_tables.sql",
                 "migrations/002_views.sql", "deploy/container_entrypoint.py"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated build input\n")
    (repo / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/build-image", repo / "scripts/build-image")
    shutil.copy2(ROOT / "scripts/release-image", repo / "scripts/release-image")
    # The release orchestrator must call this boundary by image ID, not a mutable tag.
    (repo / "scripts/smoke-image").write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
state = json.loads(pathlib.Path(os.environ['FAKE_STATE']).read_text())
with open(os.environ['FAKE_EVENTS'], 'a') as f:
    f.write(json.dumps({'command': 'smoke', 'args': args, 'has_token': any(k in os.environ for k in ('GH_TOKEN', 'GHCR_TOKEN'))}) + '\\n')
out = pathlib.Path(args[args.index('--artifacts') + 1]); out.mkdir()
r = {'success': not state.get('smoke_failure'), 'cleanup_verified': True,
     'image_id': args[args.index('--image') + 1], 'os': 'linux', 'architecture': 'amd64',
     'labels': {'org.opencontainers.image.revision': state['sha'], 'io.finn-nancy.source-tree': state['tree']}}
(out / 'receipt.json').write_text(json.dumps(r))
sys.exit(1 if state.get('smoke_failure') else 0)
''')
    (repo / "scripts/smoke-image").chmod(0o755)
    git("add", "--", ".")  # Only this disposable generated repository.
    git("-c", "user.name=Generated Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "generated")
    sha = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    git("update-ref", "refs/remotes/origin/main", sha)
    values = {"sha": sha, "tree": tree, "image_id": IMAGE_ID, "digest": DIGEST,
              "pr_head": "a" * 40}
    state.write_text(json.dumps(values))
    boundary = '''#!/usr/bin/env python3
import io, json, os, pathlib, sys, tarfile
args = sys.argv[1:]; name = pathlib.Path(sys.argv[0]).name
s = json.loads(pathlib.Path(os.environ['FAKE_STATE']).read_text())
e = {'command': name, 'args': args, 'has_token': any(k in os.environ for k in ('GH_TOKEN', 'GHCR_TOKEN')),
     'docker_config': os.environ.get('DOCKER_CONFIG')}
if name == 'gh':
    endpoint = args[-1]
    if '/actions/workflows/ci.yml/runs?' in endpoint:
        result = {'workflow_runs': [{'id': 7, 'run_number': 3, 'run_attempt': 1}]}
        if s.get('older_success'):
            result['workflow_runs'].append({'id': 5, 'run_number': 2, 'run_attempt': 1})
        if s.get('no_ci'): result = {'workflow_runs': []}
    elif endpoint.endswith('/actions/runs/5') or endpoint.endswith('/actions/runs/7'):
        result = {'id': 7, 'run_number': 3, 'run_attempt': 1, 'workflow_id': 6,
                  'head_sha': s.get('ci_sha', s['sha']), 'head_branch': 'main', 'event': s.get('ci_event', 'push'),
                  'status': s.get('ci_status', 'completed'), 'conclusion': s.get('ci_conclusion', 'success'),
                  'path': '.github/workflows/ci.yml', 'head_repository': {'full_name': 'example/finance'}}
        if endpoint.endswith('/actions/runs/5'):
            result.update(id=5, run_number=2, status='completed', conclusion='success')
    elif endpoint.endswith('/actions/workflows/ci.yml'):
        result = {'id': 6, 'path': '.github/workflows/ci.yml', 'state': 'active'}
    elif endpoint.endswith('/git/ref/heads/main'):
        result = {'object': {'sha': s.get('main_sha', s['sha'])}}
    elif endpoint.endswith('/pulls/4/reviews?per_page=100&page=1'):
        result = [] if s.get('unreviewed') else [{'id': 1, 'user': {'login': 'reviewer'}, 'state': 'APPROVED', 'commit_id': s.get('approved_sha', s['pr_head'])}]
        if s.get('dismissed'): result.append({'id': 2, 'user': {'login': 'reviewer'}, 'state': 'DISMISSED', 'commit_id': s['sha']})
        if s.get('changes_requested'): result.append({'id': 3, 'user': {'login': 'reviewer'}, 'state': 'CHANGES_REQUESTED', 'commit_id': s['pr_head']})
    elif endpoint.endswith('/pulls/4'):
        result = {'number': 4, 'merged': True, 'merge_commit_sha': s['sha'], 'base': {'ref': 'main', 'repo': {'full_name': 'example/finance'}},
                  'head': {'sha': s['pr_head']}, 'user': {'login': 'author'}}
    elif endpoint.endswith('/git/commits/' + s['pr_head']):
        result = {'sha': s['pr_head'], 'tree': {'sha': s.get('pr_tree', s['tree'])}}
    elif endpoint.endswith('/pulls?per_page=100&page=1'):
        result = [] if s.get('unmerged') else [{'number': 4}]
    else:
        raise SystemExit('unexpected generated API endpoint: ' + endpoint)
    print(json.dumps(result))
else:
    if args[:2] == ['buildx', 'build']:
        if s.get('build_failure'):
            print('generated build failure reason password=do-not-retain generated-read-token', file=sys.stderr)
            sys.exit(9)
        e['context_files'] = tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())).getnames()
        pathlib.Path(args[args.index('--iidfile') + 1]).write_text(s['image_id'])
    elif args[:2] == ['image', 'inspect']:
        print(json.dumps([{'Id': s.get('readback_id', s['image_id']) if '@' in args[-1] else s['image_id'],
            'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Labels': {
            'org.opencontainers.image.revision': s['sha'], 'io.finn-nancy.source-tree': s['tree']}}}]))
    elif args[0] == 'login':
        e['stdin_matches'] = sys.stdin.read().strip() == 'generated-publication-token'
        pathlib.Path(os.environ['DOCKER_CONFIG'], 'config.json').write_text(json.dumps({'auths': {'ghcr.io': {'auth': 'generated-publication-token'}}}))
        if s.get('login_failure'):
            print('generated-publication-token', file=sys.stderr); sys.exit(1)
    elif args[0] == 'logout':
        if not s.get('sticky_auth'):
            pathlib.Path(os.environ['DOCKER_CONFIG'], 'config.json').write_text('{"auths": {}}')
    elif args[0] == 'push':
        e['pid'] = os.getpid()
        with open(os.environ['FAKE_EVENTS'], 'a') as f: f.write(json.dumps(e) + '\\n')
        if s.get('push_timeout'):
            import time
            admitted_fd = os.environ.get('FAKE_PUSH_ADMITTED_FD')
            if admitted_fd:
                os.write(int(admitted_fd), b'1')
                os.close(int(admitted_fd))
            time.sleep(20)
        if s.get('push_failure'): sys.exit(8)
        print('generated: digest: ' + s['digest'] + ' size: 123', file=sys.stderr if s.get('push_stderr') else sys.stdout)
        if s.get('push_ambiguous'): print('digest: sha256:' + '3' * 64, file=sys.stderr)
        sys.exit(0)
    elif args[:3] == ['buildx', 'imagetools', 'inspect']:
        if '--raw' in args:
            print(json.dumps({'schemaVersion': 2, 'config': {'digest': s.get('manifest_id', s['image_id'])}, 'layers': []}))
        else:
            print('Name: generated\\nDigest: ' + s.get('tag_digest', s['digest']))
    elif args[0] == 'save':
        pathlib.Path(args[args.index('--output') + 1]).write_bytes(b'generated image archive')
    elif args[0] not in ('tag', 'pull', 'logout', 'load'):
        raise SystemExit('unexpected Docker operation')
with open(os.environ['FAKE_EVENTS'], 'a') as f: f.write(json.dumps(e) + '\\n')
'''
    for name in ("gh", "docker"):
        (tools / name).write_text(boundary)
        (tools / name).chmod(0o755)
    env = {"PATH": str(tools) + os.pathsep + os.environ["PATH"], "HOME": str(tmp_path),
           "TMPDIR": str(tmp_path), "FAKE_STATE": str(state), "FAKE_EVENTS": str(events),
           "GITHUB_REPOSITORY": "example/finance", "GITHUB_EVENT_NAME": "workflow_dispatch",
           "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": sha, "RELEASE_WORKFLOW_SHA": sha,
           "RELEASE_WORKFLOW_REF": "example/finance/.github/workflows/release-image.yml@refs/heads/main",
           "GITHUB_ACTOR": "operator", "GH_TOKEN": "generated-read-token",
           "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
           # Synthetic reference only: does not stand in for a real independent review.
           "REVIEW_RECEIPT_SHA256": "b" * 64, "REVIEWED_PR_HEAD": "a" * 40,
           "REVIEWED_PR_TREE": tree}

    def run(phase="qualify", **changes):
        values.update(changes)
        state.write_text(json.dumps(values))
        if phase == "publish":
            env["GHCR_TOKEN"] = "generated-publication-token"
        return subprocess.run([sys.executable, str(ROOT / "scripts/release-image"), phase,
                               "--repository", str(repo), "--revision", sha,
                               "--artifacts", str(tmp_path / "artifacts")],
                              env=env, text=True, capture_output=True)

    def recorded():
        return [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []

    return run, recorded, tmp_path, env, git


def test_local_review_attestation_accepts_squash_tree_without_github_approval(publication):
    run, events, root, env, _ = publication
    result = run(unreviewed=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((root / "artifacts/qualified.json").read_text())
    assert receipt["review_attestation"] == {
        "receipt_sha256": env["REVIEW_RECEIPT_SHA256"], "pr": 4,
        "head": env["REVIEWED_PR_HEAD"], "tree": env["REVIEWED_PR_TREE"],
        "actor": "operator", "run_id": "42", "run_attempt": "1",
        "trust": "authorized_dispatcher_verification",
    }
    assert receipt["revision"] != receipt["review_attestation"]["head"]
    assert not any("/reviews?" in e["args"][-1] for e in events())


def test_review_list_is_not_an_invented_approval_framework(publication):
    # Historical CHANGES_REQUESTED disposition belongs to source review/merge policy.
    # The dispatcher must verify a current independent PASS; this helper cannot prove it.
    run, events, _, _, _ = publication
    assert run(changes_requested=True).returncode == 0
    assert not any("/reviews?" in e["args"][-1] for e in events())


@pytest.mark.parametrize("key,value", [
    ("REVIEW_RECEIPT_SHA256", None), ("REVIEW_RECEIPT_SHA256", "B" * 64),
    ("REVIEW_RECEIPT_SHA256", "b" * 63), ("REVIEW_RECEIPT_SHA256", " " + "b" * 64),
    ("REVIEWED_PR_HEAD", None), ("REVIEWED_PR_HEAD", "main"),
    ("REVIEWED_PR_HEAD", "c" * 40), ("REVIEWED_PR_TREE", None),
    ("REVIEWED_PR_TREE", "C" * 40), ("REVIEWED_PR_TREE", "c" * 40),
])
def test_missing_malformed_or_mismatched_attestation_refuses_before_build(publication, key, value):
    run, events, _, env, _ = publication
    if value is None:
        env.pop(key)
    else:
        env[key] = value
    assert run().returncode == 1
    assert all(e["command"] == "gh" for e in events())
    if value is None or value in ("main", "B" * 64, "b" * 63, " " + "b" * 64, "C" * 40):
        assert events() == []


@pytest.mark.parametrize("replacement", [None, "B" * 64, "c" * 64])
def test_publish_revalidates_and_binds_review_attestation_before_login(publication, replacement):
    run, events, root, env, _ = publication
    assert run().returncode == 0
    if replacement is None:
        env.pop("REVIEW_RECEIPT_SHA256")
    else:
        env["REVIEW_RECEIPT_SHA256"] = replacement
    assert run("publish").returncode == 1
    receipt = json.loads((root / "artifacts/publication.json").read_text())
    assert not receipt["success"] and receipt["push_state"] == "push_not_attempted"
    assert receipt["stage"] == ("artifacts" if replacement == "c" * 64 else "source_gates")
    assert not any(e["args"][0] in ("login", "push") for e in events())


def test_reviewed_head_tree_must_equal_selected_main_tree(publication):
    run, events, _, _, _ = publication
    assert run(pr_tree="c" * 40).returncode == 1
    assert all(e["command"] == "gh" for e in events())


def test_qualifies_exact_source_and_smoked_image_without_publication(publication):
    run, events, root, _, _ = publication
    result = run()
    assert result.returncode == 0, result.stderr
    receipt = json.loads((root / "artifacts/qualified.json").read_text())
    assert receipt["image_id"] == IMAGE_ID
    assert receipt["ci_run"]["id"] == 7
    calls = events()
    assert any(e["command"] == "smoke" and IMAGE_ID in e["args"] for e in calls)
    assert not any(e["args"][0] in ("login", "push") for e in calls)
    assert all(not e["has_token"] for e in calls if e["command"] != "gh")
    build = next(e for e in calls if e["args"][:2] == ["buildx", "build"])
    assert "app/config.py" in build["context_files"]
    assert not any(p.startswith(("scripts/", "data/", ".git/")) for p in build["context_files"])


@pytest.mark.parametrize("changes", [
    {"main_sha": "0" * 40}, {"ci_sha": "0" * 40}, {"ci_event": "pull_request"},
    {"ci_status": "in_progress"}, {"ci_conclusion": "failure"},
    {"unmerged": True}, {"no_ci": True},
    {"older_success": True, "ci_conclusion": "failure"},
])
def test_source_and_exact_ci_refusals_happen_before_build_or_login(publication, changes):
    run, events, root, _, _ = publication
    assert run(**changes).returncode == 1
    assert all(e["command"] == "gh" for e in events())
    receipt = json.loads((root / "artifacts/qualify-phase.json").read_text())
    assert receipt["stage"] == "source_gates" and receipt["success"] is False
    assert receipt["error_code"] and receipt["push_state"] == "push_not_attempted"


@pytest.mark.parametrize("key,value", [
    ("GITHUB_REF", "refs/heads/feature"), ("RELEASE_WORKFLOW_SHA", "0" * 40),
    ("RELEASE_WORKFLOW_REF", "example/finance/.github/workflows/release-image.yml@refs/heads/feature"),
    ("GITHUB_SHA", "0" * 40), ("GITHUB_EVENT_NAME", "push"),
])
def test_untrusted_dispatch_refuses_before_external_commands(publication, key, value):
    run, events, root, env, _ = publication
    env[key] = value
    assert run().returncode == 1
    assert events() == []
    receipt = json.loads((root / "artifacts/qualify-phase.json").read_text())
    assert receipt["success"] is False and receipt["stage"] == "source_gates"
    assert receipt["error_code"] == "untrusted_dispatch"
    assert receipt["push_state"] == "push_not_attempted"


def test_build_diagnostic_redacts_secrets_before_truncation(monkeypatch):
    import runpy

    helper = runpy.run_path(str(ROOT / "scripts/release-image"))
    secret = "generated-secret-" + "x" * 20000
    monkeypatch.setenv("GH_TOKEN", secret)
    text = helper["sanitized"](secret + " password=private bearer abc github_pat_abc https://user.example.invalid:b@example.invalid")
    assert len(text) <= 16384
    assert "x" * 100 not in text and "private" not in text and "bearer abc" not in text
    assert "github_pat_abc" not in text and "user.example.invalid:b@" not in text


@pytest.mark.parametrize("header", [
    "Authorization: Bearer synthetic-header-value",
    "Authorization: Basic c3ludGhldGljLWhlYWRlcg==",
    '"Authorization": "Bearer synthetic-header-value"',
    "authorization=Basic c3ludGhldGljLWhlYWRlcg==",
])
def test_authorization_header_redacts_complete_scheme_credential(header):
    import runpy

    helper = runpy.run_path(str(ROOT / "scripts/release-image"))
    text = helper["sanitized"]("useful diagnostic " + header)
    assert "useful diagnostic" in text and "[REDACTED]" in text
    assert "synthetic-header-value" not in text
    assert "c3ludGhldGljLWhlYWRlcg==" not in text


def test_failed_build_redacts_authorization_headers_in_both_streams(publication):
    run, _, root, _, _ = publication
    tool = root / "bin/docker"
    tool.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "print('useful stdout Authorization: Bearer synthetic-header-value')\n"
        "print('useful stderr Authorization: Basic c3ludGhldGljLWhlYWRlcg==', file=sys.stderr)\n"
        "sys.exit(17)\n"
    )
    result = run()
    assert result.returncode == 1
    phase = json.loads((root / "artifacts/qualify-phase.json").read_text())
    assert phase["stage"] == "build" and phase["error_code"] == "command_failed"
    log = (root / "artifacts/build.log").read_text()
    assert "useful stdout" in log and "useful stderr" in log
    assert "synthetic-header-value" not in log
    assert "c3ludGhldGljLWhlYWRlcg==" not in log


def test_unavailable_boundary_command_has_safe_terminal_identity(publication, monkeypatch, capsys):
    import runpy

    _, _, root, env, _ = publication
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    module = runpy.run_path(str(ROOT / "scripts/release-image"))
    real_popen = subprocess.Popen

    def popen(argv, **options):
        if argv[0] == "gh":
            raise FileNotFoundError("generated unavailable boundary")
        return real_popen(argv, **options)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(sys, "argv", ["release-image", "qualify", "--repository", str(root / "repo"),
                                      "--revision", env["GITHUB_SHA"], "--artifacts", str(root / "artifacts")])
    assert module["main"]() == 1
    receipt = json.loads((root / "artifacts/qualify-phase.json").read_text())
    assert receipt["error_code"] == "command_unavailable"
    assert receipt["command"] == "gh" and receipt["exit_status"] is None
    assert "generated unavailable boundary" not in capsys.readouterr().err


def test_failed_build_retains_sanitized_diagnostics_and_terminal_receipt(publication):
    run, _, root, _, _ = publication
    assert run(build_failure=True).returncode == 1
    phase = json.loads((root / "artifacts/qualify-phase.json").read_text())
    assert phase["success"] is False and phase["stage"] == "build"
    assert phase["error_code"] == "command_failed"
    assert phase["command"] == "build-image" and phase["exit_status"] == 1
    log = (root / "artifacts/build.log").read_text()
    assert "generated build failure reason" in log
    assert "do-not-retain" not in log and "generated-read-token" not in log
    assert len(log) <= 16384
    assert not (root / "artifacts/qualified.json").exists()


@pytest.mark.parametrize("failure", ["source", "archive", "missing_receipt"])
def test_all_prelogin_failures_have_terminal_publication_receipt(publication, failure):
    run, events, root, _, _ = publication
    assert run().returncode == 0
    changes = {}
    if failure == "source":
        changes["main_sha"] = "0" * 40
    elif failure == "archive":
        (root / "artifacts/image.tar").write_bytes(b"changed")
    else:
        (root / "artifacts/qualified.json").unlink()
    assert run("publish", **changes).returncode == 1
    phase = json.loads((root / "artifacts/publish-phase.json").read_text())
    assert phase["success"] is False
    assert phase["stage"] == ("source_gates" if failure == "source" else "artifacts")
    assert phase["error_code"]
    assert phase["push_state"] == "push_not_attempted"
    assert not any(e["args"][0] in ("login", "push") for e in events())


def test_failed_smoke_cannot_create_publishable_receipt(publication):
    run, events, root, _, _ = publication
    assert run(smoke_failure=True).returncode == 1
    assert not (root / "artifacts/qualified.json").exists()
    assert run("publish").returncode == 1
    assert not any(e["args"][0] in ("login", "push") for e in events())


def test_publication_reads_back_immutable_digest_and_cleans_credentials(publication):
    run, events, root, env, _ = publication
    env.update(GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1")
    assert run().returncode == 0
    result = run("publish")
    assert result.returncode == 0, result.stderr
    receipt = json.loads((root / "artifacts/publication.json").read_text())
    assert receipt["success"] and receipt["logout_verified"]
    assert receipt["immutable_reference"] == "ghcr.io/example/finance@" + DIGEST
    assert receipt["image_id"] == IMAGE_ID
    assert ":latest" not in receipt["tag"]
    calls = events()
    assert next(e for e in calls if e["args"][0] == "login")["stdin_matches"]
    assert calls[-1]["args"] == ["logout", "ghcr.io"]
    assert any(e["args"] == ["pull", "--platform", "linux/amd64", receipt["immutable_reference"]] for e in calls)
    assert all(not e["has_token"] for e in calls if e["command"] != "gh")
    for p in (root / "artifacts").rglob("*"):
        if p.is_file():
            assert b"generated-publication-token" not in p.read_bytes()
    assert "generated-publication-token" not in result.stdout + result.stderr
    assert not list(root.glob("fn-registry-auth-*"))
    assert all(not Path(e["docker_config"]).exists() for e in calls if e.get("docker_config"))


@pytest.mark.parametrize("changes", [
    {"tag_digest": "sha256:" + "3" * 64}, {"manifest_id": "sha256:" + "3" * 64},
    {"readback_id": "sha256:" + "3" * 64},
])
def test_push_readback_mismatch_is_not_success_and_always_logs_out(publication, changes):
    run, events, root, env, _ = publication
    env.update(GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1")
    assert run().returncode == 0
    assert run("publish", **changes).returncode == 1
    receipt = json.loads((root / "artifacts/publication.json").read_text())
    assert receipt["push_completed"] and not receipt["success"] and receipt["logout_verified"]
    assert events()[-1]["args"] == ["logout", "ghcr.io"]


def test_generated_stderr_push_digest_is_supported_not_native_docker_proof(publication):
    run, _, root, _, _ = publication
    assert run().returncode == 0
    result = run("publish", push_stderr=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((root / "artifacts/publication.json").read_text())
    assert receipt["digest"] == DIGEST and receipt["readback_verified"]


@pytest.mark.parametrize("change", [{"push_failure": True}, {"push_ambiguous": True}])
def test_push_failure_or_ambiguous_digest_never_becomes_acceptance(publication, change):
    run, events, root, _, _ = publication
    assert run().returncode == 0
    assert run("publish", **change).returncode == 1
    phase = json.loads((root / "artifacts/publication.json").read_text())
    assert not phase["success"] and not phase["readback_verified"]
    assert phase["push_state"] == ("attempted_uncertain" if "push_failure" in change else "completed")
    assert phase["stage"] == "push" and phase["logout_verified"]
    assert len([e for e in events() if e["args"][0] == "push"]) == 1


def test_timed_out_push_is_uncertain_not_replayed_and_logout_is_bounded(publication, monkeypatch):
    import runpy
    import select
    import signal

    run, events, root, env, _ = publication
    assert run().returncode == 0
    state_path = Path(env["FAKE_STATE"])
    state = json.loads(state_path.read_text())
    state["push_timeout"] = True
    state_path.write_text(json.dumps(state))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GHCR_TOKEN", "generated-publication-token")
    module = runpy.run_path(str(ROOT / "scripts/release-image"))
    real_popen = subprocess.Popen
    timeouts = {}
    pushes = []

    def popen(*args, **options):
        command = args[0]
        admission = admitted = None
        if command[:2] == ("docker", "push"):
            admission, admitted = os.pipe()
            options["env"] = {**options["env"], "FAKE_PUSH_ADMITTED_FD": str(admitted)}
            options["pass_fds"] = (admitted,)
        try:
            process = real_popen(*args, **options)
        except BaseException:
            if admission is not None:
                os.close(admission)
            raise
        finally:
            if admitted is not None:
                os.close(admitted)
        if command[:2] in (("docker", "push"), ("docker", "logout")):
            operation = command[1]
            communicate = process.communicate
            if operation == "push":
                pushes.append(process)

            def observed(input=None, timeout=None):
                nonlocal admission
                timeouts.setdefault(operation, timeout)
                if operation == "push" and timeout is not None:
                    assert admission is not None
                    try:
                        # Bound fixture readiness separately: interpreter startup is
                        # not push execution. The fake signals only after closing
                        # its real admission event, then blocks in its timeout path.
                        assert select.select([admission], [], [], 5)[0], "push was not admitted"
                        assert os.read(admission, 1) == b"1", "push exited before admission"
                        assert any(e["args"][0] == "push" and e["pid"] == process.pid for e in events())
                        assert options["start_new_session"] is True
                        assert os.getpgid(process.pid) == process.pid
                    finally:
                        os.close(admission)
                        admission = None
                    return communicate(input, timeout=0.1)
                return communicate(input, timeout=timeout)

            process.communicate = observed
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(sys, "argv", ["release-image", "publish", "--repository", str(root / "repo"),
                                      "--revision", env["GITHUB_SHA"], "--artifacts", str(root / "artifacts")])
    try:
        assert module["main"]() == 1
        receipt = json.loads((root / "artifacts/publication.json").read_text())
        assert receipt["error_code"] == "command_timeout" and receipt["command"] == "docker"
        assert receipt["stage"] == "push" and receipt["push_state"] == "attempted_uncertain"
        assert not receipt["push_completed"] and receipt["logout_verified"] and receipt["auth_directory_removed"]
        assert timeouts == {"push": 300, "logout": 30}
        assert len([e for e in events() if e["args"][0] == "push"]) == 1
        assert len(pushes) == 1 and pushes[0].returncode == -signal.SIGKILL
        assert events()[-1]["args"] == ["logout", "ghcr.io"]
        assert not list(root.glob("fn-registry-auth-*"))
        assert all(not Path(e["docker_config"]).exists() for e in events() if e.get("docker_config"))
    finally:
        # A failed readiness assertion must not leak a generated sleeping child.
        for process in pushes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def test_artifact_tampering_refuses_before_registry_login(publication):
    run, events, root, env, _ = publication
    env.update(GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1")
    assert run().returncode == 0
    (root / "artifacts/image.tar").write_bytes(b"changed")
    assert run("publish").returncode == 1
    assert not any(e["args"][0] in ("login", "push") for e in events())


def test_publish_rechecks_current_main_before_login(publication):
    run, events, _, env, _ = publication
    env.update(GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1")
    assert run().returncode == 0
    assert run("publish", main_sha="0" * 40).returncode == 1
    assert not any(e["args"][0] in ("login", "push") for e in events())


@pytest.mark.parametrize("revision", ["main", "ABCDEF" * 6 + "ABCD", "0" * 39])
def test_source_sha_format_is_not_normalized(publication, revision):
    _, events, root, env, _ = publication
    result = subprocess.run([sys.executable, str(ROOT / "scripts/release-image"), "qualify",
                             "--repository", str(root / "repo"), "--revision", revision,
                             "--artifacts", str(root / "artifacts")], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert events() == []


def test_dirty_selected_source_refuses_before_remote_lookup(publication):
    run, events, root, _, _ = publication
    (root / "repo/app/config.py").write_text("uncommitted\n")
    assert run().returncode == 1
    assert events() == []


def test_origin_main_must_match_even_if_remote_api_matches(publication):
    run, events, _, _, git = publication
    git("update-ref", "-d", "refs/remotes/origin/main")
    assert run().returncode == 1
    assert events() == []


def test_login_failure_preserves_failed_receipt_and_cleans_auth(publication):
    run, events, root, env, _ = publication
    env.update(GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1")
    assert run().returncode == 0
    result = run("publish", login_failure=True)
    assert result.returncode == 1
    receipt = json.loads((root / "artifacts/publication.json").read_text())
    assert not receipt["success"] and not receipt["push_completed"] and receipt["logout_verified"]
    assert events()[-1]["args"] == ["logout", "ghcr.io"]
    assert "generated-publication-token" not in result.stdout + result.stderr


def test_retained_registry_auth_is_not_reported_as_logged_out(publication):
    run, _, root, env, _ = publication
    env.update(GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1")
    assert run().returncode == 0
    assert run("publish", sticky_auth=True).returncode == 1
    receipt = json.loads((root / "artifacts/publication.json").read_text())
    assert not receipt["success"] and not receipt["logout_verified"]
    assert receipt["stage"] == "logout" and receipt["error_code"] == "registry_logout_not_verified"
    assert receipt["cleanup_error"]["stage"] == "logout"
    assert receipt["readback_verified"] and receipt["auth_directory_removed"]


def test_workflow_command_bodies_compose_with_real_release_entrypoint(publication):
    """Execute the shipped YAML run bodies; native-host/API/Docker are generated boundaries."""
    import re

    _, events, root, env, _ = publication
    workflow = (ROOT / ".github/workflows/release-image.yml").read_text()
    bodies = re.findall(r"^        run: \|\n((?:          [^\n]*\n)+)", workflow, re.MULTILINE)
    assert len(bodies) == 2
    assert "  workflow_dispatch:" in workflow and "  push:" not in workflow and "  pull_request:" not in workflow
    qualify_job, publish_job = workflow.split("\n  publish:\n")
    assert "packages: write" not in qualify_job and "packages: write" in publish_job
    assert "needs: qualify" in publish_job and "persist-credentials: false" in qualify_job
    # Admit a typo in source_sha to the helper's receipt-producing refusal, but
    # never execute a branch-dispatched helper or a different workflow revision.
    assert "    if: github.ref == 'refs/heads/main' && github.workflow_sha == github.sha\n" in qualify_job
    assert "github.sha == inputs.source_sha" not in qualify_job
    assert "github.sha == inputs.source_sha" in publish_job
    # Model only the host-architecture boundary; never claim this local test ran native Linux Docker.
    (root / "bin/uname").write_text("#!/bin/sh\nprintf 'x86_64\\n'\n")
    (root / "bin/uname").chmod(0o755)
    env.update(RUNNER_TEMP=str(root), SOURCE_SHA=env["GITHUB_SHA"], GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1")
    for index, body in enumerate(bodies):
        if index:
            env["GHCR_TOKEN"] = "generated-publication-token"
        shell = "set -eu\n" + "\n".join(line[10:] for line in body.splitlines())
        result = subprocess.run(["bash", "-c", shell], cwd=root / "repo", env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    receipt = json.loads((root / "qualified-image/publication.json").read_text())
    assert receipt["success"] and receipt["immutable_reference"].endswith(DIGEST)
    assert any(e["command"] == "smoke" for e in events())
