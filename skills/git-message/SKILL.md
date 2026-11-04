---
name: git-message
description: Use when writing or rewriting a git commit message, merge/squash commit, tag annotation, or pull request title and body - 커밋 메시지, 머지 커밋, PR 제목과 본문 문구를 작성할 때. Also use after the git-message-style hook blocks a commit.
---

# git 문구 작성 규칙

커밋·머지·태그·PR 문구는 **개조식**으로 쓴다. 읽는 사람이 훑고 필요한 줄만 고르게 한다.

## 규칙

| 항목 | 규칙 |
|---|---|
| 형식 | 제목 1줄, 빈 줄 1개, 불릿 본문 |
| 제목 | 72칸 이하, 끝 마침표 없음, 무엇을 바꿨는지 명사구로 |
| 본문 | 각 줄 `- ` 로 시작, 한 줄 100칸 이하 |
| 종결 | 명사형(`추가` `수정` `반영` `정정` `복구`). `~했습니다` `~한다` 금지 |
| 이모지 | 금지. 이모티콘, 채팅 자모도 금지 |
| 한 항목 | 한 가지 사실만. 둘이면 줄을 나눈다 |

## 본문에 넣을 것

- **무엇이 바뀌었나** — 파일명이 아니라 동작과 수치로
- **왜 바꿨나** — 진단이 자명하지 않을 때만
- **확인한 근거** — 행 수, 테스트 결과, 재실행 결과

diff 를 읽으면 아는 것(파일 목록, 함수명 나열)은 적지 않는다.

## 예시

커밋

```
브론즈 파서 시그니처 3종 복구

- 이관 때 누락된 관리손익 시그니처 복원, xl_table 175 -> 189
- p2_powder 748 -> 700행 기준 재적용
- sales_excel 재실행 시 중복 적재 0건 확인

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

머지도 같다. 제목에 병합 대상, 본문에 이 병합이 들여오는 변화를 항목으로 적는다.

```
feature/gold 병합 - 골드 DAG 자산 연쇄

- hsgld 집계 6표 적재 태스크 추가
- refine 완료 후 자산 연쇄로 자동 기동
- schedule=None, 기본 dry_run=true 유지
```

## 흔한 실수

| 쓴 것 | 고칠 것 |
|---|---|
| `수정했습니다` | `수정` |
| `여러 가지 버그 수정` | 버그별로 줄을 나누고 무엇이 틀렸는지 적는다 |
| 제목 끝 `.` | 뺀다 |
| 본문이 산문 한 문단 | 사실 단위로 쪼개 불릿으로 |
| `feat: ...` 앞 이모지 | 제거 |
| `parse.py, load.py 수정` | 파일명 대신 동작 변화를 적는다 |

## 강제 장치

agent-hub-ao 플러그인의 `hooks/git-message-style.py` 가 `PreToolUse` 에서 `git` 의 commit·merge·tag·revert 와
`gh pr` 의 create·edit 을 검사하고 위반 시 실행을 막는다. 막히면 문구를 고쳐 다시 실행한다.
일시 해제는 환경변수 `CLAUDE_GIT_STYLE=off`.

한 Bash 호출에 여러 git 명령이 있어도 **명령마다 따로** 검사한다. 회귀 테스트는
플러그인 저장소 `tests/` 에 둘 있다 — 훅을 고치면 둘 다 돌린다.

```bash
python3 tests/test_style_basic.py   # 기본 규칙 36건
python3 tests/test_style_multi.py   # 한 호출에 여러 명령 7건
```

## 도구 쪽 함정

- **`git merge -F -` 는 stdin 을 읽지 못한다.** `could not read file '-'` 로 죽는다.
  `git commit` 은 되지만 merge 는 안 된다 — 머지 메시지가 길면 파일에 쓰고 `-F <파일>` 로 준다
- `-m` 에 줄바꿈을 넣는 것은 commit·merge 모두 된다. 짧은 메시지에는 그게 편하다
