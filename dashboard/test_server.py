"""Parser self-check: python3 test_server.py"""
import json
import os
import shutil
import tempfile
import time

import server


def rec(**kw):
    return json.dumps(kw, ensure_ascii=False) + '\n'


lines = [
    rec(type='user', timestamp='t1', origin={'kind': 'human'}, message={'content': '대시보드 만들어'}),
    rec(type='user', timestamp='t2', isMeta=True, origin={'kind': 'peer'}, message={'content':
        'Another Claude session sent a message:\n<cross-session-message from="uds:x" from-name="hub">[배정] 할 일</cross-session-message>'}),
    rec(type='user', timestamp='t3', isMeta=True, message={'content': 'skill body'}),
    rec(type='attachment', timestamp='t4', attachment={'type': 'queued_command', 'origin': {'kind': 'peer'},
        'prompt': '<cross-session-message from-name="WF1">보고</cross-session-message>'}),
    rec(type='assistant', timestamp='t5', message={'content': [
        {'type': 'thinking', 'thinking': ''},
        {'type': 'text', 'text': '알겠습니다'},
        {'type': 'tool_use', 'id': 'a', 'name': 'Bash', 'input': {'command': 'ls', 'description': 'List files'}},
        {'type': 'tool_use', 'id': 'b', 'name': 'SendMessage', 'input': {'to': 'hub', 'summary': '보고', 'message': '끝'}}]}),
    rec(type='user', timestamp='t6', message={'content': [{'type': 'tool_result', 'tool_use_id': 'a', 'is_error': True, 'content': 'boom'}]}),
    rec(type='api-request', body='secret'),
]
with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='', suffix='.jsonl', delete=False) as f:
    f.write(''.join(lines) + '{"type": "user", "partial')   # last line still being written
r = server.read_transcript(f.name, -1)
kinds = [e['k'] for e in r['events']]
assert kinds == ['user', 'peer', 'peer', 'reply', 'tool', 'send', 'result'], kinds
assert r['events'][1]['from'] == 'hub' and r['events'][1]['text'] == '[배정] 할 일'
assert r['events'][2]['from'] == 'WF1'
assert r['events'][4]['sum'] == 'List files' and r['events'][6]['err'] is True
assert r['offset'] == len(''.join(lines).encode()), 'partial line must not be consumed'
assert server.read_transcript(f.name, r['offset'])['events'] == []
os.unlink(f.name)

# roster: resident / task / other, and a broken or missing file falls back to other
with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='', suffix='.json', delete=False) as f:
    json.dump({'_note': 'x', 'resident': {'hub': {'lane': 'HUB'}, 'WF1': {'lane': 'WF1'}, 'bad': 'str'},
               'task': {'task-ui': {'task': 'T-1', 'lane': '도구'}}}, f)
ro = server.roster(f.name)
assert ro['hub'] == {'group': 'resident', 'lane': 'HUB', 'task': '', 'order': 0}
assert ro['WF1']['order'] == 1 and 'bad' not in ro
assert ro['task-ui']['group'] == 'task' and ro['task-ui']['task'] == 'T-1'
assert ro.get('stranger', {'group': 'other'})['group'] == 'other'
for broken in ('{"resident": ', '[1, 2]', '{"resident": [1]}', '{"task": {"x": null}}'):
    with open(f.name, 'w', encoding='utf-8', newline='') as g:
        g.write(broken)
    assert server.roster(f.name) == {}, broken
os.unlink(f.name)
assert server.roster(f.name) == {}

# layout: busy / wait / done, then task above resident above other, then latest activity first
A = lambda name, group, act, **kw: dict(name=name, group=group, lastActivity=act, **kw)
rows = [A('r-done', 'resident', 9, kind='background', state='done', status='idle'),
        A('o-busy', 'other', 9, kind='background', state='working', status='busy'),
        A('r-busy', 'resident', 9, kind='interactive', status='busy'),
        A('t-busy', 'task', 1, kind='background', state='working', status='busy'),
        A('r-idle-old', 'resident', 1, kind='interactive', status='idle'),
        A('r-blocked', 'resident', 5, kind='background', state='blocked', status='idle'),
        A('t-blocked', 'task', 2, kind='background', state='blocked', status='idle'),
        A('o-blocked', 'other', 9, kind='background', state='blocked', status='idle'),
        A('t-done', 'task', 3, kind='background', state='done', status='idle')]
got = [(a['section'], a['name']) for a in server.arrange(rows)]
assert got == [('busy', 't-busy'), ('busy', 'r-busy'), ('busy', 'o-busy'),
               ('wait', 't-blocked'), ('wait', 'r-blocked'), ('wait', 'r-idle-old'), ('wait', 'o-blocked'),
               ('done', 't-done'), ('done', 'r-done')], got

# decisions: file items by status plus live blocked needs, each section in display order
names = {'WF1': {'group': 'resident', 'lane': 'WF1', 'task': '', 'order': 0},
         'task-x': {'group': 'task', 'lane': '도구', 'task': 'T-9', 'order': 0}}
rows = [A('task-x', 'task', 5, state='blocked', needs='권한 승인?', sessionId='s1'),
        A('WF1', 'resident', 9, state='blocked', needs='어느 쪽?', sessionId='s2'),
        A('busy-one', 'other', 9, state='working', needs='옛 질문', sessionId='s3'),
        A('done-one', 'other', 9, state='done', needs='옛 질문', sessionId='s4'),
        A('quiet', 'other', 9, state='blocked', needs='', sessionId='s5')]
items = [{'id': 'o1', 'status': 'open', 'title': 'a', 'source': 'WF1', 'raised': '2026-10-20 19:30', 'refs': 'one'},
         {'id': 'o2', 'status': 'open', 'title': 'b', 'source': 'flow-env', 'raised': '2026-10-20 21:00'},
         {'id': 'x', 'status': '???', 'title': 'c', 'raised': '2026-10-20 18:00'},
         {'id': 'w1', 'status': 'waiting', 'until': '사용자가 다시 말할 때', 'raised': '2026-10-20 19:00'},
         {'id': 'w2', 'status': 'waiting', 'until': '2026-10-22', 'raised': '2026-10-20 19:00'},
         {'id': 'w3', 'status': 'waiting', 'until': '2026-10-21', 'raised': '2026-10-20 18:00'},
         {'id': 'w4', 'status': 'waiting', 'until': '나중', 'raised': '2026-10-20 20:00'},
         {'id': 'd1', 'status': 'decided', 'decided_at': '2026-10-20 20:10'},
         {'id': 'd2', 'status': 'decided'},
         {'id': 'd3', 'status': 'decided', 'decided_at': '2026-10-20 21:30'}]
m = server.merge_decisions(items, rows, names)
assert [d.get('id') or d['source'] for d in m['now']] == ['WF1', 'task-x', 'o2', 'o1', 'x'], m['now']
assert m['now'][1]['live'] and m['now'][1]['task'] == 'T-9' and m['now'][1]['sid'] == 's1'
assert [d['id'] for d in m['waiting']] == ['w3', 'w2', 'w4', 'w1']
assert [d['id'] for d in m['decided']] == ['d3', 'd1', 'd2']
o1, o2 = m['now'][3], m['now'][2]
assert o1['refs'] == ['one'] and o1['options'] == [] and o1['who']['group'] == 'resident'
assert o2['who'] == {'group': 'task', 'closed': True}   # not listed, not running: closed one-off
with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='', suffix='.json', delete=False) as f:
    f.write('{"items": {"a": 1}}')
assert server.load_decisions(f.name) == ([], 'items 가 목록이 아님')
with open(f.name, 'w', encoding='utf-8', newline='') as g:
    g.write('{"items": [1, "x", {"id": "ok"}]}')
assert server.load_decisions(f.name) == ([{'id': 'ok'}], '')
for broken in ('{"items": ', '[1]'):
    with open(f.name, 'w', encoding='utf-8', newline='') as g:
        g.write(broken)
    assert server.load_decisions(f.name)[0] == [] and server.load_decisions(f.name)[1], broken
os.unlink(f.name)
assert server.load_decisions(f.name) == ([], '파일 없음')
m = server.merge_decisions([], rows, {})
assert len(m['now']) == 2 and not m['waiting'] and not m['decided']   # live asks survive a missing file
# codex: rollout parser, then rows from a fake ~/.codex (liveness passed in, no /proc)
def ev(t, **kw):
    return rec(timestamp='t', type='event_msg', payload=dict(type=t, **kw))
def item(**kw):
    return ev('item_completed', item=kw)
U1, U2, U3, U4, U5 = ('01a00000-0000-7000-8000-00000000000%d' % i for i in range(1, 6))
def meta(sid, source='exec'):
    return rec(timestamp='t', type='session_meta', payload={'id': sid, 'cwd': '/w/' + sid[:8], 'source': source,
               'originator': 'codex_exec', 'cli_version': '0.163.0', 'timestamp': '2026-10-26T01:07:10.402Z'})
busy = [meta(U1), ev('task_started', turn_id='a'),
        rec(timestamp='t', type='response_item', payload={'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '# AGENTS.md'}]}),
        item(type='UserMessage', content=[{'type': 'text', 'text': '조사해'}, {'type': 'local_image', 'path': '/x.png'}, {'type': 'mention', 'name': 'Ponytail'}]),
        item(type='Reasoning', id='r', summary_text=[]),
        item(type='AgentMessage', id='m', content=[{'type': 'Text', 'text': '보겠습니다'}], phase='commentary'),
        item(type='CommandExecution', id='c1', command=['/bin/bash', '-lc', 'ls docs\nwc -l'], status='failed', exit_code=2, aggregated_output='no such'),
        item(type='McpToolCall', id='c2', server='cbm', tool='search_graph', arguments={'pattern': 'x'}, status='completed',
             result={'content': [{'type': 'text', 'text': '{"total":1}'}]}),
        item(type='FileChange', id='c3', changes={'/w/a.md': {'type': 'add', 'content': 'x'}}, status='completed', stdout='', stderr=''),
        item(type='Extension', id='c4', kind='web.search', query='codex'),
        item(type='ContextCompaction', id='cc'),
        ev('turn_aborted', reason='interrupted'),
        ev('task_started', turn_id='b')]
tmp = tempfile.mkdtemp()
day = os.path.join(tmp, 'sessions', '2026', '10', '26')
os.makedirs(day)
def roll(sid, lines, age=0, tail=''):
    path = os.path.join(day, f'rollout-2026-10-26T10-07-10-{sid}.jsonl')
    with open(path, 'w', encoding='utf-8', newline='') as g:
        g.write(''.join(lines) + tail)
    os.utime(path, (NOW - age, NOW - age))
    return path
NOW = 1792976830.0
p1 = roll(U1, busy, tail='{"timestamp":"t","type":"event_msg","payl')   # last line still being written
r = server.read_transcript(p1, -1, server.codex_events_of)
assert [e['k'] for e in r['events']] == ['user', 'reply', 'tool', 'result', 'tool', 'result', 'tool', 'result',
                                         'tool', 'result', 'note', 'note'], r['events']
e = r['events']
assert e[0]['text'] == '조사해\n[이미지]\n@Ponytail'
assert e[2]['name'] == 'shell' and e[2]['sum'] == 'ls docs' and e[3] == {'k': 'result', 't': 't', 'id': 'c1', 'err': True, 'text': 'no such'}
assert e[4]['name'] == 'cbm.search_graph' and e[5]['text'] == '{"total":1}' and not e[5]['err']
assert e[6]['sum'] == 'a.md' and e[8]['name'] == 'web.search' and e[8]['sum'] == 'codex'
assert e[11]['text'] == '턴 중단: interrupted'
assert r['offset'] == len(''.join(busy).encode()), 'partial line must not be consumed'
roll(U2, [meta(U2, 'cli'), ev('task_started'), item(type='UserMessage', content=[{'type': 'text', 'text': '첫 지시'}]),
          ev('task_complete'), ev('task_started'), item(type='UserMessage', content=[{'type': 'text', 'text': '둘째 지시'}]),
          item(type='AgentMessage', content=[{'type': 'Text', 'text': '어느 쪽?'}], questions=[{'title': '어느 쪽?'}], delivery='async'),
          item(type='AgentMessage', content=[{'type': 'Text', 'text': '끝'}]), ev('task_complete')], age=60)
roll(U3, [meta(U3), ev('task_started'), ev('task_complete')], age=3600)
roll(U4, [meta(U4, {'subagent': {'thread_spawn': {'parent_thread_id': U1}}}), ev('task_started')])
roll(U5, [meta(U5, 'cli'), ev('task_started')], age=25 * 3600)
with open(os.path.join(tmp, 'session_index.jsonl'), 'w', encoding='utf-8', newline='') as g:
    g.write(rec(id=U2, thread_name='보고서 계획') + '{broken\n')
rows = server.codex_rows(tmp, live={U1: 11, U2: 22, U4: 44, U5: 55}, now=NOW)
got = {a['sessionId']: (a['name'], a['state'], a['status'], a['pid'], a['detail']) for a in rows}
assert got == {U1: ('codex 00000001', 'working', 'busy', 11, '조사해\n[이미지]\n@Ponytail'),
               U2: ('보고서 계획', 'idle', 'idle', 22, '둘째 지시'),   # async questions never mean blocked
               U3: ('codex 00000003', 'done', 'idle', None, '')}, got   # U4 subagent, U5 older than 24h
c1 = next(a for a in rows if a['sessionId'] == U1)
assert c1['engine'] == 'codex' and c1['source'] == 'exec' and c1['startedAt'] == 1792976830402 and c1['path'] == p1
assert 'id' not in c1   # no claude job id: job_state() and /api/logs must not touch codex rows
with open(p1, 'a', encoding='utf-8', newline='') as g:   # turn ends: incremental read picks it up
    g.write('l"}\n' + ev('task_complete'))
assert {a['sessionId']: a['state'] for a in server.codex_rows(tmp, live={U1: 11}, now=NOW)}[U1] == 'idle'
mixed = server.arrange(rows + [A('c-busy', 'other', NOW * 1000, kind='interactive', status='busy'),
                               A('t-done', 'task', 1, kind='background', state='done', status='idle')])
assert [(a['section'], a['name']) for a in mixed] == [
    ('busy', 'codex 00000001'), ('busy', 'c-busy'), ('wait', '보고서 계획'), ('done', 't-done'), ('done', 'codex 00000003')], mixed
shutil.rmtree(tmp)

# usage: claude usage.json and codex token_count rate_limits, each missing / broken / stale / ok
tmp = tempfile.mkdtemp()
cj, MS = os.path.join(tmp, 'usage.json'), int(NOW * 1000)
def cwrite(body, age=0):
    with open(cj, 'w', encoding='utf-8', newline='') as g:
        g.write(body if isinstance(body, str) else json.dumps(body))
    os.utime(cj, (NOW - age, NOW - age))
U = lambda now=MS: server.usage(cj, os.path.join(tmp, 'codex'), now)
assert U() == {'now': MS, 'claude': {'state': 'missing'}, 'codex': {'state': 'missing'}}
cwrite({'at': MS - 60000, 'rate_limits': {'five_hour': {'used_percentage': 16, 'resets_at': NOW + 3600},
        'seven_day': {'used_percentage': 85.5, 'resets_at': NOW - 1}, 'opus': {'used_percentage': None}, 'x': 3}})
c = U()['claude']
assert c['state'] == 'ok' and c['at'] == MS - 60000, c
assert c['windows'] == [{'key': 'five_hour', 'minutes': 300, 'used': 16.0, 'resets': MS + 3600000, 'reset': False},
                        {'key': 'seven_day', 'minutes': 10080, 'used': 85.5, 'resets': MS - 1000, 'reset': True}], c
assert U(MS + 31 * 60000)['claude']['state'] == 'stale'   # stale is decided per request, not cached
cwrite({'at': MS, 'rate_limits': {}}, age=-5)   # rewrite: the mtime cache must notice
assert U()['claude'] == {'at': MS, 'windows': [], 'state': 'ok'}
for broken in ('{"at": ', '[1]', '{"at": 1}', '{"at": 1, "rate_limits": [1]}', '{"at": "x", "rate_limits": {}}'):
    cwrite(broken, age=len(broken))
    c = U()['claude']
    assert c['state'] == 'broken' and c['error'], (broken, c)
cday = os.path.join(tmp, 'codex', 'sessions', '2026', '10', '26')
os.makedirs(cday)
def tc(ts, used, plan='pro', rl=True):
    return rec(timestamp=ts, type='event_msg', payload={'type': 'token_count', 'info': {}, 'rate_limits': None if not rl else {
        'limit_id': 'codex', 'primary': {'used_percent': used, 'window_minutes': 10080, 'resets_at': NOW + 60},
        'secondary': None, 'plan_type': plan}})
def croll(name, lines, age):
    path = os.path.join(cday, f'rollout-{name}.jsonl')
    with open(path, 'w', encoding='utf-8', newline='') as g:
        g.write(''.join(lines))
    os.utime(path, (NOW - age, NOW - age))
croll('a', [meta(U1), ev('task_started')], age=0)   # newest file, no token_count yet
assert U()['codex'] == {'state': 'missing'}
# b written later (mtime) but its last token_count is older than c's
croll('b', [meta(U2), tc('2026-10-26T01:00:00Z', 10), tc('2026-10-26T01:05:00Z', 20, rl=False)], age=10)
croll('c', [meta(U3), tc('2026-10-26T01:06:00Z', 30, 'prolite') + '{"timestamp":"t","type":"event_msg","pay'], age=20)
croll('d', [meta(U4), '{broken\n', tc('2026-10-26T01:07:00Z', 99)], age=90000)   # newer record, but mtime says the file is older: never read
x = U()['codex']
assert x == {'at': server.iso_ms('2026-10-26T01:06:00Z'), 'plan': 'prolite', 'state': 'ok', 'windows': [
    {'key': 'primary', 'minutes': 10080, 'used': 30.0, 'resets': MS + 60000, 'reset': False}]}, x
assert U(x['at'] + 31 * 60000)['codex']['state'] == 'stale'
shutil.rmtree(tmp)

# sub-agents: <project>/<sid>/subagents/agent-*.meta.json + .jsonl, found under any project folder
sp = tempfile.mkdtemp()
sd = os.path.join(sp, '-some-cwd', 'sid1', 'subagents')
os.makedirs(sd)
def sub(name, meta, age):
    base = os.path.join(sd, 'agent-' + name)
    with open(base + '.meta.json', 'w', encoding='utf-8', newline='') as f:
        json.dump(meta, f)
    if age is not None:
        with open(base + '.jsonl', 'w', encoding='utf-8', newline='') as f:
            f.write('{}\n')
        os.utime(base + '.jsonl', (time.time() - age,) * 2)
sub('aaa', {'agentType': 'Explore', 'description': '조사', 'finished': True}, age=100)
sub('bbb', {'agentType': 'general-purpose', 'description': '진행'}, age=10)
sub('ccc', {'agentType': 'Plan', 'description': '멈춤'}, age=9000)
sub('ddd', {'agentType': 'Plan', 'description': '기록 없음'}, age=None)
open(os.path.join(sd, 'agent-bad.meta.json'), 'w', encoding='utf-8', newline='').write('{broken')
old, server.PROJECTS = server.PROJECTS, sp
try:
    subs = server.subagents_of('sid1')
    by = {s['id']: s for s in subs}
    assert set(by) == {'aaa', 'bbb', 'ccc', 'ddd'}, by
    assert by['aaa']['state'] == 'done' and by['bbb']['state'] == 'busy' and by['ccc']['state'] == 'idle', by
    assert by['bbb']['type'] == 'general-purpose' and by['bbb']['desc'] == '진행'
    assert [s['at'] for s in subs] == sorted((s['at'] for s in subs), reverse=True)
    assert server.subagents_of('nope') == [] and server.subagents_of('') == []
finally:
    server.PROJECTS = old
    shutil.rmtree(sp)

# messages: SendMessage calls and peer receipts across transcripts, one record per message
from datetime import datetime, timezone
iso = lambda sec: datetime.fromtimestamp(sec, timezone.utc).isoformat().replace('+00:00', 'Z')
for text, kind in (('[배정] T-1', 'assign'), ('[배정 보완] T-1', 'assign'), (' [보고] 끝', 'report'), ('[알림] dev', 'notice'),
                   ('[예고 배정] T-2', 'other'), ('[보고서] x', 'other'), ('[허브] x', 'other'), ('T-1 보고', 'other'), ('', 'other')):
    assert server.kind_of(text) == kind, (text, kind)
def send(ts, sid, to, msg, summary=''):
    return rec(type='assistant', timestamp=iso(ts), message={'id': 'm' + sid, 'content': [
        {'type': 'tool_use', 'id': sid, 'name': 'SendMessage', 'input': {'to': to, 'summary': summary, 'message': msg}}]})
def result(sid, ok=True, msg_id=None, as_text=False):
    r = {'success': ok, 'message': '→ x'}
    if msg_id:
        r['msg_id'] = msg_id
    return rec(type='user', toolUseResult=None if as_text else r, message={'content': [
        {'type': 'tool_result', 'tool_use_id': sid, 'content': [{'type': 'text', 'text': json.dumps(r)}]}]})
def peer(ts, frm, msg, msg_id=None, addr='uds:/run/user/1/cc-socks/9.sock', queued=False):
    o = {'kind': 'peer', 'from': addr, 'name': frm, 'msg_id': msg_id, 'body': msg} if msg_id else {'kind': 'peer'}
    text = f'<cross-session-message from="{addr}" from-name="{frm}" from-mode="prompting">\n{msg}\n</cross-session-message>'
    if queued:
        return rec(type='attachment', timestamp=iso(ts), attachment={'type': 'queued_command', 'prompt': text, 'origin': o})
    return rec(type='user', timestamp=iso(ts), origin=o, isMeta=True, message={'content': 'Another Claude session sent a message:\n' + text})
T0 = NOW - 3600
mp = tempfile.mkdtemp()
def tr(folder, sid, lines, age=0, tail=''):
    os.makedirs(os.path.join(mp, folder), exist_ok=True)
    path = os.path.join(mp, folder, sid + '.jsonl')
    with open(path, 'w', encoding='utf-8', newline='') as g:
        g.write(''.join(lines) + tail)
    os.utime(path, (NOW - age, NOW - age))
    return path
hub = tr('-hub', 'hubsid', [
    rec(type='agent-name', agentName='SUP'),
    send(T0, 's1', 'WF1', '[배정] T-1 할 일\n자세히', '할 일 배정'), result('s1', msg_id='m1'),
    send(T0 + 60, 's2', 'uds:/run/user/1/cc-socks/222.sock', '[알림] dev 재기동'), result('s2', msg_id='m2', as_text=True),
    send(T0 + 70, 's3', 'gone', '못 감'), result('s3', ok=False),                       # failed: not delivered
    send(T0 + 80, 's4', 'WF1', '아직 결과 없음'),                                          # no result yet
    send(T0 + 90, 's5', 'release [a1b2c3]', '옛 형식 메시지'), result('s5', msg_id='m5'),   # the receipt has no msg_id
    send(T0 + 95, 's10', 'worker', '서브에이전트에게'), result('s10'),                     # success without msg_id: a sub-agent
    peer(T0 + 200, 'WF1', '[보고] T-1 끝', 'm3'),
    peer(T0 + 300, 'task-old', '[보고] 닫힌 세션 보고', 'm4', queued=True),
    peer(T0 + 310, 'task-old', '[보고] 닫힌 세션 보고', 'm4'),                             # queued, then delivered: one message
    rec(type='queue-operation', operation='enqueue', timestamp=iso(T0 + 300), content='<cross-session-message from-name="x">echo</cross-session-message>'),
    rec(type='user', timestamp=iso(T0 + 400), message={'content': '[Cross-session idle notice] "WF1" ... is idle now'}),
    rec(type='user', timestamp=iso(T0 + 410), origin={'kind': 'peer', 'from': 'a5730', 'name': 'Explore', 'handback': True},
        message={'content': '<cross-session-message from="a5730" from-name="Explore">조사 결과</cross-session-message>'}),
    send(NOW - 30 * 3600, 's6', 'WF1', '[배정] 어제 일'), result('s6', msg_id='m6'),         # outside 24 h
], tail='{"type":"assistant","message"')
tr('-wf1', 'wf1sid', [
    rec(type='agent-name', agentName='wf1-old'),
    peer(T0 + 5, 'SUP', '[배정] T-1 할 일\n자세히', 'm1'),
    peer(T0 + 65, 'SUP', '[알림] dev 재기동', 'm2'),
    send(T0 + 190, 's7', 'SUP', '[보고] T-1 끝'), result('s7', msg_id='m3'),
    rec(type='agent-name', agentName='WF1'),                                               # renamed: the node takes the latest name
])
tr('-rel', 'relsid', [rec(type='agent-name', agentName='release'), peer(T0 + 95, 'SUP', '옛 형식 메시지', queued=True)])
tr('-old', 'oldsid', [rec(type='agent-name', agentName='old'), send(NOW - 50 * 3600, 's8', 'SUP', '[보고] 옛날'), result('s8', msg_id='m8')],
   age=40 * 3600)
os.makedirs(os.path.join(mp, '-hub', 'hubsid', 'subagents'))
with open(os.path.join(mp, '-hub', 'hubsid', 'subagents', 'agent-x.jsonl'), 'w', encoding='utf-8', newline='') as g:
    g.write(send(T0, 's9', 'main', '서브에이전트 메시지') + result('s9', msg_id='m9'))
names = {'SUP': {'group': 'resident', 'lane': 'HUB', 'task': '', 'order': 0}, 'WF1': {'group': 'resident', 'lane': 'WF1', 'task': '', 'order': 1}}
live = [dict(name='WF1', sessionId='wf1sid', pid=222, state='working', status='busy', kind='background')]
M = lambda hours=24: server.messages(hours, now=NOW, root=mp, rows=live, names=names)
r = M()
got = [(m['id'], m['from'], m['to'], m['kind'], m['src']) for m in r['messages']]
assert got == [('m4', 'task-old', 'SUP', 'report', 'recv'),          # sender transcript missing: the receipt stands in
               ('m3', 'WF1', 'SUP', 'report', 'send'),
               ('m5', 'SUP', 'release', 'other', 'send'),            # joined on body head and time
               ('m2', 'SUP', 'WF1', 'notice', 'send'),               # uds address named by the receiver's transcript
               ('m1', 'SUP', 'WF1', 'assign', 'send')], got
assert r['total'] == 5 and r['hub'] == 'SUP' and r == M()   # same answer twice: the page skips re-drawing
m1 = r['messages'][-1]
assert m1['sum'] == '할 일 배정' and m1['text'] == '[배정] T-1 할 일\n자세히' and m1['t'] == T0 * 1000
assert r['messages'][1]['sum'] == '[보고] T-1 끝'   # no summary: first line
assert {(e['from'], e['to']): e['count'] for e in r['edges']} == {('SUP', 'WF1'): 2, ('WF1', 'SUP'): 1, ('SUP', 'release'): 1, ('task-old', 'SUP'): 1}
nodes = {n['name']: n for n in r['nodes']}
assert nodes['SUP']['sent'] == 3 and nodes['SUP']['got'] == 2 and nodes['SUP']['group'] == 'resident' and not nodes['SUP']['listed']
assert nodes['WF1']['listed'] and nodes['WF1']['state'] == 'working' and nodes['WF1']['sessionId'] == 'wf1sid'
assert nodes['task-old']['group'] == 'task' and nodes['release']['group'] == 'task'   # neither listed nor in agents.json
assert [m['id'] for m in M(0)['messages']][-3:] == ['m1', 'm6', 'm8'] and M(0)['total'] == 7   # all: the 30 h and 50 h ones too
assert M(0.5)['total'] == 0 and M(1.5)['total'] == 5
assert server.merge_messages([], [dict(t=1, to='WF1', **{'from': 'SUP'}, addr='', msg_id=None, text='x')])[0]['src'] == 'recv'
# the uds address of a receipt names the send's target when no receipt of that message exists, then a live pid
sends = [dict(t=1, to='uds:/x/7.sock', sum='', text='a', ok=True, msg_id='q1', **{'from': 'SUP'}),
         dict(t=2, to='uds:/x/8.sock', sum='', text='b', ok=True, msg_id='q2', **{'from': 'SUP'}),
         dict(t=3, to='uds:/x/6.sock', sum='', text='c', ok=True, msg_id='q3', **{'from': 'SUP'})]
recvs = [dict(t=0, to='SUP', addr='uds:/x/7.sock', msg_id='q0', text='hi', **{'from': 'WF2'})]
assert [m['to'] for m in server.merge_messages(sends, recvs, {8: 'release'})][:3] == ['uds:/x/6.sock', 'release', 'WF2']
with open(hub, 'a', encoding='utf-8', newline='') as g:   # the partial line completes and a new report arrives: read incrementally
    g.write(',"x":1}\n' + peer(NOW - 10, 'WF1', '[보고] 새 보고', 'm10'))
assert server.messages(24, now=NOW, root=mp, rows=live, names=names)['messages'][0]['id'] == 'm10'
shutil.rmtree(mp)

# dev occupancy: the hub's dev-occupancy.json, missing / broken / stale / ok
dp = tempfile.mkdtemp()
df, MS = os.path.join(dp, 'dev-occupancy.json'), int(NOW * 1000)
def dwrite(body, age=0):
    with open(df, 'w', encoding='utf-8', newline='') as g:
        g.write(body if isinstance(body, str) else json.dumps(body))
    os.utime(df, (NOW - age, NOW - age))
D = lambda now=MS: server.dev_occupancy(df, now)
assert D() == {'state': 'missing', 'error': '파일 없음'}
up = iso(NOW - 600)
dwrite({'updated_at': up, 'items': [
    {'who': 'task-wf1-supervisor', 'kind': 'run', 'what': 'WF1 dev 비교 실행', 'since': '2026-10-26T15:10:00+09:00', 'until': '', 'note': '허브 추정'},
    'junk', {'who': 'release', 'kind': 'restart', 'what': 'dev Langflow 재기동', 'since': iso(NOW + 900), 'until': None}]})
d = D()
assert d['state'] == 'ok' and d['updatedAt'] == MS - 600000, d
assert [i['who'] for i in d['items']] == ['release', 'task-wf1-supervisor']   # restart first, junk dropped
assert d['items'][0]['sinceAt'] == MS + 900000 and d['items'][0]['until'] == '' and d['items'][0]['untilAt'] is None
assert d['items'][1]['sinceAt'] == server.iso_ms('2026-10-26T06:10:00Z') and d['items'][1]['note'] == '허브 추정'
assert D(MS + 2 * 3600 * 1000)['state'] == 'stale'   # updated_at older than 2 h: faded, decided per request
dwrite({'items': []}, age=-5)   # rewrite (the mtime cache must notice); no updated_at counts as stale
assert D() == {'state': 'stale', 'updatedAt': None, 'items': []}
for broken in ('{"items": ', '[1]', '{"updated_at": "x"}', '{"items": {"a": 1}}', '{"items": "x"}'):
    dwrite(broken, age=len(broken) + 10)
    d = D()
    assert d['state'] == 'broken' and d['error'], (broken, d)
shutil.rmtree(dp)

# tokens: 처리 토큰 = input + cache write + output, once per API message id, sub-agent transcripts included
tp = tempfile.mkdtemp()
def asst(mid, i, o, cr, cw, **kw):
    return rec(type='assistant', message=dict(id=mid, usage={'input_tokens': i, 'output_tokens': o, 'cache_read_input_tokens': cr,
               'cache_creation_input_tokens': cw, 'service_tier': 'standard'}, content=[]), **kw)
tf = os.path.join(tp, 's.jsonl')
with open(tf, 'w', encoding='utf-8', newline='') as g:   # one API message logged once per content block: count it once, the last copy wins
    g.write(asst('a', 3, 1, 100, 10) + asst('a', 3, 50, 100, 10) + asst('b', 2, 20, 300, 0) + rec(type='user', message={'content': 'x'})
            + rec(type='assistant', message={'id': 'c', 'content': []}) + '{"type":"assistant","message":{"usage"')
k = {'input': 5, 'output': 70, 'cacheRead': 400, 'cacheWrite': 10, 'engine': 'claude'}
assert server.claude_tokens(tf) == dict(k, total=85, subagents=0)   # cache reads stay out of the total
assert server.claude_tokens(None) is None
os.makedirs(os.path.join(tp, 's', 'subagents'))
with open(os.path.join(tp, 's', 'subagents', 'agent-x.jsonl'), 'w', encoding='utf-8', newline='') as g:
    g.write(asst('e', 10, 5, 1000, 7) + asst('e', 10, 6, 1000, 7))
with open(os.path.join(tp, 's', 'subagents', 'agent-x.meta.json'), 'w', encoding='utf-8', newline='') as g:
    g.write('{}')   # not a transcript
assert server.claude_tokens(tf) == {'input': 15, 'output': 76, 'cacheRead': 1400, 'cacheWrite': 17, 'engine': 'claude',
                                    'total': 108, 'subagents': 1}
with open(tf, 'a', encoding='utf-8', newline='') as g:   # incremental: the partial line completes as a new message
    g.write(',"x":1}}\n' + asst('d', 1, 1, 1, 1))
assert server.claude_tokens(tf)['total'] == 111
os.makedirs(os.path.join(tp, 'jobs', 'j1'))
with open(os.path.join(tp, 'jobs', 'j1', 'state.json'), 'w', encoding='utf-8', newline='') as g:
    json.dump({'detail': 'd', 'tokens': 793023}, g)
old, server.JOBS = server.JOBS, os.path.join(tp, 'jobs')
try:
    assert server.job_state('j1') == {'detail': 'd', 'needs': ''}   # state.json tokens is not used
finally:
    server.JOBS = old
cu = {'input_tokens': 6836614, 'cached_input_tokens': 6730112, 'output_tokens': 27138, 'reasoning_output_tokens': 11467, 'total_tokens': 6863752}
cday = os.path.join(tp, 'codex', 'sessions', '2026', '10', '26')
os.makedirs(cday)
with open(os.path.join(cday, f'rollout-x-{U1}.jsonl'), 'w', encoding='utf-8', newline='') as g:
    g.write(meta(U1) + ev('token_count', info={'total_token_usage': dict(cu, total_tokens=1)}, rate_limits=None)
            + ev('token_count', info={'total_token_usage': cu}, rate_limits=None) + ev('token_count', info=None, rate_limits=None))
os.utime(os.path.join(cday, f'rollout-x-{U1}.jsonl'), (NOW, NOW))
t = server.codex_rows(os.path.join(tp, 'codex'), live={}, now=NOW)[0]['tokens']
assert t == {'total': 133640, 'input': 106502, 'cacheRead': 6730112, 'output': 27138, 'reasoning': 11467, 'engine': 'codex'}, t
assert t['total'] == cu['input_tokens'] - cu['cached_input_tokens'] + cu['output_tokens']   # reasoning is inside output
shutil.rmtree(tp)
assert server.parse_hours(None) == (24, '') and server.parse_hours('0') == (0, '') and server.parse_hours('1.5') == (1.5, '')
for bad in ('x', '', 'nan', 'inf', '-3'):
    h, why = server.parse_hours(bad)
    assert h is None and why.startswith('hours'), (bad, why)
print('ok')

# todos: <tasks>/<sid>/<id>.json grouped by session, old <todos>/<sid>-agent-<id>.json lists read too
d = tempfile.mkdtemp()
tasks, old = os.path.join(d, 'tasks'), os.path.join(d, 'todos')
now = time.time()


def todo(sid, tid, age, **kw):
    os.makedirs(os.path.join(tasks, sid), exist_ok=True)
    p = os.path.join(tasks, sid, tid + '.json')
    with open(p, 'w', encoding='utf-8', newline='') as f:
        json.dump(dict({'id': tid, 'subject': 's' + tid, 'blocks': [], 'blockedBy': []}, **kw), f)
    os.utime(p, (now - age, now - age))


todo('live-sid', '2', 60, status='pending')
todo('live-sid', '10', 60, status='pending', blockedBy=['2'], owner='w')
todo('live-sid', '1', 60, status='completed')
todo('live-sid', '3', 30, status='in_progress', activeForm='하는 중')
todo('live-sid', 'x', 60, status='weird')
with open(os.path.join(tasks, 'live-sid', '9.json'), 'w', encoding='utf-8', newline='') as f:
    f.write('{"id": "9", "subj')           # half written
open(os.path.join(tasks, 'live-sid', '.lock'), 'w').close()
os.makedirs(os.path.join(tasks, 'empty-sid'))  # only lock files, no todos
todo('0123456789abcdef', '1', 10 * 86400, status='pending')   # closed and stale
os.makedirs(old)
with open(os.path.join(old, 'oldsid-agent-oldsid.json'), 'w', encoding='utf-8', newline='') as f:
    json.dump([{'content': '옛 형식', 'status': 'in_progress', 'activeForm': '옛 형식 하는 중'}], f)
os.utime(os.path.join(old, 'oldsid-agent-oldsid.json'), (now - 10, now - 10))
rows = [{'sessionId': 'live-sid', 'name': 'WF1', 'state': 'working', 'group': 'resident', 'lane': 'WF1'}]
t = server.todos(rows, tasks, old, now)
assert [g['sid'] for g in t['groups']] == ['oldsid', 'live-sid', '0123456789abcdef'], t['groups']
o, live, gone = t['groups']
assert o['items'][0]['subject'] == '옛 형식' and o['closed'] and o['name'] == 'oldsid'
assert live['name'] == 'WF1' and not live['closed'] and live['group'] == 'resident' and live['at'] == int((now - 30) * 1000)
assert [i['id'] for i in live['items']] == ['3', '2', '10', 'x', '1'], live['items']   # numeric ids, unknown status with pending
assert live['items'][2]['blockedBy'] == ['2'] and live['items'][2]['owner'] == 'w'
assert gone['closed'] and gone['stale'] and gone['name'] == '01234567' and 'group' not in gone
assert t['counts'] == {'in_progress': 2, 'pending': 2}, t['counts']   # stale groups and unknown status stay out of the tab count
assert server.todos([], os.path.join(d, 'none'), os.path.join(d, 'none')) == {'groups': [], 'counts': {'in_progress': 0, 'pending': 0}}
shutil.rmtree(d)

# 세션 출처: 허브 팀(agents.json), 그 밖의 세션
assert server.origin({'group': 'resident', 'cwd': 'C:/Users/me/hub'}) == 'team'
assert server.origin({'group': 'task', 'cwd': 'C:/Users/me/projects/nipa/ax-flow'}) == 'team'
assert server.origin({'group': 'other', 'cwd': 'C:/Users/me/projects/nipa'}) == 'other'
assert all('origin' in a for a in server.arrange([{'name': 'x', 'group': 'other', 'cwd': '/tmp'}]))
print('ok')
