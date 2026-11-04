"""agent-hub-ao: agent-hub 와 Agent Orchestrator(AO) 연동.

Main Claude Code 세션(허브)이 AO 프로젝트 Supervisor(orchestrator 세션)에 일을 맡기고,
결과와 질문을 받아 사용자에게 보고하고, Obsidian Vault 에 기록한다.

- AO 는 세션 실행 상태와 Worker 수명 주기를 맡는다. 이 모듈은 AO CLI 만 부른다.
- 이 모듈은 업무 목표, 완료 조건, 사용자 질문과 결정, 전달 기록을 맡는다(SQLite 하나).
- Python 표준 라이브러리만 쓴다(3.8 이상).
"""

__version__ = "0.1.0"
