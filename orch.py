#!/usr/bin/env python3
"""orch: Codex-first task orchestrator (Claude manages, Codex works). Stdlib only, Python 3.9+.

State lives in $ORCH_HOME (default ~/.ai-orchestrator): state.db, config.json, projects/<id>/tasks/<task>/.
See README.md for the workflow; `orch -h` for commands.
"""
import argparse, fnmatch, getpass, hashlib, json, os, re, shutil, signal, sqlite3, subprocess, sys, time, unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get('ORCH_HOME') or Path.home() / '.ai-orchestrator')
OWNED = '<!-- ai-orchestrator -->'               # memory files orch may rewrite
SKELETON = '<!-- ai-orchestrator:skeleton -->'   # worker replaces these on the first run
BEGIN, END = '<!-- ai-orchestrator:begin -->', '<!-- ai-orchestrator:end -->'

DEFAULTS = {
    'enabled': True,
    'codex_bin': 'auto',            # auto = `codex` on PATH; set an absolute path to pin one build
    'codex_model': None,            # None = ~/.codex/config.toml default
    'codex_effort': {'low': 'medium', 'medium': 'high', 'high': 'xhigh'},
    'timeout_sec': 1800, 'test_timeout_sec': 600,
    'max_attempts': 3, 'max_auto_repair_attempts': 3,
    'memory_dir': 'docs/ai',
    'extra_high_risk_keywords': [], 'extra_medium_risk_keywords': [],
}
HARD_MAX = {'max_attempts': 3, 'max_auto_repair_attempts': 3}   # overrides may lower these, never raise

FLOW = {
    'CREATED': {'TRIAGED', 'CANCELLED'},
    'TRIAGED': {'ASSIGNED', 'AWAITING_USER_APPROVAL', 'CANCELLED'},
    'AWAITING_USER_APPROVAL': {'ASSIGNED', 'CANCELLED'},
    'ASSIGNED': {'RUNNING', 'BLOCKED', 'CANCELLED'},
    'RUNNING': {'VALIDATING', 'RETRY_PENDING', 'BLOCKED', 'FAILED', 'CANCELLED'},
    'VALIDATING': {'REVIEW_PENDING', 'APPROVED', 'RETRY_PENDING', 'BLOCKED', 'FAILED', 'CANCELLED'},
    'RETRY_PENDING': {'RUNNING', 'BLOCKED', 'FAILED', 'CANCELLED'},
    'REVIEW_PENDING': {'APPROVED', 'RETRY_PENDING', 'FAILED', 'CANCELLED'},
    'BLOCKED': {'RETRY_PENDING', 'CANCELLED'},
    'APPROVED': {'COMPLETED'},
    'COMPLETED': set(), 'FAILED': set(), 'CANCELLED': set(),
}
ACTIVE = ('RUNNING', 'VALIDATING')
NEXT = {
    'ASSIGNED': 'orch run {id}', 'RETRY_PENDING': 'orch run {id}',
    'AWAITING_USER_APPROVAL': 'a human runs `orch approve {id}` in a terminal (or `orch cancel {id}`)',
    'RUNNING': 'orch wait {id}', 'VALIDATING': 'orch wait {id}',
    'REVIEW_PENDING': 'orch review {id} --approve | --fix "<what to change>" | --reject "<why>"',
    'BLOCKED': 'resolve the blocker, then orch review {id} --fix "<instruction>"',
    'FAILED': 'escalate: read the evidence, or create a narrower task', 'COMPLETED': 'done', 'CANCELLED': 'none',
}

# ponytail: keyword heuristic misses semantic risk; the manager can raise risk (never lower it) per task.
HIGH_RX = (r"\bprod(uction)?\b|deploy|migrat|\bdrop\b|truncate|rm -rf|force[- ]?push|rewrite history|credential|"
           r"secret|password|access token|api[ _-]?keys?|private key|\.env\b|permission|\bauth(n|z|entication|orization)?\b|"
           r"oauth|rbac|payment|billing|irreversible|delete (all|data|users?|records?)|"
           r"triển khai|mật khẩu|phân quyền|thanh toán|xóa dữ liệu|xoá dữ liệu")
MEDIUM_RX = (r"refactor|schema|database|\bdb\b|dependenc|upgrade|\bapi\b|config|\bci\b|pipeline|docker|security|"
             r"performance|tái cấu trúc|cơ sở dữ liệu|cấu hình|nâng cấp")
HIGH_PATH_RX = r"(^|/)\.env|secret|credential|migrations?/|\.github/workflows|deploy|terraform|helm|k8s"
# ponytail: fixed list of tool caches; a project's own .gitignore covers anything else
CACHE_RX = r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules|\.venv|\.DS_Store)(/|$)"
BLOCKING_RX = r"not supported|unauthori[sz]ed|\b40[13]\b|log ?in|quota|billing|invalid api key"
LEVELS = ['low', 'medium', 'high']

S, SA = {'type': 'string'}, {'type': 'array', 'items': {'type': 'string'}}
BRIEF_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['objective'], 'properties': {
    'objective': S, 'scope': SA, 'constraints': SA, 'acceptance_criteria': SA, 'required_tests': SA,
    'priority': {'type': 'string', 'enum': LEVELS}, 'risk_level': {'type': 'string', 'enum': LEVELS},
    'context_reference': S, 'requires_approval': {'type': 'boolean'}}}
RESULT_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': [
    'task_id', 'status', 'summary', 'changed_files', 'tests', 'evidence_refs', 'risks', 'requires_decision', 'next_action'],
    'properties': {
        'task_id': S, 'summary': S, 'next_action': S, 'changed_files': SA, 'evidence_refs': SA, 'risks': SA,
        'status': {'type': 'string', 'enum': ['completed', 'failed', 'blocked', 'partial']},
        'requires_decision': {'type': 'boolean'},
        'tests': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['command', 'status', 'detail'],
            'properties': {'command': S, 'detail': S,
                           'status': {'type': 'string', 'enum': ['passed', 'failed', 'skipped', 'not_run']}}}}}}

WORKER_PROMPT = """You are the Codex technical worker for orchestrated task {id}.
Mode: TECHNICAL EXECUTION / SELF-REVIEW / EVIDENCE-FIRST.

TASK BRIEF (JSON):
{brief}

PROJECT MEMORY in `{mem}/`: read LAST_HANDOFF.md first, then only the memory files the task needs.
They are references; the source code wins when they disagree.
{survey}Checkpoint: {checkpoint}. Files changed since the checkpoint: {since}.
Analyse deeply only the modules this task touches; no full-repository audit unless the brief asks for one.

Rules:
- Work only inside this repository{scope_rule}. No destructive operations (deleting data, force-push,
  history rewrite), no production/deploy actions, no reading or printing credentials. If the task needs
  one of these, stop and return status "blocked" with the reason.
- Run every command in required_tests yourself, plus relevant existing tests. Report each one honestly as
  passed / failed / skipped / not_run. Never report a test you did not run. The orchestrator re-runs
  required_tests independently and compares.
- Fix failures you caused (within scope), then self-review your diff.
- If your change makes PROJECT_CONTEXT.md or ARCHITECTURE_MAP.md stale, update them briefly.
  Do not edit TASK_BOARD.md or LAST_HANDOFF.md (orchestrator-owned). Do not commit.
- Final message: ONLY the result JSON (schema enforced). task_id = "{id}". summary under 120 words;
  put long detail in files and list their paths in evidence_refs.
"""
SURVEY = ("FIRST RUN: PROJECT_CONTEXT.md and ARCHITECTURE_MAP.md are skeletons. Survey the repository once and "
          "replace them with concise content (under 150 lines each, remove the skeleton marker).\n")
CONTINUE = ("Task {id}: the previous run was interrupted. Continue where you left off, re-run the required tests, "
            "and reply with the result JSON.")
MEMORY = {
    'PROJECT_CONTEXT.md': f'# Project Context\n\n{SKELETON}\nPurpose, stack, build/test/run commands, conventions, '
                          'gotchas. Keep under 150 lines.\nReference only: the source code wins when they disagree.\n',
    'ARCHITECTURE_MAP.md': f'# Architecture Map\n\n{SKELETON}\nModules, entry points, data flow, external services: '
                           'one line each, with paths. Keep under 150 lines.\n',
    'DECISIONS.md': '# Decisions\n\nAppend-only log of review decisions (newest last).\n',
    'TASK_BOARD.md': f'# Task Board\n\n{OWNED}\n_No tasks yet._\n',
    'LAST_HANDOFF.md': f'# Last Handoff\n\n{OWNED}\n_No completed tasks yet._\n',
}
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, root TEXT UNIQUE, checkpoint TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project_id TEXT, seq INT, idem_key TEXT, state TEXT, risk TEXT,
  brief TEXT, thread_id TEXT, pid INT, worker_pid INT, attempts INT DEFAULT 0, repairs INT DEFAULT 0,
  pending_prompt TEXT, result TEXT, verify TEXT, usage TEXT, snapshot TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS transitions(id INTEGER PRIMARY KEY, task_id TEXT, ts TEXT, actor TEXT, prev TEXT, new TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id, state);
"""


class Err(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def short(s, n):
    s = ' '.join(str(s or '').split())
    return s if len(s) <= n else s[:n - 1] + '…'


def check(v, s, at='$'):
    """Validate v against the small JSON-schema subset used here; returns a list of errors."""
    kind = {'object': dict, 'array': list, 'string': str, 'boolean': bool}[s['type']]
    if not isinstance(v, kind):
        return [f'{at}: expected {s["type"]}']
    if 'enum' in s and v not in s['enum']:
        return [f'{at}: must be one of {s["enum"]}']
    errs = []
    if kind is dict:
        errs += [f'{at}.{k}: required' for k in s.get('required', []) if k not in v]
        errs += [f'{at}.{k}: unknown field' for k in v if k not in s['properties']]
        for k, x in v.items():
            if k in s['properties']:
                errs += check(x, s['properties'][k], f'{at}.{k}')
    if kind is list:
        for i, x in enumerate(v):
            errs += check(x, s['items'], f'{at}[{i}]')
    return errs


# ---------- storage ----------

def db():
    HOME.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(HOME / 'state.db'), timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript(SCHEMA_SQL)
    return c


@contextmanager
def tx(c):
    c.execute('BEGIN IMMEDIATE')
    try:
        yield
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise


def get(c, tid):
    t = c.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    if not t:
        raise Err(f'no task {tid}')
    return t


def log(c, tid, actor, prev, new, reason):
    c.execute('INSERT INTO transitions(task_id,ts,actor,prev,new,reason) VALUES(?,?,?,?,?,?)',
              (tid, now(), actor, prev, new, reason))


def upd(c, t, **fields):
    sets = ', '.join(f'{k}=?' for k in ['updated', *fields])
    c.execute(f'UPDATE tasks SET {sets} WHERE id=?', [now(), *fields.values(), t['id']])
    return get(c, t['id'])


def move(c, t, new, actor, reason='', **fields):
    """State transition with compare-and-swap on the previous state; always audited. Call inside tx()."""
    prev = t['state']
    if new not in FLOW[prev]:
        raise Err(f'{t["id"]}: illegal transition {prev} -> {new}')
    sets = ', '.join(f'{k}=?' for k in ['state', 'updated', *fields])
    cur = c.execute(f'UPDATE tasks SET {sets} WHERE id=? AND state=?', [new, now(), *fields.values(), t['id'], prev])
    if cur.rowcount != 1:
        raise Err(f'{t["id"]} changed concurrently (expected {prev})')
    log(c, t['id'], actor, prev, new, reason)
    return get(c, t['id'])


def brief(t):
    return json.loads(t['brief'])


def task_dir(t):
    return HOME / 'projects' / t['project_id'] / 'tasks' / t['id']


def config(root=None):
    cfg = json.loads(json.dumps(DEFAULTS))
    for f in [HOME / 'config.json'] + ([Path(root) / '.ai-orchestrator.json'] if root else []):
        if f.exists():
            try:
                data = json.loads(f.read_text())
            except ValueError as e:
                raise Err(f'{f}: invalid JSON ({e})')
            unknown = set(data) - set(DEFAULTS)
            if unknown:
                raise Err(f'{f}: unknown keys {sorted(unknown)}')
            cfg.update(data)
    for k, cap in HARD_MAX.items():
        cfg[k] = max(0, min(int(cfg[k]), cap))
    cfg['max_attempts'] = max(1, cfg['max_attempts'])
    return cfg


# ---------- projects, git, memory ----------

def git(root, *a, check=True):
    p = subprocess.run(['git', '-C', str(root), *a], capture_output=True, text=True)
    if check and p.returncode:
        raise Err(f'git {" ".join(a)} failed in {root}: {p.stderr.strip()}')
    return p.stdout


def zsplit(s):
    return [x for x in s.split('\0') if x]


def project(c, path='.', create=False):
    top = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if top.returncode:
        raise Err(f'{Path(path).resolve()} is not inside a git repository (run `git init` and commit once)')
    root = nfc(Path(top.stdout.strip()).resolve())
    pid = (re.sub(r'[^a-z0-9]+', '-', root.name.lower()).strip('-')[:20] or 'project') + '-' + \
        hashlib.sha1(str(root).encode()).hexdigest()[:6]
    p = c.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
    if p:
        return p
    if not create:
        raise Err(f'{root} is not initialised; run `orch init` there')
    head = git(root, 'rev-parse', '--verify', '-q', 'HEAD', check=False).strip()
    if not head:
        raise Err(f'{root} has no commits yet; commit once so the orchestrator can checkpoint')
    with tx(c):
        c.execute('INSERT INTO projects(id,root,checkpoint,created) VALUES(?,?,?,?)', (pid, str(root), head, now()))
    p = c.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
    d = memdir(p, config(root))
    d.mkdir(parents=True, exist_ok=True)
    for name, body in MEMORY.items():
        if not (d / name).exists():        # never overwrite existing project docs
            (d / name).write_text(body)
    return p


def memdir(p, cfg):
    d = Path(cfg['memory_dir']).expanduser()
    return d / p['id'] if d.is_absolute() else Path(p['root']) / d   # absolute dir: still one folder per project


def nfc(p):
    return Path(unicodedata.normalize('NFC', str(p)))   # macOS paths may arrive as NFC or NFD (e.g. "Công việc")


def inside(root, rel):
    q, root = nfc((Path(root) / rel).resolve()), nfc(root)
    return q == root or root in q.parents


def in_scope(f, scope):
    return any(f == s or f.startswith(s.rstrip('/') + '/') or fnmatch.fnmatch(f, s) for s in scope)


def fhash(f):
    return hashlib.sha1(f.read_bytes()).hexdigest() if f.is_file() else 'deleted'


def snapshot(root):
    names = set(zsplit(git(root, 'diff', '--name-only', '-z', 'HEAD'))) | \
        set(zsplit(git(root, 'ls-files', '-o', '--exclude-standard', '-z')))
    return {'head': git(root, 'rev-parse', 'HEAD').strip(),
            'files': {n: fhash(Path(root) / n) for n in names if not re.search(CACHE_RX, n)}}


def changed_since(root, before):
    after = snapshot(root)
    files = {f for f in set(before['files']) | set(after['files']) if before['files'].get(f) != after['files'].get(f)}
    if after['head'] != before['head']:      # worker committed despite instructions: include those files too
        files |= set(zsplit(git(root, 'diff', '--name-only', '-z', before['head'], after['head'])))
    return sorted(files)


def since_checkpoint(root, ck):
    names = sorted(set(zsplit(git(root, 'diff', '--name-only', '-z', ck, check=False))) |
                   set(zsplit(git(root, 'ls-files', '-o', '--exclude-standard', '-z'))))
    return (', '.join(names[:60]) + (f' (+{len(names) - 60} more)' if len(names) > 60 else '')) or 'none'


def write_diff(root, files, d):
    tracked = set(zsplit(git(root, 'ls-files', '-z')))
    parts = [git(root, 'diff', 'HEAD', '--', *[f for f in files if f in tracked], check=False)] \
        if any(f in tracked for f in files) else []
    parts += [git(root, 'diff', '--no-index', '--', '/dev/null', f, check=False)
              for f in files if f not in tracked and (Path(root) / f).is_file()]
    (d / 'diff.patch').write_text(''.join(parts))


def put_owned(f, text):
    if f.exists() and OWNED not in f.read_text():
        return                                  # user-owned file: leave it alone
    f.write_text(text)


def sync_memory(c, p, cfg):
    d = memdir(p, cfg)
    if not d.is_dir():
        return
    p = c.execute('SELECT * FROM projects WHERE id=?', (p['id'],)).fetchone()
    rows = c.execute('SELECT * FROM tasks WHERE project_id=? ORDER BY seq', (p['id'],)).fetchall()
    board = [f'# Task Board\n\n{OWNED} generated by `orch`; edits are overwritten.\n',
             '| Task | State | Risk | Objective | Updated |', '|---|---|---|---|---|']
    board += [f"| {t['id']} | {t['state']} | {t['risk']} | {short(brief(t)['objective'], 70).replace('|', '/')} "
              f"| {t['updated'][:16]} |" for t in rows[-50:]]
    put_owned(d / 'TASK_BOARD.md', '\n'.join(board) + '\n')
    done = [t for t in rows if t['state'] == 'COMPLETED']
    if done:
        t = done[-1]
        r, v = json.loads(t['result'] or '{}'), json.loads(t['verify'] or '{}')
        open_ = [f"{x['id']} ({x['state']})" for x in rows if x['state'] not in ('COMPLETED', 'FAILED', 'CANCELLED')]
        put_owned(d / 'LAST_HANDOFF.md', f"""# Last Handoff

{OWNED} generated by `orch` at {now()}; edits are overwritten.

- Checkpoint: `{p['checkpoint']}` (uncommitted worker changes may sit on top)
- Last completed: {t['id']}: {short(brief(t)['objective'], 160)}
- Summary: {short(r.get('summary'), 600)}
- Changed files: {', '.join(v.get('changed', [])[:30]) or 'none'}
- Next action: {short(r.get('next_action'), 300) or 'none'}
- Open tasks: {', '.join(open_) or 'none'}
""")


def note_decision(p, cfg, line):
    f = memdir(p, cfg) / 'DECISIONS.md'
    if f.parent.is_dir():
        with open(f, 'a') as fh:                # append-only, safe for user-owned files
            fh.write(f'- {now()[:16]} {line}\n')


# ---------- routing ----------

def classify(b, cfg):
    text = unicodedata.normalize('NFC', ' '.join([b['objective'], *b.get('constraints', [])]).lower())
    scope = b.get('scope', [])
    m = re.search(HIGH_RX, text)
    hit = [w for w in cfg['extra_high_risk_keywords'] if w.lower() in text]
    paths = [s for s in scope if re.search(HIGH_PATH_RX, s.lower())]
    if m or hit or paths:
        return 'high', f'high-impact: {(m.group(0) if m else "") or ", ".join(hit or paths)}'
    m = re.search(MEDIUM_RX, text)
    hit = [w for w in cfg['extra_medium_risk_keywords'] if w.lower() in text]
    if m or hit:
        return 'medium', f'medium-impact keyword "{m.group(0) if m else hit[0]}"'
    if not scope:
        return 'medium', 'no scope given (impact unknown)'
    if len(scope) > 8:
        return 'medium', f'broad scope ({len(scope)} paths)'
    return 'low', 'narrow scope, no high-impact keywords'


def route(risk, approval):
    if risk == 'high' or approval:
        return 'human approval -> Codex -> validation -> manager review'
    return 'Codex -> validation -> manager review' if risk == 'medium' else 'fast path: Codex -> validation -> auto-complete'


# ---------- worker ----------

def codex_bin(cfg):
    if cfg['codex_bin'] != 'auto':
        return cfg['codex_bin'] if os.access(cfg['codex_bin'], os.X_OK) else None
    return shutil.which('codex')


def full_prompt(c, t, p, cfg):
    b, root, mem = brief(t), Path(p['root']), memdir(p, cfg)
    ctx = mem / 'PROJECT_CONTEXT.md'
    survey = ctx.exists() and SKELETON in ctx.read_text()     # asked again until the memory is really filled
    memrel = os.path.relpath(mem, root)
    scope_rule = f" and within scope {b['scope']} (the memory files in {memrel}/ are always allowed)" if b.get('scope') else ''
    return WORKER_PROMPT.format(id=t['id'], brief=json.dumps(b, indent=1, ensure_ascii=False),
                                mem=memrel, survey=SURVEY if survey else '',
                                checkpoint=p['checkpoint'], since=since_checkpoint(root, p['checkpoint']),
                                scope_rule=scope_rule), survey


def prompt_for(c, t, p, cfg, has_thread):
    if t['pending_prompt']:
        return (t['pending_prompt'], False) if has_thread else \
            (full_prompt(c, t, p, cfg)[0] + '\n' + t['pending_prompt'], False)
    return (CONTINUE.format(id=t['id']), False) if has_thread else full_prompt(c, t, p, cfg)


def claim(c, t, p, actor, reason, pid):
    """ASSIGNED/RETRY_PENDING -> RUNNING exactly once per project working tree. Call inside tx()."""
    t = get(c, t['id'])
    if t['state'] not in ('ASSIGNED', 'RETRY_PENDING'):
        raise Err(f"{t['id']} is {t['state']}: nothing to dispatch (duplicate runs are refused)")
    busy = c.execute("SELECT id FROM tasks WHERE project_id=? AND id!=? AND state IN ('RUNNING','VALIDATING')",
                     (p['id'], t['id'])).fetchone()
    if busy:
        raise Err(f"project busy: {busy['id']} is running in this working tree; wait for it or cancel it")
    snap = t['snapshot'] or json.dumps(snapshot(p['root']))
    return move(c, t, 'RUNNING', actor, reason, pid=pid, worker_pid=None, snapshot=snap)


def parse_events(f):
    thread, usage, errors = None, {}, []
    for line in f.read_text(errors='replace').splitlines() if f.exists() else []:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get('type') == 'thread.started':
            thread = e.get('thread_id')
        elif e.get('type') == 'turn.completed':
            for k, v in (e.get('usage') or {}).items():
                usage[k] = usage.get(k, 0) + (v if isinstance(v, int) else 0)
        elif e.get('type') in ('turn.failed', 'error'):
            err = e.get('error')
            errors.append(str((err.get('message') if isinstance(err, dict) else err) or e.get('message')))
    return thread, usage, errors


def kill_group(pid):
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def alive(pid):
    # ponytail: pid liveness can be fooled by pid reuse; a heartbeat column would fix that if it ever matters
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def infra_fail(c, t, cfg, why):
    if t['attempts'] + 1 >= cfg['max_attempts']:
        return move(c, t, 'FAILED', 'orchestrator', f"{why} (attempt {t['attempts'] + 1}/{cfg['max_attempts']})")
    return move(c, t, 'RETRY_PENDING', 'orchestrator', why, attempts=t['attempts'] + 1)


def repair(c, t, cfg, why):
    if t['repairs'] >= cfg['max_auto_repair_attempts']:
        return move(c, t, 'FAILED', 'orchestrator',
                    f"auto-repair limit ({cfg['max_auto_repair_attempts']}) reached: {short(why, 300)}")
    prompt = (f"REPAIR ROUND {t['repairs'] + 1} for task {t['id']}.\n{why}\nDiagnose the root cause, fix it within "
              "the original scope, re-run the required tests, self-review, then reply with the result JSON.")
    return move(c, t, 'RETRY_PENDING', 'orchestrator', short(why, 300), repairs=t['repairs'] + 1, pending_prompt=prompt)


def dispatch(c, t, p, cfg):
    root, d = Path(p['root']), task_dir(t)
    d.mkdir(parents=True, exist_ok=True)
    binary = codex_bin(cfg)
    with tx(c):
        t = get(c, t['id'])
        if not binary and t['state'] in ('ASSIGNED', 'RETRY_PENDING'):
            return move(c, t, 'BLOCKED', 'orchestrator', 'no codex worker available (run `orch doctor`)')
        resume = bool(t['thread_id'])
        prompt, survey = prompt_for(c, t, p, cfg, resume)
        t = claim(c, t, p, 'orchestrator', (f"resume codex thread {t['thread_id']}" if resume else 'new codex thread')
                  + (' + first-run survey' if survey else ''), os.getpid())
    n = c.execute("SELECT COUNT(*) FROM transitions WHERE task_id=? AND new='RUNNING'", (t['id'],)).fetchone()[0]
    events, last, schema = d / f'run-{n}.jsonl', d / f'result-{n}.json', d / 'result.schema.json'
    schema.write_text(json.dumps(RESULT_SCHEMA))
    cmd = [binary, 'exec'] + (['resume', t['thread_id']] if resume else []) + \
        ['--json', '--output-schema', str(schema), '-o', str(last), '-c', 'sandbox_mode="workspace-write"']
    if cfg['codex_model']:
        cmd += ['-m', cfg['codex_model']]
    effort = cfg['codex_effort'].get(t['risk']) if isinstance(cfg['codex_effort'], dict) else cfg['codex_effort']
    if effort:
        cmd += ['-c', f'model_reasoning_effort="{effort}"']
    mem = memdir(p, cfg)
    if not inside(root, mem):
        cmd += ['-c', f'sandbox_workspace_write.writable_roots={json.dumps([str(mem)])}']
    cmd.append(prompt)
    with open(events, 'w') as out, open(d / 'stderr.log', 'a') as err:
        proc = subprocess.Popen(cmd, cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        with tx(c):
            c.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (proc.pid, t['id']))
        try:
            rc, timed_out = proc.wait(timeout=cfg['timeout_sec']), False
        except subprocess.TimeoutExpired:
            kill_group(proc.pid)
            proc.wait()
            rc, timed_out = -9, True
    thread, usage, errors = parse_events(events)
    with tx(c):
        t = get(c, t['id'])
        if t['state'] != 'RUNNING':
            return t                                    # cancelled or recovered meanwhile
        total = json.loads(t['usage'] or '{}')
        for k, v in usage.items():
            total[k] = total.get(k, 0) + v
        t = upd(c, t, thread_id=thread or t['thread_id'], usage=json.dumps(total), worker_pid=None)
        if timed_out:
            return infra_fail(c, t, cfg, f"worker timed out after {cfg['timeout_sec']}s")
        if rc != 0 or not last.exists():
            msg = short('; '.join(errors), 500) or f'codex exited {rc} without a result'
            if re.search(BLOCKING_RX, msg, re.I):
                return move(c, t, 'BLOCKED', 'orchestrator', msg)
            return infra_fail(c, t, cfg, msg)
        shutil.copy(last, d / 'result.json')
        t = move(c, t, 'VALIDATING', 'codex', 'worker returned a result', pending_prompt=None)
    return gate(c, t, p, cfg, last.read_text())


def run_tests(cmds, root, cfg, d):
    out = []
    with open(d / 'tests.log', 'a') as fh:
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, timeout=cfg['test_timeout_sec'])
                st, text = ('passed' if r.returncode == 0 else 'failed'), r.stdout + r.stderr
            except subprocess.TimeoutExpired:
                st, text = 'failed', f"timed out after {cfg['test_timeout_sec']}s"
            fh.write(f'\n$ {cmd}  [{st}] {now()}\n{text}')
            out.append({'command': cmd, 'status': st, 'tail': text[-300:].strip()})
    return out


def gate(c, t, p, cfg, raw):
    """Quality gate: validate the contract, verify changes with git, re-run required tests, then route."""
    root, b, d = Path(p['root']), brief(t), task_dir(t)
    try:
        res = json.loads(raw[raw.find('{'):raw.rfind('}') + 1])
        errs = check(res, RESULT_SCHEMA)
    except ValueError as e:
        res, errs = None, [f'not JSON: {e}']
    if not errs and res['task_id'] != t['id']:
        errs = [f"task_id {res['task_id']!r} does not match {t['id']}"]
    if errs:
        with tx(c):
            return repair(c, get(c, t['id']), cfg, 'Result JSON invalid: ' + '; '.join(errs[:5]))
    changed = changed_since(root, json.loads(t['snapshot']))
    mem, scope = memdir(p, cfg), b.get('scope') or []
    oos = [f for f in changed if scope and not in_scope(f, scope) and not inside(mem, root / f)]
    tests = run_tests(b.get('required_tests', []), root, cfg, d)
    claimed = {x['command']: x['status'] for x in res['tests']}
    failed = [x for x in tests if x['status'] != 'passed']
    mismatch = [x['command'] for x in failed if claimed.get(x['command']) == 'passed']
    write_diff(root, changed, d)
    verify = {'changed': changed, 'out_of_scope': oos, 'tests': tests, 'mismatch': mismatch}
    with tx(c):
        t = upd(c, get(c, t['id']), result=json.dumps(res), verify=json.dumps(verify))
        if res['status'] == 'blocked':
            return move(c, t, 'BLOCKED', 'codex', short(res['next_action'] or res['summary'], 300))
        if failed:
            why = [f"Required test failed when re-run by the orchestrator: {x['command']}\n{x['tail']}" for x in failed]
            if mismatch:
                why.append('You reported these as passed, but they fail on re-run: ' + ', '.join(mismatch))
            return repair(c, t, cfg, '\n'.join(why))
        flags = [f"worker status {res['status']}"] * (res['status'] != 'completed') + \
            ['worker requests a decision'] * res['requires_decision'] + \
            [f'{len(oos)} file(s) out of scope'] * bool(oos) + ['no required tests: no independent evidence'] * (not tests)
        if flags or t['risk'] != 'low':
            return move(c, t, 'REVIEW_PENDING', 'orchestrator', '; '.join(flags) or f"{t['risk']} risk needs manager review")
        t = move(c, t, 'APPROVED', 'policy:fast-path', 'low risk, in scope, required tests verified')
        return finalize(c, t, p)


def finalize(c, t, p):
    head = git(p['root'], 'rev-parse', 'HEAD').strip()
    c.execute('UPDATE projects SET checkpoint=? WHERE id=?', (head, p['id']))
    return move(c, t, 'COMPLETED', 'orchestrator', f'checkpoint {head[:12]}')


def recover(c):
    """Runner died (session closed, crash, reboot) -> retry or fail. External workers time out instead."""
    for t in c.execute("SELECT * FROM tasks WHERE state IN ('RUNNING','VALIDATING')").fetchall():
        stale = alive(t['pid']) is False if t['pid'] else \
            (datetime.now(timezone.utc) - datetime.fromisoformat(t['updated'])).total_seconds() > config()['timeout_sec']
        if stale:
            kill_group(t['worker_pid'])
            p = c.execute('SELECT * FROM projects WHERE id=?', (t['project_id'],)).fetchone()
            with tx(c):
                t2 = get(c, t['id'])
                if t2['state'] == t['state']:
                    infra_fail(c, t2, config(p['root']), 'runner process died (session interrupted?)' if t['pid']
                               else 'external worker gave no result before the timeout')


def run_task(c, t, p, cfg):
    while True:
        t = dispatch(c, t, p, cfg)
        if t['state'] != 'RETRY_PENDING':
            return t
        if not t['pending_prompt']:
            time.sleep(min(30, 2 ** t['attempts']))   # backoff for infrastructure retries only


# ---------- reporting ----------

def report(c, t, p, cfg):
    b = brief(t)
    r, v, u = json.loads(t['result'] or '{}'), json.loads(t['verify'] or '{}'), json.loads(t['usage'] or '{}')
    last = c.execute('SELECT reason FROM transitions WHERE task_id=? ORDER BY id DESC LIMIT 1', (t['id'],)).fetchone()
    L = [f"{t['id']} · {t['state']} · risk {t['risk']} · repairs {t['repairs']}/{cfg['max_auto_repair_attempts']}"
         f" · infra retries {t['attempts']}/{cfg['max_attempts']}",
         f"Objective: {short(b['objective'], 160)}"]
    if last and last['reason']:
        L.append(f"Last transition: {short(last['reason'], 220)}")
    if r:
        L.append(f"Worker ({r['status']}): {short(r['summary'], 600)}")
    if v:
        ch = v['changed']
        L.append(f"Changed (git-verified, {len(ch)}): {', '.join(ch[:12]) or 'none'}{' …' if len(ch) > 12 else ''}")
        if v['out_of_scope']:
            L.append('WARNING out of scope: ' + ', '.join(v['out_of_scope'][:10]))
        for x in v['tests']:
            ok = x['status'] == 'passed'
            L.append(f"  {'PASS' if ok else 'FAIL'} (re-run by orch) {short(x['command'], 100)}"
                     + ('' if ok else f" :: {short(x['tail'], 160)}"))
        if not v['tests']:
            L.append('Tests: none required, so no independent evidence')
        if v['mismatch']:
            L.append('WARNING claimed passed but failed on re-run: ' + ', '.join(v['mismatch']))
    if r:
        n = Counter(x['status'] for x in r['tests'])
        L.append('Worker-reported tests: ' + (', '.join(f'{k} {n[k]}' for k in sorted(n)) or 'none'))
        if r['risks']:
            L.append('Risks: ' + '; '.join(short(x, 150) for x in r['risks'][:5]))
        L.append(f"Requires decision: {'YES' if r['requires_decision'] else 'no'} · Worker next: {short(r['next_action'], 200)}")
    if u:
        L.append(f"Codex tokens: in {u.get('input_tokens', 0):,} (cached {u.get('cached_input_tokens', 0):,})"
                 f" · out {u.get('output_tokens', 0):,}")
    L.append(f'Evidence: {task_dir(t)}')
    L.append('Next: ' + NEXT.get(t['state'], 'orch status {id}').format(id=t['id']))
    return '\n'.join(L)


# ---------- commands ----------

def task_here(c, tid, path):
    t, p = get(c, tid), project(c, path)
    if t['project_id'] != p['id']:
        root = c.execute('SELECT root FROM projects WHERE id=?', (t['project_id'],)).fetchone()['root']
        raise Err(f'{tid} belongs to another project ({root}); run orch from there or pass --path')
    return t, p, config(p['root'])


def cmd_init(a):
    c = db()
    p = project(c, a.path, create=True)
    cfg = config(p['root'])
    sync_memory(c, p, cfg)
    print(f"project {p['id']} at {p['root']}\nmemory: {memdir(p, cfg)}\ncheckpoint: {p['checkpoint'][:12]}")


def cmd_new(a):
    c = db()
    p = project(c, a.path, create=True)
    cfg = config(p['root'])
    if not cfg['enabled']:
        raise Err('orchestration is disabled ("enabled": false in config); work directly instead')
    b = {}
    if a.json:
        try:
            b = json.loads(sys.stdin.read() if a.json == '-' else Path(a.json).read_text())
        except ValueError as e:
            raise Err(f'brief is not valid JSON: {e}')
    for flag, key in [('objective', 'objective'), ('context', 'context_reference'), ('priority', 'priority'),
                      ('risk', 'risk_level')]:
        if getattr(a, flag):
            b[key] = getattr(a, flag)
    for flag, key in [('scope', 'scope'), ('constraint', 'constraints'), ('criteria', 'acceptance_criteria'),
                      ('test', 'required_tests')]:
        if getattr(a, flag):
            b[key] = b.get(key, []) + getattr(a, flag)
    if a.approval:
        b['requires_approval'] = True
    errs = check(b, BRIEF_SCHEMA)
    if errs:
        raise Err('invalid brief: ' + '; '.join(errs))
    if not b['objective'].strip():
        raise Err('invalid brief: objective is empty')
    root = p['root']
    b['scope'] = [os.path.relpath(nfc(s), root) if os.path.isabs(s) else s for s in b.get('scope', [])]
    bad = [s for s in b['scope'] + [b.get('context_reference', '.')] if not inside(root, s)]
    if bad:
        raise Err(f'paths outside project {root}: {bad}')
    risk, why = classify(b, cfg)
    risk = max(risk, b.get('risk_level', 'low'), key=LEVELS.index)   # manager may raise risk, never lower it
    key = a.key or hashlib.sha1(json.dumps({k: v for k, v in b.items() if k != 'priority'}, sort_keys=True).encode()).hexdigest()
    dup = c.execute("SELECT * FROM tasks WHERE project_id=? AND idem_key=? AND state NOT IN ('FAILED','CANCELLED')",
                    (p['id'], key)).fetchone()
    if dup:
        print(f"{dup['id']} {dup['state']} (same brief already exists; not duplicated)")
        return
    approval = risk == 'high' or b.get('requires_approval', False)
    with tx(c):
        seq = c.execute('SELECT COALESCE(MAX(seq),0)+1 FROM tasks WHERE project_id=?', (p['id'],)).fetchone()[0]
        tid = f"{p['id']}-{seq}"
        full = {'task_id': tid, 'project_id': p['id'], 'workspace_id': root, 'priority': 'medium', 'scope': [],
                'constraints': [], 'acceptance_criteria': [], 'required_tests': [], 'context_reference': '',
                'requires_approval': approval, **b, 'risk_level': risk}
        full['requires_approval'] = approval
        c.execute('INSERT INTO tasks(id,project_id,seq,idem_key,state,risk,brief,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                  (tid, p['id'], seq, key, 'CREATED', risk, json.dumps(full, ensure_ascii=False), now(), now()))
        log(c, tid, 'manager', None, 'CREATED', short(b['objective'], 200))
        t = move(c, get(c, tid), 'TRIAGED', 'router', f'risk {risk} ({why})')
        t = move(c, t, 'AWAITING_USER_APPROVAL' if approval else 'ASSIGNED', 'router', route(risk, approval))
    sync_memory(c, p, cfg)
    print(f"{tid} {t['state']} risk={risk} route: {route(risk, approval)}")
    if a.run and t['state'] == 'ASSIGNED':
        t = run_task(c, t, p, cfg)
        sync_memory(c, p, cfg)
        print(report(c, t, p, cfg))
    elif t['state'] == 'AWAITING_USER_APPROVAL':
        print(f'Next: {NEXT[t["state"]].format(id=tid)}')


def cmd_run(a):
    c = db()
    t, p, cfg = task_here(c, a.task, a.path)
    if a.detach:
        d = task_dir(t)
        d.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--path', p['root'], 'run', t['id']],
                         cwd=p['root'], stdin=subprocess.DEVNULL, stdout=open(d / 'runner.log', 'a'),
                         stderr=subprocess.STDOUT, start_new_session=True)
        print(f"{t['id']} dispatched in the background; `orch wait {t['id']}` or `orch status {t['id']}`")
        return
    t = run_task(c, t, p, cfg)
    sync_memory(c, p, cfg)
    print(report(c, t, p, cfg))


def cmd_wait(a):
    c = db()
    t, p, cfg = task_here(c, a.task, a.path)
    deadline = time.time() + a.timeout
    while get(c, t['id'])['state'] in ('ASSIGNED', 'RUNNING', 'VALIDATING', 'RETRY_PENDING') and time.time() < deadline:
        time.sleep(3)
        recover(c)
    print(report(c, get(c, t['id']), p, cfg))


def approve(tid, path='.', tty=None):
    """Human gate: needs a person typing at a real terminal. Agent shells have no /dev/tty, so they cannot approve."""
    c = db()
    t, p, cfg = task_here(c, tid, path)
    if t['state'] != 'AWAITING_USER_APPROVAL':
        raise Err(f"{tid} is {t['state']}, not awaiting approval")
    if tty is None:
        try:
            tty = open('/dev/tty', 'r+')
        except OSError:
            raise Err(f'approval needs a human at a real terminal: run `orch approve {tid}` in Terminal '
                      "(or any terminal you control). Agents cannot approve.")
    tty.write(report(c, t, p, cfg) + f'\n\nType the task id ({tid}) to approve it, anything else to abort: ')
    tty.flush()
    if tty.readline().strip() != tid:
        raise Err('not approved')
    with tx(c):
        t = move(c, get(c, tid), 'ASSIGNED', f'human:{getpass.getuser()}@tty', 'approved at terminal')
    note_decision(p, cfg, f'{tid} approved for execution by {getpass.getuser()} (human gate)')
    sync_memory(c, p, cfg)
    return t


def cmd_approve(a):
    t = approve(a.task, a.path)
    print(f"{t['id']} {t['state']}. Next: orch run {t['id']}")


def cmd_review(a):
    c = db()
    t, p, cfg = task_here(c, a.task, a.path)
    actor = os.environ.get('ORCH_ACTOR', 'manager')
    with tx(c):
        t = get(c, t['id'])
        if a.approve:
            if t['state'] != 'REVIEW_PENDING':
                raise Err(f"{t['id']} is {t['state']}, not REVIEW_PENDING")
            t = finalize(c, move(c, t, 'APPROVED', actor, a.note or 'approved after review'), p)
            decision = f"approved: {a.note or 'ok'}"
        elif a.fix:
            if t['state'] == 'BLOCKED':
                t = move(c, t, 'RETRY_PENDING', actor, short(a.fix, 300), attempts=0, pending_prompt=(
                    f"Manager instruction for task {t['id']}: {a.fix}\nContinue, re-run the required tests, "
                    "and reply with the result JSON."))
            elif t['state'] == 'REVIEW_PENDING':
                t = repair(c, t, cfg, f'Manager review requested changes: {a.fix}')
            else:
                raise Err(f"{t['id']} is {t['state']}; --fix needs REVIEW_PENDING or BLOCKED")
            decision = f'fix requested: {a.fix}'
        else:
            if t['state'] not in ('REVIEW_PENDING', 'BLOCKED'):
                raise Err(f"{t['id']} is {t['state']}; --reject needs REVIEW_PENDING or BLOCKED")
            t = move(c, t, 'FAILED' if t['state'] == 'REVIEW_PENDING' else 'CANCELLED', actor, a.reject)
            decision = f'rejected: {a.reject}'
    note_decision(p, cfg, f"{t['id']} {decision} ({actor})")
    sync_memory(c, p, cfg)
    print(f"{t['id']} {t['state']}. Next: {NEXT[t['state']].format(id=t['id'])}")


def cmd_cancel(a):
    c = db()
    t, p, cfg = task_here(c, a.task, a.path)
    if 'CANCELLED' not in FLOW[t['state']]:
        raise Err(f"{t['id']} is {t['state']}; cannot cancel")
    with tx(c):
        t = move(c, get(c, t['id']), 'CANCELLED', os.environ.get('ORCH_ACTOR', 'manager'), a.reason or 'cancelled')
    kill_group(t['worker_pid'])
    sync_memory(c, p, cfg)
    print(f"{t['id']} CANCELLED")


def cmd_status(a):
    c = db()
    if a.task:
        t, p, cfg = task_here(c, a.task, a.path)
        print(report(c, t, p, cfg))
        return
    if a.all:
        rows = c.execute('SELECT * FROM tasks ORDER BY updated DESC LIMIT 100').fetchall()
    else:
        p = project(c, a.path)
        rows = c.execute('SELECT * FROM tasks WHERE project_id=? ORDER BY seq DESC LIMIT 50', (p['id'],)).fetchall()
    for t in rows:
        print(f"{t['id']:<32} {t['state']:<23} {t['risk']:<6} r{t['repairs']} a{t['attempts']} "
              f"{t['updated'][5:16]}  {short(brief(t)['objective'], 60)}")
    if not rows:
        print('no tasks')


def cmd_log(a):
    c = db()
    t, _, _ = task_here(c, a.task, a.path)
    for r in c.execute('SELECT * FROM transitions WHERE task_id=? ORDER BY id', (t['id'],)):
        print(f"{r['ts']}  {r['actor']:<22} {str(r['prev']):<22} -> {r['new']:<22} {short(r['reason'], 140)}")


def cmd_metrics(a):
    c = db()
    if a.all:
        rows = c.execute('SELECT * FROM tasks').fetchall()
    else:
        p = project(c, a.path)
        rows = c.execute('SELECT * FROM tasks WHERE project_id=?', (p['id'],)).fetchall()
    ids = [t['id'] for t in rows]
    q = lambda sql: c.execute(sql.format(','.join('?' * len(ids))), ids).fetchall() if ids else []
    usage = Counter()
    for t in rows:
        usage.update(json.loads(t['usage'] or '{}'))
    tests = Counter(x['status'] for t in rows for x in json.loads(t['verify'] or '{}').get('tests', []))
    durations = [(datetime.fromisoformat(r['done']) - datetime.fromisoformat(r['created'])).total_seconds()
                 for r in q("SELECT t.created, x.ts AS done FROM tasks t JOIN transitions x ON x.task_id=t.id "
                            "WHERE x.new='COMPLETED' AND t.id IN ({})")]
    out = {
        'tasks': len(rows), 'by_state': dict(Counter(t['state'] for t in rows)),
        'worker_dispatches': len(q("SELECT 1 FROM transitions WHERE new='RUNNING' AND task_id IN ({})")),
        'first_run_surveys (full audits)': len(q("SELECT 1 FROM transitions WHERE reason LIKE '%first-run survey%' AND task_id IN ({})")),
        'auto_repairs': sum(t['repairs'] for t in rows), 'infra_retries': sum(t['attempts'] for t in rows),
        'verified_tests': dict(tests), 'codex_tokens': dict(usage),
        'avg_completion_sec': round(sum(durations) / len(durations)) if durations else None,
        'avg_report_tokens_est': round(sum(len(report(c, t, c.execute('SELECT * FROM projects WHERE id=?', (t['project_id'],)).fetchone(),
                                                      config()))
                                           for t in rows) / 4 / len(rows)) if rows else None,
    }
    if a.claude_transcript:
        seen, cu = set(), Counter()
        for line in Path(a.claude_transcript).read_text(errors='replace').splitlines():
            try:
                m = json.loads(line).get('message') or {}
            except (ValueError, AttributeError):
                continue
            if isinstance(m, dict) and m.get('usage') and m.get('id') not in seen:
                seen.add(m.get('id'))
                cu.update({k: v for k, v in m['usage'].items() if isinstance(v, int)})
        out['claude_tokens (transcript)'] = dict(cu)
    print(json.dumps(out, indent=1))


def cmd_doctor(a):
    cfg, ok = config(), True
    b = codex_bin(cfg)
    print(f"orch home: {HOME} · enabled: {cfg['enabled']}")
    if b:
        v = subprocess.run([b, '--version'], capture_output=True, text=True).stdout.strip()
        login = subprocess.run([b, 'login', 'status'], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        print(f"codex worker: {b} ({v})")
        print(f"codex login: {(login.stdout + login.stderr).strip().splitlines()[0] if (login.stdout + login.stderr).strip() else '?'}")
        ok = login.returncode == 0
    else:
        print('codex worker: NOT FOUND (install Codex: `npm install -g @openai/codex` or `brew install codex`)')
        ok = False
    for f in [Path.home() / '.claude/CLAUDE.md', Path.home() / '.codex/AGENTS.md']:
        print(f"policy block in {f}: {'yes' if f.exists() and BEGIN in f.read_text() else 'no'}")
    print(f"manager skill: {'yes' if (Path.home() / '.claude/skills/orchestrate/SKILL.md').exists() else 'no'}")
    sys.exit(0 if ok else 1)


# ---------- install / uninstall ----------

def backup(f):
    if f.exists():
        (HOME / 'backups').mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, HOME / 'backups' / f"{f.parent.name.strip('.')}-{f.name}.{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}")


def strip_block(s):
    return re.sub(re.escape(BEGIN) + r'.*?' + re.escape(END) + r'\n?', '', s, flags=re.S)


def put_block(f, text):
    f.parent.mkdir(parents=True, exist_ok=True)
    old = f.read_text() if f.exists() else ''
    backup(f)
    base = strip_block(old).rstrip('\n')
    f.write_text((base + '\n\n' if base.strip() else '') + f'{BEGIN}\n{text.strip()}\n{END}\n')


def cmd_install(a):
    src, pol = Path(__file__).resolve(), HOME / 'policies'
    (HOME / 'bin').mkdir(parents=True, exist_ok=True)
    if src != (HOME / 'bin' / 'orch').resolve():        # installing from a source checkout
        shutil.copy2(src, HOME / 'bin' / 'orch')
        shutil.copytree(src.parent / 'policies', pol, dirs_exist_ok=True)
        if (src.parent / 'README.md').exists():
            shutil.copy2(src.parent / 'README.md', HOME / 'README.md')
    (HOME / 'bin' / 'orch').chmod(0o755)
    if not (HOME / 'config.json').exists():
        (HOME / 'config.json').write_text(json.dumps(DEFAULTS, indent=1) + '\n')
    link = Path.home() / '.local/bin/orch'
    if link.is_symlink() or not link.exists():
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(HOME / 'bin' / 'orch')
    else:
        print(f'note: {link} exists and is not a symlink; left untouched')
    skill = Path.home() / '.claude/skills/orchestrate/SKILL.md'
    skill.parent.mkdir(parents=True, exist_ok=True)
    backup(skill)
    shutil.copy2(pol / 'claude-manager-skill.md', skill)
    put_block(Path.home() / '.claude/CLAUDE.md', (pol / 'claude-pointer.md').read_text())
    put_block(Path.home() / '.codex/AGENTS.md', (pol / 'codex-worker.md').read_text())
    print(f'installed: {HOME / "bin/orch"} (+ {link}), manager skill, CLAUDE.md + AGENTS.md policy blocks. '
          f'Backups: {HOME / "backups"}')


def cmd_uninstall(a):
    for f in [Path.home() / '.claude/CLAUDE.md', Path.home() / '.codex/AGENTS.md']:
        if f.exists() and BEGIN in f.read_text():
            backup(f)
            f.write_text(strip_block(f.read_text()).rstrip('\n') + '\n' if strip_block(f.read_text()).strip() else '')
    skill = Path.home() / '.claude/skills/orchestrate'
    if (skill / 'SKILL.md').exists():
        backup(skill / 'SKILL.md')
        shutil.rmtree(skill)
    link = Path.home() / '.local/bin/orch'
    if link.is_symlink() and Path(os.readlink(link)) == HOME / 'bin' / 'orch':
        link.unlink()
    print(f'removed policy blocks, skill and symlink. Task state and config kept in {HOME} (delete it yourself if unwanted).')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='orch', description='Codex-first orchestrator: Claude manages, Codex works.')
    ap.add_argument('--path', default='.', help='project directory (default: cwd)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sp = lambda name, fn, h: sub.add_parser(name, help=h).set_defaults(fn=fn) or sub.choices[name]
    sp('init', cmd_init, 'register the project and create its memory skeleton')
    n = sp('new', cmd_new, 'create + triage a task from a structured brief')
    n.add_argument('--objective'); n.add_argument('--scope', action='append'); n.add_argument('--constraint', action='append')
    n.add_argument('--criteria', action='append'); n.add_argument('--test', action='append', help='required test command')
    n.add_argument('--priority', choices=LEVELS); n.add_argument('--risk', choices=LEVELS, help='raise risk (never lowers)')
    n.add_argument('--context', help='context reference path inside the project')
    n.add_argument('--approval', action='store_true', help='require human approval')
    n.add_argument('--json', help='brief JSON file, or - for stdin'); n.add_argument('--key', help='idempotency key')
    n.add_argument('--run', action='store_true', help='dispatch immediately when no approval is needed')
    r = sp('run', cmd_run, 'dispatch to Codex, validate, auto-repair'); r.add_argument('task')
    r.add_argument('--detach', action='store_true', help='run in a background process that survives this session')
    w = sp('wait', cmd_wait, 'wait for a running task'); w.add_argument('task'); w.add_argument('--timeout', type=int, default=540)
    sp('approve', cmd_approve, 'HUMAN ONLY: approve a high-risk task at a terminal').add_argument('task')
    v = sp('review', cmd_review, 'manager decision on a reviewed task'); v.add_argument('task')
    g = v.add_mutually_exclusive_group(required=True)
    g.add_argument('--approve', action='store_true'); g.add_argument('--fix', metavar='INSTRUCTION'); g.add_argument('--reject', metavar='REASON')
    v.add_argument('--note')
    x = sp('cancel', cmd_cancel, 'cancel a task (kills its worker)'); x.add_argument('task'); x.add_argument('--reason')
    s = sp('status', cmd_status, 'list tasks, or compressed report of one'); s.add_argument('task', nargs='?')
    s.add_argument('--all', action='store_true', help='all projects')
    sp('report', cmd_status, 'compressed report of one task').add_argument('task')
    sp('log', cmd_log, 'audit trail of state transitions').add_argument('task')
    m = sp('metrics', cmd_metrics, 'usage and quality metrics'); m.add_argument('--all', action='store_true')
    m.add_argument('--claude-transcript', help='Claude Code JSONL transcript to sum Claude token usage from')
    sp('doctor', cmd_doctor, 'check worker availability and global policy install')
    sp('install', cmd_install, 'install globally (backs up files it touches)')
    sp('uninstall', cmd_uninstall, 'remove global policy blocks, skill and symlink (keeps state)')
    a = ap.parse_args(argv)
    if a.cmd == 'report':
        a.all = False
    try:
        if a.cmd not in ('install', 'uninstall', 'doctor'):
            recover(db())
        a.fn(a)
    except Err as e:
        print(f'orch: error: {e}', file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
