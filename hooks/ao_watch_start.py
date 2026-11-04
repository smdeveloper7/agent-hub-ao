#!/usr/bin/env python3
"""SessionStart 훅: 허브 폴더에서 Main 세션이 열리면 AO 감시 프로세스(aohub watch)를 이 세션에 붙여 띄운다.

- Claude Code 는 SessionStart 훅에도 이 세션의 inbox 주소와 토큰(CLAUDE_CODE_MESSAGING_SOCKET/TOKEN)을 내려 준다.
  감시 프로세스는 훅에서 떼어 띄우되 이 환경을 물려받아, 세션이 입력을 기다리는 동안에도 AO 결과와 질문을 넣는다.
- Main 을 다시 열면 새 세션이 Main 으로 등록되고, 옛 세션의 감시 프로세스는 스스로 끝난다.
- aohub 를 설정하지 않았거나(~/.claude-hub-ao/config.json 없음) 허브 폴더가 아니면 아무것도 하지 않는다.
- 플러그인 설정 ao_watch 를 끄면 띄우지 않는다.
- 허브 세션에 이름(sessionTitle, 기본 hub)을 붙인다. 이미 이름이 있으면 두고, 설정 hub_title 을 비우면 붙이지 않는다.
"""
import json
import os
import subprocess
import sys

from hubpath import inside, norm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def opt(key, default=''):
    return os.environ.get('CLAUDE_PLUGIN_OPTION_' + key.upper()) or default


def emit(title, msg=None, ctx=None):
    """세 갈래 출력을 한 JSON 으로: 세션 이름(sessionTitle), 사용자에게 보일 줄, Claude 에게 줄 맥락."""
    out, spec = {}, {}
    if msg:
        out['systemMessage'] = msg
    if title:
        spec['sessionTitle'] = title
    if ctx:
        spec['additionalContext'] = ctx
    if spec:
        out['hookSpecificOutput'] = dict(spec, hookEventName='SessionStart')
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


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
    # 허브 세션에 이름을 붙인다(/rename 과 같음). 이미 이름이 있으면(--name, /rename) 두고, hub_title 을 비우면 붙이지 않는다.
    title = None if inp.get('session_title') else (opt('hub_title', 'hub').strip() or None)
    if opt('ao_watch', 'true').lower() not in ('true', '1', 'yes', 'on'):
        return emit(title)
    home = os.path.abspath(os.path.expanduser(os.environ.get('AOHUB_HOME') or '~/.claude-hub-ao'))
    if not os.path.exists(os.path.join(home, 'config.json')):
        return emit(title)
    sid = inp.get('session_id') or os.environ.get('CLAUDE_CODE_SESSION_ID')
    if not sid or not os.environ.get('CLAUDE_CODE_MESSAGING_SOCKET') or not os.environ.get('CLAUDE_CODE_MESSAGING_TOKEN'):
        return emit(title, 'AO 감시: 이 세션에 inbox 가 없어 띄우지 않았습니다(/status 의 Peer address 확인)')
    env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
    env.setdefault('CLAUDE_PID', str(os.getppid()))  # 훅은 셸 없이 Claude Code 가 직접 띄운다
    logdir = os.path.join(home, 'logs')
    os.makedirs(logdir, exist_ok=True)
    cmd = [sys.executable, os.path.join(ROOT, 'scripts', 'aohub.py'), 'watch']
    with open(os.path.join(logdir, 'watch-stdout.log'), 'ab') as out:
        kw = dict(env=env, cwd=project, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
        if os.name != 'nt':
            subprocess.Popen(cmd, start_new_session=True, **kw)
        else:
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:
                subprocess.Popen(cmd, creationflags=flags, **kw)
    name = title or inp.get('session_title') or sid[:8]
    ctx = ('이 세션은 Main(허브, 세션 이름 %s)입니다. AO 연동 감시 프로세스를 이 세션에 붙였습니다. 먼저 agent-hub-ao:ao 스킬대로 '
           '`aohub resume` 으로 진행 중 작업, 미처리 질문, 이전 결정 노트를 확인하세요. 현재 실행 상태는 AO 에 다시 묻습니다.' % name)
    return emit(title, 'AO 감시 시작(Main %s, %s)' % (name, sid[:8]), ctx)


if __name__ == '__main__':
    sys.exit(main())
