#!/usr/bin/env python3
"""Read-only local dashboard for running Claude Code and Codex sessions (127.0.0.1:4990).

HUB_DIR picks the hub folder (default: the current folder) and PORT the port (default 4990).

Nothing here writes to sessions or stores transcript content; every API call
reads `claude agents`, `claude logs`, /proc or the session .jsonl and answers in memory.
"""
import glob
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST, PORT = '127.0.0.1', int(os.environ.get('PORT', 4990))
ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.expanduser('~/.claude/projects')
JOBS = os.path.expanduser('~/.claude/jobs')
HUB = os.path.abspath(os.path.expanduser(os.environ.get('HUB_DIR') or '.'))   # hub folder; the four files below are read only
ROSTER = os.path.join(HUB, 'agents.json')   # hub's roster
DECISIONS = os.path.join(HUB, 'decisions.json')   # hub's decision list
STATUS_MD = os.path.join(HUB, 'STATUS.md')   # hub's status board
DEV = os.path.join(HUB, 'dev-occupancy.json')   # hub's list of who holds the dev stack
CLAUDE = shutil.which('claude') or 'claude'   # Windows: finds claude.exe or npm's claude.cmd through PATHEXT
DEV_STALE = 2 * 3600   # seconds; an older updated_at is shown faded
TASKS = os.path.expanduser('~/.claude/tasks')   # sessions' todo lists, <sessionId>/<id>.json, read only
TODOS = os.path.expanduser('~/.claude/todos')   # older todo format, <sessionId>-agent-<agentId>.json, read only
TODO_STALE = 7 * 86400   # seconds; todo groups untouched this long are hidden until asked for
CODEX = os.path.expanduser('~/.codex')
USAGE = os.path.expanduser('~/.claude/usage.json')   # rate_limits copied by ~/.claude/statusline-tee.sh
USAGE_STALE = 30 * 60   # seconds; older readings are shown as 갱신 안 됨
CODEX_HOURS = float(os.environ.get('CODEX_HOURS', 24))   # codex sessions older than this (by file mtime) are left out
STATIC = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/vendor/xterm.js': ('vendor/xterm.js', 'text/javascript; charset=utf-8'),
    '/vendor/xterm.css': ('vendor/xterm.css', 'text/css; charset=utf-8'),
    '/vendor/WantedSansVariable.woff2': ('vendor/WantedSansVariable.woff2', 'font/woff2'),
}
MAX_EVENTS = 500       # events sent on the first read (30 MB parses in ~0.2 s)
MAX_MESSAGES = 300     # newest messages sent by /api/messages; edge counts still use all of them
CLIP = 4000            # max chars per text field sent to the browser

_cache = {'at': 0, 'agents': []}
_lock = threading.Lock()
_codex = {}   # rollout path -> what codex_scan() has read so far
_claude = {}  # transcript path -> what claude_scan() has read so far
_scan_lock = threading.Lock()
_usage = {}   # usage source path -> ((mtime_ns, size), parsed reading)


def transcript_path(sid, cwd):
    p = os.path.join(PROJECTS, re.sub(r'[^A-Za-z0-9]', '-', cwd or ''), sid + '.jsonl')
    if os.path.exists(p):
        return p
    hits = glob.glob(os.path.join(PROJECTS, '*', glob.escape(sid) + '.jsonl'))
    return hits[0] if hits else None


SUBAGENT_BUSY_S = 300   # an unfinished sub-agent whose transcript changed within this many seconds counts as working


def subagents_of(sid):
    """Sub-agents (Agent tool) a session started: <project>/<sid>/subagents/agent-*.meta.json + .jsonl."""
    out = []
    if not sid:
        return out
    now = time.time()
    for meta in glob.glob(os.path.join(PROJECTS, '*', glob.escape(sid), 'subagents', 'agent-*.meta.json')):
        try:
            with open(meta, encoding='utf-8') as f:
                m = json.load(f)
            tr = meta[:-len('.meta.json')] + '.jsonl'
            at = os.path.getmtime(tr) if os.path.exists(tr) else os.path.getmtime(meta)
        except (OSError, ValueError):
            continue
        finished = bool(m.get('finished'))
        out.append({'id': os.path.basename(meta)[len('agent-'):-len('.meta.json')], 'type': str(m.get('agentType') or ''),
                    'desc': str(m.get('description') or ''), 'at': int(at * 1000),
                    'state': 'done' if finished else 'busy' if now - at < SUBAGENT_BUSY_S else 'idle'})
    return sorted(out, key=lambda s: -s['at'])


def job_state(job_id):
    try:
        with open(os.path.join(JOBS, job_id, 'state.json'), encoding='utf-8') as f:
            s = json.load(f)
        return {'detail': s.get('detail') or '', 'needs': s.get('needs') or ''}
    except (OSError, ValueError):
        return {}


def roster(path=ROSTER):
    """name -> {group, lane, task, order}; a missing or malformed file means everyone is 'other'."""
    out = {}
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        for group in ('resident', 'task'):
            for i, (name, v) in enumerate((d.get(group) or {}).items()):
                if isinstance(v, dict):
                    out[name] = {'group': group, 'lane': str(v.get('lane') or ''),
                                 'task': str(v.get('task') or ''), 'order': i}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}
    return out


SECTIONS = ('busy', 'wait', 'done')    # 진행 중, 입력 대기, 종료 (top to bottom)
KIND_ORDER = {'task': 0, 'resident': 1, 'other': 2}


def section(a):
    if a.get('status') == 'busy' or a.get('state') == 'working':
        return 'busy'
    if a.get('state') == 'done':      # finished its turn, or completed (only listed with --all)
        return 'done'
    return 'wait'                     # blocked on a person, or an interactive session waiting for the next prompt


def origin(a):
    """team: agents.json 에 있는 허브 팀, other: 그 밖의 이 PC 세션."""
    return 'team' if a.get('group') in ('resident', 'task') else 'other'


def arrange(rows):
    """Sort by section, then task / resident / other, then most recent activity first."""
    for a in rows:
        a['section'] = section(a)
        a['origin'] = origin(a)
    return sorted(rows, key=lambda a: (SECTIONS.index(a['section']), KIND_ORDER[a.get('group', 'other')],
                                       -(a.get('lastActivity') or a.get('startedAt') or 0)))


def codex_live(root=CODEX):
    """session id -> pid for this user's codex processes holding a rollout or thread-writer lock open.

    Only processes named codex* count, so a reader (tail, grep, this server) never makes a session look alive.
    """
    out = {}
    for proc in glob.glob('/proc/[0-9]*'):
        try:
            if os.stat(proc).st_uid != os.getuid() or int(proc[6:]) == os.getpid():
                continue
            with open(proc + '/comm') as f:
                if not f.read().startswith('codex'):
                    continue
            fds = os.listdir(proc + '/fd')
        except OSError:
            continue
        for fd in fds:
            try:
                t = os.readlink(f'{proc}/fd/{fd}')
            except OSError:
                continue
            if t.startswith(root + '/sessions/') and t.endswith('.jsonl'):
                out[t[-42:-6]] = int(proc[6:])
            elif t.startswith(root + '/thread-writer-locks/') and t.endswith('.lock'):
                out[os.path.basename(t)[:-5]] = int(proc[6:])
    return out


def codex_text(content):
    parts = []
    for c in content or []:
        if c.get('type') in ('text', 'Text'):
            parts.append(c.get('text', ''))
        elif c.get('type') == 'local_image':
            parts.append('[이미지]')
        elif c.get('type') == 'mention':
            parts.append('@' + str(c.get('name', '')))
    return '\n'.join(parts)


def codex_scan(path):
    """Session meta, whether a turn is open, the latest prompt and rate limits; reads only new bytes."""
    with _scan_lock:   # agents() and /api/usage both advance the same read position
        s = _codex.get(path)
        size = os.path.getsize(path)
        if not s or size < s['pos']:
            s = _codex[path] = {'pos': 0, 'meta': None, 'turn': False, 'ask': '', 'limits': None, 'tokens': None}
        with open(path, 'rb') as f:
            f.seek(s['pos'])
            data = f.read(size - s['pos'])
        end = data.rfind(b'\n') + 1   # skip a line still being written
        for line in data[:end].splitlines():
            head = line[:200]   # records start {"timestamp":…,"ordinal":…,"type":…}; skip the bulky kinds unparsed
            if b'"event_msg"' not in head and not (s['meta'] is None and b'"session_meta"' in head):
                continue
            try:
                d = json.loads(line)
                p = d.get('payload') or {}
                if d.get('type') == 'session_meta' and s['meta'] is None:
                    s['meta'] = p
                if d.get('type') != 'event_msg':
                    continue
                t = p.get('type')
                if t == 'task_started':
                    s['turn'] = True
                elif t in ('task_complete', 'turn_aborted'):
                    s['turn'] = False
                elif t == 'item_completed' and (p.get('item') or {}).get('type') == 'UserMessage':
                    s['ask'] = codex_text(p['item'].get('content')).strip()[:300]
                elif t == 'token_count':
                    if isinstance(p.get('rate_limits'), dict):
                        s['limits'] = codex_limits(p['rate_limits'], d.get('timestamp'))
                    if isinstance((p.get('info') or {}).get('total_token_usage'), dict):   # info can be null or {}
                        s['tokens'] = p['info']['total_token_usage']
            except (ValueError, AttributeError, TypeError, KeyError):
                continue
        s['pos'] += end
        return s


def codex_rows(root=CODEX, live=None, now=None, hours=CODEX_HOURS):
    """Codex sessions touched in the last `hours`, shaped like `claude agents` rows plus engine/path."""
    live = codex_live(root) if live is None else live
    now = time.time() if now is None else now
    names = {}
    try:
        with open(os.path.join(root, 'session_index.jsonl'), encoding='utf-8') as f:
            for line in f:
                try:
                    d = json.loads(line)
                    names[d['id']] = d.get('thread_name') or ''
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    rows = []
    for p in glob.glob(os.path.join(root, 'sessions', '*', '*', '*', 'rollout-*.jsonl')):
        mtime = os.path.getmtime(p)
        if now - mtime > hours * 3600:
            _codex.pop(p, None)
            continue
        s = codex_scan(p)
        m = s['meta']
        if not isinstance(m, dict) or isinstance(m.get('source'), dict):
            continue   # no meta yet, or a subagent thread (its parent session is listed)
        sid = str(m.get('id') or p[-42:-6])
        pid = live.get(sid)
        state = 'done' if not pid else 'working' if s['turn'] else 'idle'
        try:
            started = iso_ms(m.get('timestamp'))
        except ValueError:
            started = None
        # codex ids are UUIDv7: the head is the start time, so sessions started together share it; use the tail
        rows.append({'engine': 'codex', 'sessionId': sid, 'name': names.get(sid) or 'codex ' + sid[-8:],
                     'cwd': m.get('cwd') or '', 'kind': 'codex', 'source': str(m.get('source') or ''),
                     'originator': str(m.get('originator') or ''), 'version': str(m.get('cli_version') or ''),
                     'pid': pid, 'state': state, 'status': 'busy' if state == 'working' else 'idle',
                     'startedAt': started, 'lastActivity': int(mtime * 1000), 'detail': s['ask'],
                     'group': 'other', 'hasTranscript': True, 'path': p, 'tokens': codex_tokens(s['tokens'])})
    return rows


def codex_tokens(u):
    """처리 토큰 from the last total_token_usage: input less its cached part, plus output (reasoning is inside output)."""
    if not u:
        return None
    n = lambda k: int(u.get(k) or 0)
    fresh = n('input_tokens') - n('cached_input_tokens')
    return {'total': fresh + n('output_tokens'), 'input': fresh, 'cacheRead': n('cached_input_tokens'),
            'output': n('output_tokens'), 'reasoning': n('reasoning_output_tokens'), 'engine': 'codex'}


def iso_ms(ts):
    return int(datetime.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp() * 1000)


def window(key, minutes, used, resets):
    """One rate-limit window; resets_at comes in epoch seconds from both engines."""
    return {'key': key, 'minutes': minutes, 'used': float(used),
            'resets': int(resets * 1000) if isinstance(resets, (int, float)) else None}


def codex_limits(r, ts):
    wins = [window(k, w.get('window_minutes'), w['used_percent'], w.get('resets_at'))
            for k in ('primary', 'secondary') if isinstance(w := r.get(k), dict)
            and isinstance(w.get('used_percent'), (int, float))]
    return {'at': iso_ms(ts), 'plan': str(r.get('plan_type') or ''), 'windows': wins}


CLAUDE_WINDOWS = {'five_hour': 300, 'seven_day': 10080}


def claude_limits(path):
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    wins = [window(k, CLAUDE_WINDOWS.get(k), w['used_percentage'], w.get('resets_at'))
            for k, w in d['rate_limits'].items() if isinstance(w, dict)
            and isinstance(w.get('used_percentage'), (int, float))]
    return {'at': int(d['at']), 'windows': wins}


def by_stat(path, parse):
    """parse(path), reused while the file's mtime and size stay the same (the dashboard polls every 3 s)."""
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    c = _usage.get(path)
    if not c or c[0] != key:
        c = _usage[path] = (key, parse(path))
    return c[1]


def codex_usage(root=CODEX):
    """The newest token_count rate_limits across rollouts, None if no rollout has one."""
    paths = sorted(((os.path.getmtime(p), p) for p in glob.glob(os.path.join(root, 'sessions', '*', '*', '*', 'rollout-*.jsonl'))),
                   reverse=True)
    best = None
    for mtime, p in paths:
        if best and mtime * 1000 < best['at']:
            break   # nothing in an older file can be newer than what we have
        r = by_stat(p, lambda p: codex_scan(p)['limits'])
        if r and (not best or r['at'] > best['at']):
            best = r
    return best


def usage_of(get, now):
    """missing / broken / stale / ok, with windows past their reset flagged (their used % no longer holds)."""
    try:
        r = get()
    except FileNotFoundError:
        return {'state': 'missing'}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return {'state': 'broken', 'error': str(e)[:200] or type(e).__name__}
    if not r:
        return {'state': 'missing'}
    return dict(r, state='stale' if now - r['at'] > USAGE_STALE * 1000 else 'ok',
                windows=[dict(w, reset=bool(w['resets'] and w['resets'] <= now)) for w in r['windows']])


def usage(claude=USAGE, codex=CODEX, now=None):
    now = int(time.time() * 1000) if now is None else now
    return {'now': now, 'claude': usage_of(lambda: by_stat(claude, claude_limits), now),
            'codex': usage_of(lambda: codex_usage(codex), now)}


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def dev_occupancy(path=DEV, now=None):
    """missing / broken / stale / ok; items keep the file's strings plus since and until in ms (None if not a time)."""
    now = int(time.time() * 1000) if now is None else now
    try:
        d = by_stat(path, load_json)
        items = d['items']
        if not isinstance(items, list):
            raise TypeError('items 가 목록이 아님')
    except FileNotFoundError:
        return {'state': 'missing', 'error': '파일 없음'}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return {'state': 'broken', 'error': str(e)[:200] or type(e).__name__}
    out = []
    for i in items:
        if isinstance(i, dict):
            x = {k: str(i.get(k) or '') for k in ('who', 'kind', 'what', 'since', 'until', 'note')}
            out.append(dict(x, sinceAt=ms_or_none(x['since']), untilAt=ms_or_none(x['until'])))
    out.sort(key=lambda x: x['kind'] != 'restart')   # a planned restart is the one to see first
    at = ms_or_none(d.get('updated_at'))
    return {'state': 'ok' if at and now - at <= DEV_STALE * 1000 else 'stale', 'updatedAt': at, 'items': out}


def claude_tokens(path):
    """처리 토큰 = input + cache write + output, once per API message, over the session's transcript and its
    sub-agents' (<sid>/subagents/agent-*.jsonl beside it). Cache reads are kept apart and stay out of the total."""
    if not path:
        return None
    sid = os.path.basename(path)[:-len('.jsonl')]
    subs = glob.glob(os.path.join(os.path.dirname(path), glob.escape(sid), 'subagents', 'agent-*.jsonl'))
    u = [0, 0, 0, 0]
    for p in [path] + subs:
        try:
            for v in claude_scan(p)['usage']:
                u = [a + b for a, b in zip(u, v)]
        except OSError:
            continue
    i, o, cr, cw = u
    return {'total': i + cw + o, 'input': i, 'output': o, 'cacheRead': cr, 'cacheWrite': cw,
            'subagents': len(subs), 'engine': 'claude'}


def agents():
    with _lock:
        if time.time() - _cache['at'] < 2:
            return _cache['agents']
        out = subprocess.run([CLAUDE, 'agents', '--json', '--all'],
                             capture_output=True, encoding='utf-8', errors='replace', timeout=15).stdout
        rows = json.loads(out or '[]')
        names = roster()
        for a in rows:
            a.update(names.get(a.get('name'), {'group': 'other'}))
            p = transcript_path(a.get('sessionId', ''), a.get('cwd'))
            a['hasTranscript'] = bool(p)
            a['subagents'] = subagents_of(a.get('sessionId'))
            a['lastActivity'] = int(os.path.getmtime(p) * 1000) if p else None
            if a.get('id'):
                a.update(job_state(a['id']))
            a['tokens'] = claude_tokens(p)
        try:
            rows += codex_rows()
        except Exception as e:   # codex trouble must never take the Claude list down with it
            print('codex:', e, flush=True)
        rows = arrange(rows)
        _cache.update(at=time.time(), agents=rows)
        return rows


def load_decisions(path=DECISIONS):
    """(items, error); a missing or malformed file gives no items and a message."""
    try:
        with open(path, encoding='utf-8') as f:
            items = json.load(f).get('items')
        if not isinstance(items, list):
            return [], 'items 가 목록이 아님'
        return [i for i in items if isinstance(i, dict)], ''
    except OSError:
        return [], '파일 없음'
    except (ValueError, AttributeError) as e:
        return [], '읽지 못함: ' + str(e)[:200]


def merge_decisions(items, rows, names):
    """Hub decisions plus live blocked sessions' needs -> now / waiting / decided, each in display order."""
    live = {a.get('name') for a in rows}

    def who(name):
        r = names.get(name)
        if r:
            return r
        # residents are always in the roster and task sessions leave it when closed,
        # so a source that is neither listed nor running is taken as a closed one-off session
        return {'group': 'other'} if name in live else {'group': 'task', 'closed': True}

    def lst(v):
        return [str(x) for x in v] if isinstance(v, list) else [str(v)] if v else []

    out = {'now': [], 'waiting': [], 'decided': []}
    for a in rows:
        if a.get('state') == 'blocked' and a.get('needs'):
            out['now'].append({'live': True, 'title': a['needs'], 'source': a.get('name') or '',
                               'sid': a.get('sessionId'), 'at': a.get('lastActivity') or a.get('startedAt'),
                               'task': names.get(a.get('name'), {}).get('task', ''), 'who': who(a.get('name'))})
    for i in items:
        d = {k: str(i[k]) for k in ('id', 'title', 'detail', 'source', 'task', 'raised', 'until', 'decision', 'decided_at')
             if i.get(k) not in (None, '')}
        d.update(options=lst(i.get('options')), refs=lst(i.get('refs')), who=who(d.get('source', '')))
        st = i.get('status')
        out[st if st in ('waiting', 'decided') else 'now'].append(d)   # unknown status still needs a look
    # sessions asking right now first (latest activity first), then open items newest raised first
    out['now'].sort(key=lambda d: d.get('raised', ''), reverse=True)
    out['now'].sort(key=lambda d: (not d.get('live'), -(d.get('at') or 0)))
    # dated deadlines first (soonest), then open-ended ones; newest raised first within each
    out['waiting'].sort(key=lambda d: d.get('raised', ''), reverse=True)
    out['waiting'].sort(key=lambda d: (0, d['until']) if re.match(r'\d{4}-\d\d-\d\d', d.get('until', '')) else (1, ''))
    out['decided'].sort(key=lambda d: d.get('decided_at', ''), reverse=True)
    return out


TODO_ORDER = {'in_progress': 0, 'pending': 1, 'completed': 2}


def todo_item(d):
    t = {k: str(d.get(k) or '') for k in ('id', 'subject', 'description', 'activeForm', 'status', 'owner')}
    t['subject'] = t['subject'] or str(d.get('content') or '')   # the older todos format calls it content
    b = d.get('blockedBy')
    t['blockedBy'] = [str(x) for x in b] if isinstance(b, list) else []
    return t


def todos(rows=(), tasks=TASKS, legacy=TODOS, now=None):
    """Sessions' todo lists (<tasks>/<sid>/<id>.json, and <legacy>/<sid>-agent-<id>.json lists), grouped by session.
    Groups are newest change (file mtime) first; items in progress, pending, completed, then by numeric id."""
    now = time.time() if now is None else now
    found = {}   # sid -> [newest mtime, items]

    def add(sid, path, d):
        items = [todo_item(x) for x in (d if isinstance(d, list) else [d]) if isinstance(x, dict)]
        if items:
            g = found.setdefault(sid, [0, []])
            g[0] = max(g[0], os.path.getmtime(path))
            g[1] += items

    for p in glob.glob(os.path.join(glob.escape(tasks), '*', '*.json')):
        try:
            add(os.path.basename(os.path.dirname(p)), p, load_json(p))
        except (OSError, ValueError):
            continue   # half written or just removed; the next poll picks it up
    for p in glob.glob(os.path.join(glob.escape(legacy), '*.json')):
        try:
            add(os.path.basename(p)[:-len('.json')].split('-agent-')[0], p, load_json(p))
        except (OSError, ValueError):
            continue
    live = {a.get('sessionId'): a for a in rows}
    groups, counts = [], {'in_progress': 0, 'pending': 0}
    for sid, (mtime, items) in found.items():
        items.sort(key=lambda t: (TODO_ORDER.get(t['status'], 1),   # unknown status sits with pending
                                  (0, int(t['id']), '') if t['id'].isdecimal() else (1, 0, t['id'])))
        a = live.get(sid) or {}
        g = {'sid': sid, 'name': a.get('name') or sid[:8], 'closed': not a, 'at': int(mtime * 1000),
             'stale': now - mtime > TODO_STALE, 'items': items,
             **{k: a.get(k) for k in ('state', 'status', 'kind', 'engine', 'group', 'lane', 'task') if a.get(k)}}
        if not g['stale']:
            for k in counts:
                counts[k] += sum(t['status'] == k for t in items)
        groups.append(g)
    groups.sort(key=lambda g: -g['at'])
    return {'groups': groups, 'counts': counts}


def clip(s, n=CLIP):
    s = s or ''
    return s if len(s) <= n else s[:n] + f'\n… ({len(s) - n}자 생략)'


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if b.get('type') == 'text':
            parts.append(b.get('text', ''))
        elif b.get('type') == 'image':
            parts.append('[이미지]')
    return '\n'.join(parts)


def tag(s, name):
    m = re.search(rf'<{name}>(.*?)</{name}>', s, re.S)
    return m.group(1).strip() if m else ''


PEER = re.compile(r'<cross-session-message([^>]*)>(.*?)(?:</cross-session-message>|$)', re.S)


def prompt_event(s, origin, meta, ts):
    """Classify a user prompt (user record or mid-turn queued_command)."""
    kind = (origin or {}).get('kind')
    s = s.strip()
    if kind == 'peer':
        m = PEER.search(s)
        attrs, body = (m.group(1), m.group(2)) if m else ('', s)
        frm = re.search(r'from-name="([^"]*)"', attrs)
        return {'k': 'peer', 't': ts, 'from': frm.group(1) if frm else '?', 'text': clip(body.strip())}
    if kind == 'task-notification' or s.startswith('<task-notification>'):
        return {'k': 'note', 't': ts, 'text': '작업 알림: ' + (tag(s, 'summary') or tag(s, 'status'))[:300]}
    if meta or s.startswith('<local-command') or s.startswith('<system-reminder>'):
        return None
    if '<command-name>' in s:
        return {'k': 'user', 't': ts, 'text': (tag(s, 'command-name') + ' ' + tag(s, 'command-args')).strip()}
    if s.startswith('[Request interrupted'):
        return {'k': 'note', 't': ts, 'text': '사용자가 중단함'}
    if kind not in (None, 'human'):
        return {'k': 'note', 't': ts, 'text': clip(s, 300)}
    return {'k': 'user', 't': ts, 'text': clip(s)}


def summarize(inp):
    for k in ('description', 'file_path', 'pattern', 'command', 'url', 'query',
              'skill', 'action', 'prompt', 'path', 'notebook_path'):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0][:160]
    return ''


def events_of(d):
    t, ts = d.get('type'), d.get('timestamp')
    if t == 'attachment':
        a = d.get('attachment') or {}
        if a.get('type') == 'queued_command' and isinstance(a.get('prompt'), str):
            e = prompt_event(a['prompt'], a.get('origin'), False, a.get('timestamp') or ts)
            return [e] if e else []
        return []
    if t == 'system' and d.get('subtype') == 'compact_boundary':
        return [{'k': 'note', 't': ts, 'text': '대화가 요약되어 이어짐'}]
    if t not in ('user', 'assistant'):
        return []
    content = (d.get('message') or {}).get('content')
    if t == 'user':
        if isinstance(content, list) and any(b.get('type') == 'tool_result' for b in content):
            return [{'k': 'result', 't': ts, 'id': b.get('tool_use_id'), 'err': bool(b.get('is_error')),
                     'text': clip(text_of(b.get('content')), 2000)}
                    for b in content if b.get('type') == 'tool_result']
        e = prompt_event(text_of(content), d.get('origin'), d.get('isMeta'), ts)
        return [e] if e else []
    out = []
    for b in content if isinstance(content, list) else []:
        if b.get('type') == 'text' and b.get('text', '').strip():
            out.append({'k': 'reply', 't': ts, 'text': clip(b['text'])})
        elif b.get('type') == 'tool_use':
            inp = b.get('input') or {}
            if b.get('name') == 'SendMessage':
                out.append({'k': 'send', 't': ts, 'id': b.get('id'), 'to': str(inp.get('to', '')),
                            'sum': str(inp.get('summary', ''))[:200], 'text': clip(str(inp.get('message', '')))})
            else:
                out.append({'k': 'tool', 't': ts, 'id': b.get('id'), 'name': b.get('name'),
                            'sum': summarize(inp), 'text': clip(json.dumps(inp, ensure_ascii=False, indent=1), 2000)})
    return out


def codex_events_of(d):
    """One codex rollout record -> feed events, from the item_completed layer (what the codex UI shows)."""
    p, ts = d.get('payload') or {}, d.get('timestamp')
    if d.get('type') != 'event_msg':
        return []
    t = p.get('type')
    if t == 'turn_aborted':
        return [{'k': 'note', 't': ts, 'text': '턴 중단: ' + str(p.get('reason') or '')}]
    if t == 'task_complete' and p.get('error'):
        return [{'k': 'note', 't': ts, 'text': '턴 오류: ' + clip(str(p['error']), 300)}]
    if t != 'item_completed':
        return []
    it = p.get('item') or {}
    k, iid = it.get('type'), it.get('id')
    if k == 'UserMessage':
        return [{'k': 'user', 't': ts, 'text': clip(codex_text(it.get('content')).strip())}]
    if k == 'AgentMessage':
        text = codex_text(it.get('content')).strip()
        return [{'k': 'reply', 't': ts, 'text': clip(text)}] if text else []
    if k == 'Reasoning':
        return []   # encrypted
    if k == 'ContextCompaction':
        return [{'k': 'note', 't': ts, 'text': '대화가 요약되어 이어짐'}]
    if k == 'SubAgentActivity':
        return [{'k': 'note', 't': ts, 'text': f"하위 에이전트 {it.get('agent_path', '')} {it.get('kind', '')}"}]
    # the rest are tool calls that already finished: send the call and its result together
    err = it.get('status') not in (None, 'completed')
    if k == 'CommandExecution':
        cmd = it.get('command')
        inp = cmd[-1] if isinstance(cmd, list) and cmd else str(cmd or '')
        name, sm, out = 'shell', summarize({'command': inp}), it.get('aggregated_output') or (it.get('stdout') or '') + (it.get('stderr') or '')
        err = err or it.get('exit_code') not in (None, 0)
    elif k == 'McpToolCall':
        inp = it.get('arguments') or {}
        name, sm = f"{it.get('server', '')}.{it.get('tool', '')}", summarize(inp) if isinstance(inp, dict) else ''
        out = text_of((it.get('result') or {}).get('content')) if isinstance(it.get('result'), dict) else ''
        out = out or str(it.get('error') or '')
    elif k == 'FileChange':
        ch = it.get('changes') if isinstance(it.get('changes'), dict) else {}
        inp = {path: (c or {}).get('type') for path, c in ch.items()}
        name, sm, out = 'FileChange', ', '.join(os.path.basename(x) for x in ch)[:160], (it.get('stderr') or it.get('stdout') or '')
    else:
        inp = {x: v for x, v in it.items() if x not in ('type', 'id')}
        name, sm, out = str(it.get('kind') or it.get('tool') or k), str(it.get('query') or it.get('path') or '')[:160], ''
    text = inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False, indent=1)
    return [{'k': 'tool', 't': ts, 'id': iid, 'name': name, 'sum': sm, 'text': clip(text, 2000)},
            {'k': 'result', 't': ts, 'id': iid, 'err': bool(err), 'text': clip(str(out), 2000)}]


def read_transcript(path, start, parse=events_of):
    """Parse complete lines from byte `start`; start < 0 means the first read."""
    size = os.path.getsize(path)
    first = start < 0 or start > size
    pos = 0 if first else start
    with open(path, 'rb') as f:
        f.seek(pos)
        data = f.read(size - pos)
    end = data.rfind(b'\n') + 1   # skip a line still being written
    events = []
    for line in data[:end].splitlines():
        try:
            events.extend(parse(json.loads(line)))
        except (ValueError, AttributeError, TypeError):
            continue
    truncated = first and len(events) > MAX_EVENTS
    if first:
        events = events[-MAX_EVENTS:]
    return {'offset': pos + end, 'reset': first, 'truncated': truncated, 'events': events}


def ms_or_none(ts):
    try:
        return iso_ms(ts)
    except (ValueError, TypeError):
        return None


# lines worth parsing: usage, SendMessage calls, their results and peer receipts (msg_id), the session name
SCAN_KEYS = (b'"usage"', b'"SendMessage"', b'msg_id', b'cross-session-message', b'"agent-name"')
SCAN_SKIP = (b'{"type":"api-request', b'{"type":"queue-operation')   # request copies and queue echoes of the same messages
USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')


def peer_receipt(s, origin, text, ts):
    o = origin if isinstance(origin, dict) else {}
    # idle and delivery notices carry no peer origin; sub-agent handbacks are peer but not session messages
    if o.get('kind') != 'peer' or o.get('handback') or not isinstance(text, str):
        return
    m = PEER.search(text)
    if not m:
        return
    attr = lambda k: (re.search(rf'\b{k}="([^"]*)"', m.group(1)) or [None, ''])[1]
    s['recv'].append({'t': ms_or_none(ts), 'from': str(o.get('name') or attr('from-name') or '?'),
                      'addr': str(o.get('from') or attr('from')), 'msg_id': o.get('msg_id'),
                      'text': clip(str(o.get('body') or m.group(2)).strip())})


def send_result(tur, content):
    """(delivered, msg_id) from a SendMessage tool result."""
    if not isinstance(tur, dict):
        try:
            tur = json.loads(text_of(content))
        except (ValueError, TypeError, AttributeError):
            return False, None
    return tur.get('success') is True, tur.get('msg_id')


def scan_record(s, d):
    t = d.get('type')
    if t == 'agent-name':
        s['name'] = str(d.get('agentName') or '') or s['name']
    elif t == 'assistant':
        m = d.get('message') or {}
        if isinstance(m.get('usage'), dict) and m.get('id'):
            # every content block of one API message repeats its usage: keep one per id
            s['usage'][m['id']] = [int(m['usage'].get(k) or 0) for k in USAGE_KEYS]
        for b in m.get('content') or []:
            if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'SendMessage':
                inp = b.get('input') or {}
                to = re.sub(r' \[[0-9a-f]{4,}\]$', '', str(inp.get('to') or ''))   # 'name [ref]' when names collide
                s['sends'][b.get('id')] = {'t': ms_or_none(d.get('timestamp')), 'to': to,
                                           'sum': str(inp.get('summary') or '')[:200], 'text': clip(str(inp.get('message') or '')),
                                           'ok': None, 'msg_id': None}
    elif t == 'user':
        c = (d.get('message') or {}).get('content')
        if isinstance(c, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c):
            for b in c:
                x = s['sends'].get(b.get('tool_use_id')) if isinstance(b, dict) else None
                if x:
                    x['ok'], x['msg_id'] = send_result(d.get('toolUseResult'), b.get('content'))
        else:
            peer_receipt(s, d.get('origin'), text_of(c), d.get('timestamp'))
    elif t == 'attachment':
        a = d.get('attachment') or {}
        if a.get('type') == 'queued_command':
            peer_receipt(s, a.get('origin'), a.get('prompt'), a.get('timestamp') or d.get('timestamp'))


def claude_scan(path):
    """A transcript's latest agent-name, SendMessage calls, peer receipts and usage per message; reads only new bytes.

    Returns copies taken under the lock: another request thread may be extending the same file's state meanwhile.
    """
    with _scan_lock:
        s = _claude.get(path)
        size = os.path.getsize(path)
        if not s or size < s['pos']:
            s = _claude[path] = {'pos': 0, 'name': '', 'sends': {}, 'recv': [], 'usage': {}}
        with open(path, 'rb') as f:
            f.seek(s['pos'])
            data = f.read(size - s['pos'])
        end = data.rfind(b'\n') + 1   # skip a line still being written
        for line in data[:end].splitlines():
            if line.startswith(SCAN_SKIP) or not any(k in line for k in SCAN_KEYS):
                continue
            try:
                scan_record(s, json.loads(line))
            except (ValueError, AttributeError, TypeError, KeyError):
                continue
        s['pos'] += end
        return {'name': s['name'], 'sends': [dict(x) for x in s['sends'].values()], 'recv': list(s['recv']),
                'usage': list(s['usage'].values())}


KINDS = {'배정': 'assign', '보고': 'report', '알림': 'notice'}


def kind_of(text):
    """[배정] [보고] [알림] at the head of the body (also [배정 보완]); anything else is other."""
    m = re.match(r'\s*\[(배정|보고|알림)[\]\s]', text or '')
    return KINDS[m.group(1)] if m else 'other'


def head_of(text):
    return hashlib.sha1(text.strip()[:200].encode()).hexdigest()[:16]


JOIN_MS = 10 * 60 * 1000   # a receipt without msg_id matches a send with the same body head this close in time


def merge_messages(sends, recvs, pids=None):
    """One record per message: the sender's copy wins and a receipt only names the receiver,
    or stands in when the sender's transcript is missing. Joined on msg_id, else on body head and time.

    sends: delivered SendMessage calls with 'from' = the sender session; recvs: receipts with 'to' = the receiver.
    pids: live pid -> session name, the last resort for a `to` given as a uds:…/<pid>.sock address.
    """
    by_id, by_head, rest = {}, {}, []
    for r in sorted(recvs, key=lambda r: r['t'] or 0):
        h = by_head.setdefault(head_of(r['text']), [])
        if r['msg_id'] in by_id or any(not (r['msg_id'] and x['msg_id']) and (x['from'], x['to']) == (r['from'], r['to'])
                                       and abs((x['t'] or 0) - (r['t'] or 0)) <= JOIN_MS for x in h):
            continue   # the same message logged twice (queued, then delivered)
        if r['msg_id']:
            by_id[r['msg_id']] = r
        h.append(r)
        rest.append(r)
    addr = {r['addr']: r['from'] for r in rest if r['addr']}   # later receipts win
    used, out = set(), []

    def to_name(to):
        m = re.search(r'/(\d+)\.sock$', to)
        return addr.get(to) or (m and (pids or {}).get(int(m.group(1)))) or to

    for x in sends:
        r = by_id.get(x['msg_id']) if x['msg_id'] else None
        if r is None:
            r = next((r for r in by_head.get(head_of(x['text']), []) if id(r) not in used
                      and not (r['msg_id'] and x['msg_id']) and abs((r['t'] or 0) - (x['t'] or 0)) <= JOIN_MS), None)
        if r is not None:
            used.add(id(r))
        out.append({'id': x['msg_id'] or 'h' + head_of(x['text']) + str(x['t']), 't': x['t'], 'from': x['from'],
                    'to': r['to'] if r else to_name(x['to']), 'sum': x['sum'], 'text': x['text'], 'src': 'send'})
    out += [{'id': r['msg_id'] or 'h' + head_of(r['text']) + str(r['t']), 't': r['t'], 'from': r['from'], 'to': r['to'],
             'sum': '', 'text': r['text'], 'src': 'recv'} for r in rest if id(r) not in used]
    for m in out:
        m['kind'] = kind_of(m['text'])
        m['sum'] = m['sum'] or next((l.strip() for l in m['text'].splitlines() if l.strip()), '')[:200]
    return sorted(out, key=lambda m: -(m['t'] or 0))


def parse_hours(v):
    """(hours, '') for /api/messages?hours=; missing means 24, 0 means all. A bad value gives (None, reason)."""
    if v is None:
        return 24, ''
    try:
        h = float(v)
    except ValueError:
        return None, f'hours 는 숫자여야 함: {v[:40]}'
    if not math.isfinite(h) or h < 0:
        return None, f'hours 는 0 이상의 유한한 수여야 함: {v[:40]}'
    return h, ''


def messages(hours=24, now=None, root=PROJECTS, rows=(), names=None):
    """Messages between Claude sessions in the last `hours` (0: all), with the graph's nodes and edges.

    Every top-level transcript touched in the window counts, closed sessions included (sub-agent ones do not).
    """
    now = time.time() if now is None else now
    since = int((now - hours * 3600) * 1000) if hours > 0 else 0
    names = roster() if names is None else names
    by_sid = {a.get('sessionId'): a for a in rows}
    sends, recvs = [], []
    for p in glob.glob(os.path.join(root, '*', '*.jsonl')):
        try:
            if os.path.getmtime(p) * 1000 < since:
                continue
            s = claude_scan(p)
        except OSError:
            continue
        sid = os.path.basename(p)[:-len('.jsonl')]
        owner = s['name'] or (by_sid.get(sid) or {}).get('name') or sid[:8]   # latest name: renames fold into one node
        # delivered with a msg_id; a success without one went to a sub-agent of the same session
        sends += [dict(x, **{'from': owner}) for x in s['sends'] if x['ok'] and x['msg_id']]
        recvs += [dict(x, to=owner) for x in s['recv']]
    pids = {a['pid']: a.get('name') for a in rows if a.get('pid') and a.get('engine') != 'codex'}
    msgs = [m for m in merge_messages(sends, recvs, pids) if m['t'] is not None and m['t'] >= since]
    edges, count = {}, {}
    for m in msgs:
        e = edges.setdefault((m['from'], m['to']), {'from': m['from'], 'to': m['to'], 'count': 0, 'last': 0})
        e['count'] += 1
        e['last'] = max(e['last'], m['t'] or 0)
        for k, n in (('sent', m['from']), ('got', m['to'])):
            count.setdefault(n, {'sent': 0, 'got': 0})[k] += 1
    first = {}
    for a in rows:   # rows come arranged, so the first row of a name is its most active session
        first.setdefault(a.get('name'), a)
    nodes = []
    for n, c in count.items():
        a, r = first.get(n), names.get(n) or {}
        # as in merge_decisions(): neither in agents.json nor listed is taken as a closed one-off session
        group = 'codex' if a and a.get('engine') == 'codex' else r.get('group') or ('other' if a else 'task')
        nodes.append(dict(c, name=n, group=group, lane=r.get('lane', ''), task=r.get('task', ''), listed=bool(a),
                          **{k: a.get(k) for k in ('state', 'status', 'kind', 'engine', 'sessionId')} if a else {}))
    hub = next((n for n, r in names.items() if r.get('lane') == 'HUB'), None)
    return {'hours': hours, 'hub': hub, 'nodes': nodes,   # nothing time-varying, so an unchanged answer is byte-identical
            'edges': sorted(edges.values(), key=lambda e: -e['count']), 'total': len(msgs),
            'messages': msgs[:MAX_MESSAGES]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode('utf-8', 'replace')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # DNS-rebinding guard: only answer requests addressed to this machine by name.
        if self.headers.get('Host') not in (f'127.0.0.1:{PORT}', f'localhost:{PORT}'):
            return self.send(403, {'error': 'bad host'})
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                with open(os.path.join(ROOT, name), 'rb') as f:
                    return self.send(200, f.read(), ctype)
            if url.path == '/api/agents':
                return self.send(200, {'now': int(time.time() * 1000), 'agents': agents()})
            if url.path == '/api/usage':
                return self.send(200, usage())
            if url.path == '/api/decisions':
                items, err = load_decisions()
                try:
                    rows = agents()
                except (subprocess.SubprocessError, OSError, ValueError):
                    rows = []
                return self.send(200, dict(merge_decisions(items, rows, roster()), error=err))
            if url.path == '/api/todos':
                try:
                    rows = agents()
                except (subprocess.SubprocessError, OSError, ValueError):
                    rows = []
                return self.send(200, todos(rows))
            if url.path == '/api/dev':
                return self.send(200, dev_occupancy())
            if url.path == '/api/messages':
                try:
                    rows = agents()
                except (subprocess.SubprocessError, OSError, ValueError):
                    rows = []
                hours, why = parse_hours(q.get('hours'))
                if why:
                    return self.send(400, {'error': why})
                return self.send(200, messages(hours, rows=rows))
            if url.path == '/api/status':
                try:
                    with open(STATUS_MD, encoding='utf-8', errors='replace') as f:
                        return self.send(200, {'mtime': int(os.path.getmtime(STATUS_MD) * 1000), 'text': f.read(), 'path': STATUS_MD})
                except OSError:
                    return self.send(200, {'missing': True, 'path': STATUS_MD})
            if url.path == '/api/transcript':
                a = next((a for a in agents() if a.get('sessionId') == q.get('sid')), None)
                if not a:
                    return self.send(404, {'error': '목록에 없는 세션'})
                if a.get('engine') == 'codex':
                    return self.send(200, read_transcript(a['path'], int(q.get('from', -1)), codex_events_of))
                p = transcript_path(a['sessionId'], a.get('cwd'))
                if not p:
                    return self.send(200, {'offset': 0, 'reset': True, 'events': [], 'missing': True})
                return self.send(200, read_transcript(p, int(q.get('from', -1))))
            if url.path == '/api/logs':
                a = next((a for a in agents() if a.get('id') and a['id'] == q.get('id')), None)
                if not a:
                    return self.send(404, {'error': 'logs 는 background 세션만'})
                r = subprocess.run([CLAUDE, 'logs', a['id']], capture_output=True, timeout=15)
                return self.send(200, r.stdout or r.stderr, 'text/plain; charset=utf-8')
            self.send(404, {'error': 'not found'})
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            self.send(500, {'error': str(e)})


if __name__ == '__main__':
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f'http://{HOST}:{PORT} hub {HUB} pid {os.getpid()}', flush=True)   # 끌 때 이 pid 를 쓴다
    srv.serve_forever()
