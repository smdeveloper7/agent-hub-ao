#!/usr/bin/env python3
"""SessionStart 훅: 허브 폴더에서 세션이 열리면 세션 이름(sessionTitle, 기본 hub)을 붙인다.

- /rename 과 같다. 대시보드와 다른 세션(SendMessage)이 허브를 이 이름으로 찾는다.
- 이미 이름이 있는 세션(--name, /rename)은 그대로 둔다. 플러그인 설정 hub_title 을 비우면 붙이지 않는다.
- 허브 폴더가 아니면 아무것도 하지 않는다.
"""
import json
import os
import sys

from hubpath import inside, norm


def opt(key, default=''):
    return os.environ.get('CLAUDE_PLUGIN_OPTION_' + key.upper()) or default


def main():
    for s in (sys.stdin, sys.stdout, sys.stderr):
        s.reconfigure(encoding='utf-8')
    try:
        inp = json.load(sys.stdin)
    except ValueError:
        inp = {}
    hub = opt('hub_dir')
    project = os.environ.get('CLAUDE_PROJECT_DIR') or inp.get('cwd') or ''
    if not hub or not project or not inside(norm(project), norm(hub)):
        return 0
    if inp.get('session_title'):
        return 0
    title = opt('hub_title', 'hub').strip()
    if title:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'sessionTitle': title}},
                         ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
