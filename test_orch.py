"""Orchestration workflow tests. `python3 test_orch.py` (fake worker); ORCH_REAL=1 adds a real-Codex run."""
import importlib.util, json, os, shutil, sqlite3, subprocess, sys, tempfile, time, unittest
from contextlib import closing
from pathlib import Path

ORCH = Path(__file__).resolve().with_name('orch.py')
TEST = 'python3 -c "import m; assert m.add(2,3)==5"'
FAKE = r'''#!/usr/bin/env python3
import json, os, re, sys, time, uuid
a = sys.argv[1:]
if a[:1] == ['--version']: print('codex-cli fake'); sys.exit(0)
if a[:2] == ['login', 'status']: print('Logged in (fake)'); sys.exit(0)
log = os.environ['FAKE_LOG']
n = sum(1 for _ in open(log)) if os.path.exists(log) else 0
modes = os.environ.get('FAKE_MODES', 'ok').split(',')
mode = modes[min(n, len(modes) - 1)]
resume = a[a.index('resume') + 1] if 'resume' in a else None
prompt, out = a[-1], a[a.index('-o') + 1]
tid = re.search(r'task ([\w.-]+-\d+)', prompt, re.I).group(1)
open(log, 'a').write(json.dumps({'mode': mode, 'resume': resume, 'prompt': prompt, 'cwd': os.getcwd()}) + '\n')
print(json.dumps({'type': 'thread.started', 'thread_id': resume or 'th-' + uuid.uuid4().hex[:8]}), flush=True)
def result(status='completed'):
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 10}}))
    json.dump({'task_id': tid, 'status': status, 'summary': 'fake ' + mode, 'changed_files': ['m.py'],
               'tests': [{'command': %r, 'status': 'passed', 'detail': ''}], 'evidence_refs': [], 'risks': [],
               'requires_decision': False, 'next_action': 'none'}, open(out, 'w'))
if mode == 'hang': time.sleep(60)
if mode == 'crash': print(json.dumps({'type': 'error', 'message': 'stream disconnected'})); sys.exit(1)
if mode == 'auth':
    print(json.dumps({'type': 'turn.failed', 'error': {'message': "The 'x' model is not supported when using Codex with a ChatGPT account."}})); sys.exit(1)
if mode in ('ok', 'outscope'): open('m.py', 'w').write('def add(a, b):\n    return a + b\n')
if mode == 'ok': os.makedirs('__pycache__', exist_ok=True); open('__pycache__/m.pyc', 'w').write('x')
if mode == 'ok' and 'FIRST RUN' in prompt:
    for name in ('PROJECT_CONTEXT.md', 'ARCHITECTURE_MAP.md'): open('docs/ai/' + name, 'w').write('# filled by survey\n')
if mode == 'outscope': open('other.py', 'w').write('x = 1\n')
if mode == 'lie': open('m.py', 'w').write('def add(a, b):\n    return a - b\n')
if mode == 'badjson': open(out, 'w').write('not json'); sys.exit(0)
result('blocked' if mode == 'blocked' else 'completed')
''' % TEST


class FakeTTY:
    def __init__(self, answer):
        self.answer, self.shown = answer, []

    def write(self, s):
        self.shown.append(s)

    def flush(self):
        pass

    def readline(self):
        return self.answer + '\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='orch-test-')).resolve()
        self.home = self.tmp / 'orch-home'
        (self.tmp / 'home').mkdir()
        fake = self.tmp / 'fake-codex'
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.flog = self.tmp / 'fake.log'
        self.env = {**os.environ, 'ORCH_HOME': str(self.home), 'FAKE_LOG': str(self.flog), 'HOME': str(self.tmp / 'home')}
        self.config(codex_bin=str(fake), timeout_sec=20)
        self.repo = self.make_repo('alpha')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, **kw):
        self.home.mkdir(exist_ok=True)
        f = self.home / 'config.json'
        cfg = json.loads(f.read_text()) if f.exists() else {}
        cfg.update(kw)
        f.write_text(json.dumps(cfg))

    def make_repo(self, name):
        d = self.tmp / name
        d.mkdir()
        (d / 'm.py').write_text('def add(a, b):\n    return a - b\n')
        for cmd in (['init', '-q'], ['add', '.'], ['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init']):
            subprocess.run(['git', *cmd], cwd=d, check=True)
        return d

    def orch(self, *args, cwd=None, modes='ok', ok=True, **kw):
        p = subprocess.run([sys.executable, str(ORCH), *args], cwd=cwd or self.repo, capture_output=True, text=True,
                           env={**self.env, 'FAKE_MODES': modes}, timeout=120, **kw)
        if ok and p.returncode:
            self.fail(f'orch {args} failed: {p.stderr}{p.stdout}')
        if not ok:
            self.assertNotEqual(p.returncode, 0, p.stdout)
        return p

    def new(self, objective='fix add() so it returns the sum', scope=('m.py',), tests=(TEST,), extra=(), cwd=None, modes='ok'):
        args = ['new', '--objective', objective, *sum((['--scope', s] for s in scope), []),
                *sum((['--test', t] for t in tests), []), *extra]
        return self.orch(*args, cwd=cwd, modes=modes).stdout.split()[0]

    def q(self, sql, *a):
        with closing(sqlite3.connect(self.home / 'state.db')) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(sql, a).fetchall()
            c.commit()
            return rows

    def row(self, tid):
        return self.q('SELECT * FROM tasks WHERE id=?', tid)[0]

    def sql(self, sql, *a):
        self.q(sql, *a)

    def states(self, tid):
        return [r[0] for r in self.q('SELECT new FROM transitions WHERE task_id=? ORDER BY id', tid)]

    def calls(self):
        return [json.loads(x) for x in self.flog.read_text().splitlines()] if self.flog.exists() else []


class Workflow(Base):
    def test_low_risk_fast_path_with_evidence(self):
        tid = self.new()
        self.assertEqual(self.row(tid)['state'], 'ASSIGNED')
        out = self.orch('run', tid).stdout
        self.assertIn('memory files in docs/ai/ are always allowed', self.calls()[0]['prompt'])
        self.assertEqual(self.states(tid), ['CREATED', 'TRIAGED', 'ASSIGNED', 'RUNNING', 'VALIDATING', 'APPROVED', 'COMPLETED'])
        v = json.loads(self.row(tid)['verify'])
        self.assertEqual(v['tests'][0]['status'], 'passed')
        self.assertIn('m.py', v['changed'])
        self.assertLess(len(out), 2000)                                    # compressed report, ~<500 tokens
        self.assertIn('PASS (re-run by orch)', out)
        ev = self.home / 'projects' / self.row(tid)['project_id'] / 'tasks' / tid
        self.assertIn('return a + b', (ev / 'diff.patch').read_text())     # detail kept out of the report
        self.assertIn(tid, (self.repo / 'docs/ai/LAST_HANDOFF.md').read_text())
        self.assertEqual(json.loads(self.row(tid)['usage'])['input_tokens'], 100)

    def test_medium_risk_needs_manager_review_and_fix_round(self):
        tid = self.new('refactor add() for clarity')
        self.orch('run', tid)
        self.assertEqual(self.row(tid)['state'], 'REVIEW_PENDING')
        self.orch('review', tid, '--fix', 'add a docstring')
        self.assertEqual(self.row(tid)['state'], 'RETRY_PENDING')
        self.orch('run', tid)
        last = self.calls()[-1]
        self.assertTrue(last['resume'])                                    # same worker thread reused
        self.assertIn('Manager review requested changes: add a docstring', last['prompt'])
        self.orch('review', tid, '--approve', '--note', 'lgtm')
        self.assertEqual(self.row(tid)['state'], 'COMPLETED')
        self.assertIn('approved: lgtm', (self.repo / 'docs/ai/DECISIONS.md').read_text())

    def test_high_risk_requires_human_at_terminal(self):
        tid = self.new('deploy the fix to production', extra=['--risk', 'low'])
        r = self.row(tid)
        self.assertEqual((r['state'], r['risk']), ('AWAITING_USER_APPROVAL', 'high'))   # risk floor: cannot lower
        self.assertIn('nothing to dispatch', self.orch('run', tid, ok=False).stderr)
        p = self.orch('approve', tid, ok=False, start_new_session=True)                 # agent shell: no /dev/tty
        self.assertIn('real terminal', p.stderr)
        os.environ['ORCH_HOME'] = str(self.home)
        try:
            spec = importlib.util.spec_from_file_location('orch_t', ORCH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            with self.assertRaises(mod.Err):
                mod.approve(tid, str(self.repo), FakeTTY('no'))
            mod.approve(tid, str(self.repo), FakeTTY(tid))
        finally:
            os.environ.pop('ORCH_HOME')
        self.assertEqual(self.row(tid)['state'], 'ASSIGNED')
        self.assertTrue(self.q("SELECT actor FROM transitions WHERE task_id=? AND new='ASSIGNED' ORDER BY id DESC",
                               tid)[0][0].startswith('human:'))
        self.orch('run', tid)
        self.assertEqual(self.row(tid)['state'], 'REVIEW_PENDING')          # high risk: manager still reviews

    def test_lying_worker_is_caught_and_auto_repaired(self):
        tid = self.new()
        self.orch('run', tid, modes='lie,ok')
        r = self.row(tid)
        self.assertEqual((r['state'], r['repairs']), ('COMPLETED', 1))
        self.assertTrue(self.calls()[1]['prompt'].startswith('REPAIR ROUND 1'))
        self.assertIn('reported these as passed', self.calls()[1]['prompt'])

    def test_repair_limit_cannot_be_raised_by_project(self):
        (self.repo / '.ai-orchestrator.json').write_text('{"max_auto_repair_attempts": 10}')
        tid = self.new()
        self.orch('run', tid, modes='lie')
        r = self.row(tid)
        self.assertEqual((r['state'], r['repairs'], len(self.calls())), ('FAILED', 3, 4))

    def test_timeout_then_resume_same_thread(self):
        self.config(timeout_sec=2)
        tid = self.new()
        self.orch('run', tid, modes='hang,ok')
        r = self.row(tid)
        self.assertEqual((r['state'], r['attempts']), ('COMPLETED', 1))
        first, second = self.calls()
        self.assertTrue(second['resume'])
        self.assertIn('previous run was interrupted', second['prompt'])

    def test_infra_retry_limit(self):
        tid = self.new()
        self.orch('run', tid, modes='crash')
        self.assertEqual((self.row(tid)['state'], len(self.calls())), ('FAILED', 3))

    def test_auth_error_blocks_without_retry_then_resumes(self):
        tid = self.new()
        self.orch('run', tid, modes='auth')
        self.assertEqual((self.row(tid)['state'], len(self.calls())), ('BLOCKED', 1))
        self.orch('review', tid, '--fix', 'login fixed, continue')
        self.orch('run', tid, modes='auth,ok')
        self.assertEqual(self.row(tid)['state'], 'COMPLETED')

    def test_quality_gate_flags(self):
        t1 = self.new()
        self.orch('run', t1, modes='outscope')
        self.assertEqual(self.row(t1)['state'], 'REVIEW_PENDING')
        self.assertEqual(json.loads(self.row(t1)['verify'])['out_of_scope'], ['other.py'])
        self.orch('review', t1, '--reject', 'scope creep')
        t2 = self.new('fix add() again', tests=())
        self.orch('run', t2)
        self.assertEqual(self.row(t2)['state'], 'REVIEW_PENDING')       # no tests -> no fast path
        t3 = self.new('third try')
        self.orch('run', t3, modes='blocked')
        self.assertEqual(self.row(t3)['state'], 'BLOCKED')

    def test_invalid_result_json_is_repaired(self):
        tid = self.new()
        self.orch('run', tid, modes='badjson,ok')
        r = self.row(tid)
        self.assertEqual((r['state'], r['repairs']), ('COMPLETED', 1))
        self.assertIn('Result JSON invalid', self.calls()[1]['prompt'])

    def test_idempotency_and_project_lock(self):
        t1 = self.new()
        out = self.orch('new', '--objective', 'fix add() so it returns the sum', '--scope', 'm.py', '--test', TEST).stdout
        self.assertIn('not duplicated', out)
        self.assertTrue(out.startswith(t1))
        t2 = self.new('another task')
        self.sql("UPDATE tasks SET state='RUNNING', pid=? WHERE id=?", os.getpid(), t1)   # live runner
        self.assertIn('nothing to dispatch', self.orch('run', t1, ok=False).stderr)
        self.assertIn('project busy', self.orch('run', t2, ok=False).stderr)
        self.assertEqual(self.calls(), [])

    def test_interrupted_runner_recovers(self):
        tid = self.new()
        self.sql("UPDATE tasks SET state='RUNNING', pid=999999 WHERE id=?", tid)          # dead runner
        self.orch('status')
        r = self.row(tid)
        self.assertEqual((r['state'], r['attempts']), ('RETRY_PENDING', 1))
        self.orch('run', tid)
        self.assertEqual(self.row(tid)['state'], 'COMPLETED')

    def test_cancel_kills_detached_worker(self):
        self.config(timeout_sec=60)
        tid = self.new()
        self.orch('run', tid, '--detach', modes='hang')
        for _ in range(100):
            if self.row(tid)['worker_pid']:
                break
            time.sleep(0.1)
        pid = self.row(tid)['worker_pid']
        self.assertTrue(pid)
        self.orch('cancel', tid)
        self.assertEqual(self.row(tid)['state'], 'CANCELLED')
        time.sleep(1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_external_worker_path(self):
        tid = self.new('fix add', scope=())                                 # medium: no scope
        out = self.orch('brief', tid).stdout
        self.assertIn('Codex technical worker', out)
        self.assertEqual(self.row(tid)['state'], 'RUNNING')
        self.orch('collect', tid, '--result', '-', input='garbage')
        self.assertEqual(self.row(tid)['state'], 'RETRY_PENDING')
        self.assertTrue(self.orch('brief', tid).stdout.startswith('REPAIR ROUND 1'))
        (self.repo / 'm.py').write_text('def add(a, b):\n    return a + b\n')
        res = {'task_id': tid, 'status': 'completed', 'summary': 's', 'changed_files': ['m.py'], 'evidence_refs': [],
               'tests': [{'command': TEST, 'status': 'passed', 'detail': ''}], 'risks': [], 'requires_decision': False,
               'next_action': ''}
        self.orch('collect', tid, '--result', '-', input='```json\n' + json.dumps(res) + '\n```')
        r = self.row(tid)
        self.assertEqual(r['state'], 'REVIEW_PENDING')
        self.assertEqual(json.loads(r['verify'])['tests'][0]['status'], 'passed')

    def test_disabled_switch(self):
        self.config(enabled=False)
        self.assertIn('disabled', self.orch('new', '--objective', 'x', ok=False).stderr)


class MemoryAndIsolation(Base):
    def test_new_session_continues_from_checkpoint(self):
        t1 = self.new()
        self.orch('run', t1)
        self.assertIn('FIRST RUN', self.calls()[0]['prompt'])
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        t2 = self.new('add a subtract() function', tests=('python3 -c "import m"',))
        self.orch('run', t2)                                                # fresh process = new session
        p = self.calls()[1]['prompt']
        self.assertNotIn('FIRST RUN', p)                                    # no repeated full audit
        self.assertEqual((self.repo / 'docs/ai/PROJECT_CONTEXT.md').read_text(), '# filled by survey\n')
        self.assertIn(head, p)
        self.assertIn('m.py', p.split('Files changed since the checkpoint:')[1])
        m = json.loads(self.orch('metrics').stdout)
        self.assertEqual((m['worker_dispatches'], m['first_run_surveys (full audits)']), (2, 1))
        self.assertEqual(m['codex_tokens']['input_tokens'], 200)

    def test_bootstrap_never_overwrites_project_docs(self):
        d = self.repo / 'docs/ai'
        d.mkdir(parents=True)
        (d / 'DECISIONS.md').write_text('# Our decisions\nkeep\n')
        (d / 'LAST_HANDOFF.md').write_text('my notes\n')
        self.orch('init')
        self.assertTrue((d / 'PROJECT_CONTEXT.md').exists())
        tid = self.new('refactor add')
        self.orch('run', tid)
        self.orch('review', tid, '--approve')
        self.assertEqual((d / 'LAST_HANDOFF.md').read_text(), 'my notes\n')
        self.assertTrue((d / 'DECISIONS.md').read_text().startswith('# Our decisions\nkeep\n'))

    def test_projects_are_isolated(self):
        beta = self.make_repo('beta')
        ta = self.new()
        tb = self.new(cwd=beta)
        self.assertNotEqual(ta.rsplit('-', 1)[0], tb.rsplit('-', 1)[0])
        self.assertNotIn(ta, self.orch('status', cwd=beta).stdout)
        self.assertIn('belongs to another project', self.orch('run', ta, cwd=beta, ok=False).stderr)
        self.assertIn('outside project', self.orch('new', '--objective', 'x', '--scope', '../alpha/m.py', cwd=beta, ok=False).stderr)
        self.assertIn('outside project', self.orch('new', '--objective', 'x', '--context', str(self.repo / 'm.py'), cwd=beta, ok=False).stderr)
        self.orch('run', tb, cwd=beta)
        self.assertEqual(self.calls()[0]['cwd'], str(beta))
        self.assertEqual((self.repo / 'm.py').read_text(), 'def add(a, b):\n    return a - b\n')   # alpha untouched
        self.assertNotIn(tb, (self.repo / 'docs/ai/TASK_BOARD.md').read_text())


    def test_vietnamese_path_in_nfc_or_nfd_is_inside_project(self):
        import unicodedata
        d = self.make_repo('Công việc')
        for form in ('NFC', 'NFD'):
            m = unicodedata.normalize(form, str(d / 'm.py'))
            tid = self.new(f'task {form}', scope=(m,), cwd=d)
            self.assertEqual(json.loads(self.row(tid)['brief'])['scope'], ['m.py'])


class Install(Base):
    def test_install_is_idempotent_and_reversible(self):
        h = self.tmp / 'home'
        (h / '.claude').mkdir()
        (h / '.codex').mkdir()
        (h / '.local/bin').mkdir(parents=True)
        (h / '.claude/CLAUDE.md').write_text('# mine\n')
        (h / '.codex/AGENTS.md').write_text('')
        self.orch('install')
        self.orch('install')
        claude = (h / '.claude/CLAUDE.md').read_text()
        self.assertTrue(claude.startswith('# mine\n'))
        self.assertEqual(claude.count('ai-orchestrator:begin'), 1)
        self.assertIn('EVIDENCE-FIRST', (h / '.codex/AGENTS.md').read_text())
        self.assertTrue((h / '.claude/skills/orchestrate/SKILL.md').exists())
        installed = h / '.local/bin/orch'
        self.assertEqual(os.readlink(installed), str(self.home / 'bin/orch'))
        p = subprocess.run([str(installed), 'doctor'], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('policy block in', p.stdout)
        self.orch('uninstall')
        self.assertEqual((h / '.claude/CLAUDE.md').read_text(), '# mine\n')
        self.assertEqual((h / '.codex/AGENTS.md').read_text(), '')
        self.assertFalse((h / '.claude/skills/orchestrate').exists() or installed.is_symlink())
        self.assertTrue(any((self.home / 'backups').iterdir()))


@unittest.skipUnless(os.environ.get('ORCH_REAL'), 'set ORCH_REAL=1 to run against the real Codex worker')
class RealCodex(Base):
    def test_two_projects_real_worker(self):
        self.config(codex_bin='auto', timeout_sec=900)
        beta = self.make_repo('beta')
        env = {k: v for k, v in self.env.items() if k != 'HOME'}            # real codex needs the real ~/.codex
        for repo in (self.repo, beta):
            p = subprocess.run([sys.executable, str(ORCH), 'new', '--objective', 'fix add() so it returns the sum',
                                '--scope', 'm.py', '--test', TEST, '--run'], cwd=repo, env=env,
                               capture_output=True, text=True, timeout=1000)
            print(p.stdout, p.stderr)
            tid = p.stdout.split()[0]
            self.assertEqual(self.row(tid)['state'], 'COMPLETED')
            self.assertIn('return a + b', (repo / 'm.py').read_text().replace('a+b', 'a + b'))
            self.assertNotIn('ai-orchestrator:skeleton', (repo / 'docs/ai/PROJECT_CONTEXT.md').read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
