"""hooks/hub_title.py: 허브 폴더에서 열린 세션에만 이름을 붙인다."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, 'hooks', 'hub_title.py')


def run(inp, hub='C:/Users/me/hub', title=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('CLAUDE_')}
    env['CLAUDE_PLUGIN_OPTION_HUB_DIR'] = hub
    if title is not None:
        env['CLAUDE_PLUGIN_OPTION_HUB_TITLE'] = title
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(inp), capture_output=True, text=True,
                       encoding='utf-8', env=env)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)['hookSpecificOutput']['sessionTitle'] if p.stdout.strip() else None


assert run({'cwd': 'C:/Users/me/hub'}) == 'hub'
assert run({'cwd': 'C:/Users/me/hub/sub'}) == 'hub'
assert run({'cwd': 'C:/Users/me/hub'}, title='manager') == 'manager'
assert run({'cwd': 'C:/Users/me/hub', 'session_title': 'mine'}) is None   # 이미 이름이 있으면 둔다
assert run({'cwd': 'C:/Users/me/projects/x'}) is None                     # 허브 폴더 밖
assert run({'cwd': 'C:/Users/me/hub'}, hub='') is None                    # 설정 없음
print('ok')
