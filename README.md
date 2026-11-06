# agent-hub-ao

사용자는 허브 Claude Code 세션(Manager) 하나와 대화합니다. Manager 는 프로젝트 폴더마다 작업 세션을 띄워 일을 맡기고(`claude --bg`, `SendMessage`), 보고를 받아 사용자에게 알리고 기록합니다. 대시보드는 떠 있는 세션과 세션 사이 메시지를 그림으로 보여 줍니다.

[holiday0726/claude-hub-plugin](https://github.com/holiday0726/claude-hub-plugin)(agent-hub)을 바탕으로 허브 세션 이름 붙이기, 대시보드 다른 세션 묶음, humanizer·git-message 스킬을 더했습니다.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/architecture-dark.png">
  <img alt="허브 구성도: 사용자는 허브와 대화하고, 허브는 레인 세션에 배정하고 보고를 받습니다" src="docs/images/architecture-light.png" width="900">
</picture>

## 역할

| 누가 | 하는 일 | 하지 않는 일 |
|---|---|---|
| 사용자 | Manager 와 대화, 결정, 권한 승인 | 작업 세션에 직접 지시(하면 기록에서 빠짐) |
| Manager(허브 세션) | 할 일과 완료 조건 정하기, 작업 세션 띄우기와 배정, 보고 확인, `STATUS.md`·`decisions.json` 기록 | 프로젝트 저장소 코드를 직접 고치기 |
| 작업 세션 | 맡은 저장소에서 일하고 `[보고]` 로 알림, 판단이 필요하면 Manager 에 질문 | 허브 폴더 파일 고치기, 사용자 대신 결정 |
| 대시보드 | 세션 상태, 대화, 세션 사이 메시지 흐름, 결정, 현황, 할 일 보기 | 세션에 입력 보내기 |

## 빠른 시작

필요한 것: Claude Code(native Windows 는 2.1.234 이상), Python 3.8 이상.

### 1. 플러그인 설치

```sh
claude plugin marketplace add smdeveloper7/agent-hub-ao
claude plugin install agent-hub-ao@agent-hub-ao
```

저장소를 받아 고쳐 쓰려면 받은 폴더를 마켓플레이스로 붙입니다. 플러그인은 그 폴더를 그대로 읽으니 설치 뒤 옮기지 않습니다.

```powershell
git clone https://github.com/smdeveloper7/agent-hub-ao.git C:\Users\<나>\agent-hub-ao
claude plugin marketplace add C:/Users/<나>/agent-hub-ao
claude plugin install agent-hub-ao@agent-hub-ao `
  --config python=C:/Users/<나>/AppData/Local/Programs/Python/Python313/python.exe `
  --config hub_dir=C:/Users/<나>/hub
```

설정값을 비워 설치했다면 아무 폴더에서 `claude` 를 열고 `/agent-hub-ao:setup` 을 부릅니다. Python 을 찾고, 허브 폴더를 만들고, 플러그인 설정값을 채웁니다.

### 2. Manager 열기

```sh
cd ~/hub
claude
```

훅이 세션 이름을 `hub` 로 붙이고 대시보드(http://127.0.0.1:4990)를 띄웁니다.

## 사용법

Manager 에게 평소 말로 맡깁니다.

```
ax-flow 에서 WF4-A2 응답에 고장확률이 48% 로 고정돼 나오는 원인을 찾아줘. 고치기 전에 원인부터 보고해.
```

1. Manager 가 할 일, 완료 조건, 작업 폴더를 정합니다. 모호하면 먼저 묻습니다.
2. 그 폴더에서 작업 세션을 띄웁니다(`claude --bg --name task-<주제>`). 한 번 쓰고 닫을 세션은 `agents.json` 의 `task`, 계속 둘 세션은 `resident` 에 적습니다.
3. `SendMessage` 로 `[배정]` 을 보냅니다. 작업 세션은 끝나면 `[보고]` 로 답합니다. 형식은 `templates/hub/COORDINATION.md` "배정과 보고" 입니다.
4. Manager 가 완료 조건과 근거를 확인하고 사용자에게 알립니다. 결정이 필요한 것은 모아서 묻고 `decisions.json` 에 남깁니다.

작업 세션이 권한 승인을 기다리며 멈추면 대시보드에 "입력 필요" 로 보입니다. `claude attach <이름>` 으로 열어 승인합니다. 처음 쓰는 폴더는 한 번 직접 `claude` 로 열어 신뢰를 승인해야 그 폴더에 세션을 띄울 수 있습니다.

### 보는 곳

| 화면 | 보는 것 |
|---|---|
| Manager 창 | 대화, 보고 요약, 질문 |
| 대시보드 에이전트 탭 | 떠 있는 세션, 상태(진행 중, 입력 필요, 지시 대기, 끝남), 세션 대화 기록 |
| 대시보드 흐름 탭 | Manager 를 가운데 두고 세션 사이 메시지(배정, 보고)를 그래프와 목록으로 |
| 대시보드 결정·현황·할 일 탭 | `decisions.json`, `STATUS.md`, 세션별 할 일 목록 |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/dashboard-flow-dark.png">
  <img alt="대시보드 흐름 탭: 허브를 가운데 두고 세션 사이 메시지를 화살표로 보여 줍니다" src="docs/images/dashboard-flow-light.png" width="480">
</picture>

## 훅과 설정

| 훅 | 언제 | 하는 일 |
|---|---|---|
| `open_dashboard.py` | 세션 시작 | 허브 폴더에서 열리면 대시보드를 띄우고, `orca` 가 켜져 있으면 Orca 탭으로 엽니다 |
| `hub_title.py` | 세션 시작 | 허브 폴더에서 열린 세션에 이름(기본 `hub`)을 붙입니다. 이미 이름이 있으면 두고, 설정을 비우면 붙이지 않습니다 |
| `hub_only_write.py` | Edit, Write 직전 | 허브 폴더 밖 세션이 허브 폴더 파일을 고치지 못하게 막습니다 |
| `git-message-style.py` | Bash, PowerShell 직전 | git 커밋·PR 문구가 개조식 규칙을 어기면 막습니다(`CLAUDE_GIT_STYLE=off` 로 잠시 끔) |

| 설정값 | 필수 | 뜻 | 기본값 |
|---|---|---|---|
| `python` | 예 | 훅을 돌릴 Python. Windows 는 `python.exe` 절대 경로 | 없음 |
| `hub_dir` | 예 | 허브 폴더 절대 경로 | 없음 |
| `dashboard_port` | 아니오 | 대시보드 포트 | `4990` |
| `orca` | 아니오 | Orca 탭으로 대시보드 열기 | 켬 |
| `hub_title` | 아니오 | 허브 세션에 자동으로 붙일 이름(`/rename` 과 같음). 비우면 붙이지 않음 | `hub` |

허브 폴더 구성, 대시보드 화면, 설정 예시는 [templates/hub/CLAUDE.md](templates/hub/CLAUDE.md), [dashboard/README.md](dashboard/README.md), [examples/](examples/) 에 있습니다. 허브 규칙의 감독 스킬은 [Addy-skills](https://github.com/holiday0726/Addy-skills) 플러그인을 함께 설치해 씁니다(`/agent-hub-ao:setup` 이 설치).

## 시험

```sh
python dashboard/test_server.py
python tests/test_hub_title.py
python tests/test_hub_only_write.py
python tests/test_open_dashboard.py
python tests/test_setup_hub.py
python tests/test_style_basic.py
python tests/test_style_multi.py
```

## 저장소 구조

```
.claude-plugin/   plugin.json, marketplace.json
dashboard/        server.py, index.html, test_server.py, vendor/
docs/             README 그림(images/)과 원본(diagrams/)
examples/         hub.settings.json, supervisor.settings.json, user.settings.json
hooks/            hooks.json, open_dashboard.py, hub_title.py, hub_only_write.py, git-message-style.py, hubpath.py
scripts/          setup_hub.py
skills/           setup/, humanizer/, git-message/
templates/hub/    CLAUDE.md, COORDINATION.md, STATUS.md, agents.json, decisions.json, dev-occupancy.json
tests/            허브 훅·설치 스크립트 시험
```

## 라이선스와 출처

MIT 라이선스입니다([LICENSE](LICENSE)).

- 허브 규칙 틀, 대시보드, 허브 훅, setup·git-message 스킬: [holiday0726/claude-hub-plugin](https://github.com/holiday0726/claude-hub-plugin), MIT
- `skills/humanizer`: [blader/humanizer](https://github.com/blader/humanizer), MIT
- `dashboard/vendor/xterm.js`, `xterm.css`: [xterm.js](https://github.com/xtermjs/xterm.js) 5.5.0, MIT
- `dashboard/vendor/WantedSansVariable.woff2`: [Wanted Sans](https://github.com/wanteddev/wanted-sans) 1.0.3, SIL OFL 1.1
