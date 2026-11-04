"""고정 실행 경로와 떼어 띄우기.

플러그인은 판마다 다른 폴더(~/.claude/plugins/cache/<마켓>/<플러그인>/<판>)에 깔린다. Supervisor 규칙, 배정 메시지,
Main 알림에 그 경로를 적으면 업데이트 뒤 끊긴다. 그래서 상태 폴더에 고정 실행 파일(bin/aohub.py)을 두고,
깔린 판 중 가장 새 판의 scripts/aohub.py 를 부르게 한다. 옛 판이 돌면서 새 판을 덮어쓰지 않도록 판을 비교한다.
"""
import json
import os
import subprocess
import sys

from . import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = '''# aohub 가 만든 고정 실행 파일입니다. 깔린 플러그인 판이 바뀌면 다시 씁니다.
import runpy, sys
TARGET = %r
sys.argv[0] = TARGET
runpy.run_path(TARGET, run_name="__main__")
'''


def version_of(root):
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            v = json.load(f).get("version") or "0"
        return tuple(int(x) for x in v.split(".") if x.isdigit())
    except (OSError, ValueError):
        return (0,)


def shim_path():
    return os.path.join(config.home(), "bin", "aohub.py")


def target_of_shim(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("TARGET = "):
                    return eval(line[len("TARGET = "):].strip())  # repr 로 쓴 문자열 한 줄
    except (OSError, SyntaxError):
        pass
    return None


def newest_root():
    """플러그인 캐시처럼 판 이름 폴더가 나란히 있으면 그중 가장 새 판. 아니면 지금 폴더."""
    parent = os.path.dirname(ROOT)
    best = ROOT
    try:
        names = os.listdir(parent)
    except OSError:
        return best
    for name in names:
        root = os.path.join(parent, name)
        if root != ROOT and os.path.exists(os.path.join(root, "scripts", "aohub.py")) \
                and os.path.exists(os.path.join(root, ".claude-plugin", "plugin.json")) \
                and version_of(root) > version_of(best):
            best = root
    return best


def ensure():
    """고정 실행 파일이 없거나, 가리키는 판보다 깔린 최신 판이 같거나 새로우면 최신 판을 가리키게 한다."""
    path = shim_path()
    root = newest_root()
    me = os.path.join(root, "scripts", "aohub.py").replace("\\", "/")
    cur = target_of_shim(path)
    if cur == me:
        return path
    if cur and os.path.exists(cur):
        their_root = os.path.dirname(os.path.dirname(cur))
        if version_of(their_root) > version_of(root):
            return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(SHIM % me)
    os.replace(tmp, path)
    return path


def command(cfg):
    """규칙과 알림에 적는 명령. 셸(Git Bash, PowerShell)과 관계없이 돈다."""
    return '"%s" "%s"' % (config.python_bin(cfg), shim_path().replace("\\", "/"))


def spawn_detached(cmd, env=None, cwd=None, log=None):
    """부른 프로세스(훅, 세션 명령)가 끝나도 살아 있게 떼어 띄운다."""
    out = open(log, "ab") if log else subprocess.DEVNULL
    try:
        kw = dict(env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
        if os.name != "nt":
            return subprocess.Popen(cmd, start_new_session=True, **kw).pid
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw).pid
        except OSError:
            return subprocess.Popen(cmd, creationflags=flags, **kw).pid
    finally:
        if log:
            out.close()


def self_cmd(*args):
    return [sys.executable, os.path.join(ROOT, "scripts", "aohub.py")] + list(args)
