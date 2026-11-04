#!/usr/bin/env python3
"""PreToolUse 훅: Obsidian Vault 의 위키(wiki/)는 허브 Main 세션만 Edit/Write 한다.

- Main 은 aohub 에 지금 등록된 세션이다. 허브 창이면 Claude 세션 id, Manager Agent(AO 세션)면 `ao:<AO_SESSION_ID>`.
- 보는 것(Read)과 일지(raw) 쓰기는 막지 않는다. 일지는 공통 스크립트가 잠금과 중복 방지를 하고 모든 세션이 쓴다.
- aohub 를 설정하지 않았거나(~/.claude-hub-ao/config.json 없음), Vault 가 없거나, 등록된 Main 이 없으면 막지 않는다.
- Bash 로 쓰는 것(sed -i, >)과 aohub 의 기록 모듈은 이 훅에 걸리지 않는다.
"""
import json
import os
import sqlite3
import sys

from hubpath import NT, inside, norm

MSG = ('Obsidian 위키({wiki})는 허브 Main 세션만 고칩니다. 이 세션은 Main 이 아닙니다(지금 Main: {main}). '
       '위키에 반영할 내용은 Main 에 알리고, 작업 기록은 일지(raw) 공통 스크립트로 남기세요.')


def home():
    return os.path.abspath(os.path.expanduser(os.environ.get('AOHUB_HOME') or '~/.claude-hub-ao'))


def active_main(h):
    path = os.path.join(h, 'state.sqlite3')
    if not os.path.exists(path):
        return None
    try:
        db = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True, timeout=5)
        try:
            row = db.execute('SELECT session_id FROM mains WHERE active=1 ORDER BY last_seen DESC LIMIT 1').fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def wiki_root(h):
    try:
        with open(os.path.join(h, 'config.json'), encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    vault = (cfg.get('vault_dir') or '').strip()
    if not vault:
        return None
    return os.path.join(os.path.expanduser(vault), cfg.get('wiki_dir') or 'wiki')


def is_main(inp, main, env=os.environ):
    ids = {inp.get('session_id'), env.get('CLAUDE_CODE_SESSION_ID')}
    if env.get('AO_SESSION_ID'):
        ids.add('ao:' + env['AO_SESSION_ID'])
    return main in ids


def blocked(inp, h=None, env=os.environ, nt=NT):
    """막아야 하면 (위키 경로, Main) 을, 아니면 None 을 돌려준다."""
    ti = inp.get('tool_input') or {}
    f = ti.get('file_path') or ti.get('notebook_path')
    h = h or home()
    wiki = wiki_root(h)
    if not f or not wiki:
        return None
    if not inside(norm(f, nt, inp.get('cwd') or ''), norm(wiki, nt)):
        return None
    main = active_main(h)
    if not main or is_main(inp, main, env):
        return None
    return wiki, main


def main():
    for s in (sys.stdin, sys.stdout, sys.stderr):
        s.reconfigure(encoding='utf-8')
    try:
        inp = json.load(sys.stdin)
    except ValueError:
        return 0
    hit = blocked(inp)
    if hit:
        print(MSG.format(wiki=hit[0].replace('\\', '/'), main=hit[1]), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
