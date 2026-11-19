# 머니 플래너 에이전트 (AI 코어 프로젝트 3회차)
금융 도메인 Agent 플래너. FastAPI + LangGraph + SQLite, 프론트는 정적 HTML.
**금융 도메인이므로 모든 답변 끝에 "전문가 금융 조언을 대신하지 않는다"는 안내가 붙습니다.**

## 실행 (프로젝트 폴더에서)
```
python -m pip install -r requirements.txt
cp .env.example .env          # 키 입력
python -m backend.tools        # 외부 API 3종 호출 시험 (--fake: 키 없이 연습용 응답)
python -m backend.agent --fake # 그래프 리허설 (모델 대역 + 연습용 응답)
python -m uvicorn backend.main:app --reload   # http://localhost:8000 , 문서: /docs
```

## 구조
- backend/tools.py        Tool 클래스 + 외부 API Tool 3종, ok/fail, 키 가리기(scrub), catalog()
- backend/llm.py          Plan 양식(llm_plan), 응답 문장(llm_answer), 리허설 대역(fake_*)
- backend/agent.py        LangGraph: analyze → dispatch → judge →(replan≤MAX_REPLAN)→ respond. 계산은 코드
- backend/events.py       EventBus (발행/구독)      backend/logger_agent.py  구독 저장, LLM 없음
- backend/main.py         FastAPI (대화·계획 CRUD·로그 조회/삭제). 로그 수정 API 없음
- backend/fake_finance.py 연습용 API 응답 (제출 코드에 넣지 않는 소품)
- frontend/index.html     대화 화면(실행 이력 포함) + 계획 관리 화면

## 외부 API (출처·이용 조건은 발급처에서 확인해 기록 — NF3007)
| API | 용도 | 호출 한도 / 약관 |
|---|---|---|
| 금융감독원 금융상품한눈에 (finlife.fss.or.kr) | 예·적금 금리, 우대조건 | (기재) |
| 한국수출입은행 환율 (oapi.koreaexim.go.kr) | 오늘 환율 | (기재) |
| 한국은행 ECOS (ecos.bok.or.kr) | 기준금리 | (기재) |
