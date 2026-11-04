"""Main Claude Code 세션의 inbox 로 글을 넣는다.

Claude Code 는 세션마다 inbox 를 연다(macOS/Linux 는 Unix 소켓, native Windows 는 named pipe).
주소는 CLAUDE_CODE_MESSAGING_SOCKET, 세션 토큰은 CLAUDE_CODE_MESSAGING_TOKEN 으로 그 세션의 hook 과
Bash 명령에 내려온다. 그래서 감시 프로세스는 반드시 Main 세션 안에서(백그라운드 Bash 또는 SessionStart 훅)
시작해 이 두 값을 물려받는다. 토큰은 메모리에서만 쓰고 기록하지 않는다.

한 연결에 첫 줄 auth, 다음 줄 user 메시지를 보낸다(Windows 는 auth 줄이 필수).
쓰기 성공은 전달 통로에 들어갔다는 뜻일 뿐, Claude 가 처리했다는 뜻이 아니다.
처리 여부는 Main 이 `aohub ack` 를 부를 때 기록한다.
"""
import json
import os
import socket


class InboxUnavailable(RuntimeError):
    pass


def address():
    return os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET") or ""


def available():
    return bool(address() and os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN"))


def _payload(text):
    token = os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN")
    if not token:
        raise InboxUnavailable("CLAUDE_CODE_MESSAGING_TOKEN 이 없습니다. Main 세션 안에서 시작해야 합니다.")
    lines = [json.dumps({"type": "auth", "token": token}),
             json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def post(text, addr=None):
    """글 하나를 보낸다. 연결이나 쓰기가 실패하면 InboxUnavailable."""
    addr = addr or address()
    if not addr:
        raise InboxUnavailable("CLAUDE_CODE_MESSAGING_SOCKET 이 없습니다.")
    data = _payload(text)
    try:
        if addr.startswith("\\\\.\\pipe\\") or addr.startswith("//./pipe/"):
            with open(addr, "r+b", buffering=0) as f:
                f.write(data)
        else:
            path = addr[4:] if addr.startswith("uds:") else addr
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.settimeout(10)
                s.connect(path)
                s.sendall(data)
            finally:
                s.close()
    except OSError as e:
        raise InboxUnavailable("inbox 에 쓰지 못했습니다: %s" % e)
    return len(data)
