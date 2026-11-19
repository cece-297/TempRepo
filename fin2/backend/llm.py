"""모델 자리: 진짜 GMS 모델(llm_plan · llm_answer)과 리허설용 대역(fake_plan · fake_answer)"""
import json
import os
import re
from typing import Literal

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from backend.tools import catalog

class Plan(BaseModel):
    """모델이 채울 계획서 양식. 말하지 않은 값은 null (추측 금지)"""
    intent: Literal["lump_deposit", "monthly_goal", "fx_compare", "chat"] = Field(
        description="lump_deposit=목돈을 예치할 최고금리 찾기, monthly_goal=매월 저축으로 목표액 달성 여부, "
                    "fx_compare=환전 후 외화 예금 vs 국내 예금 비교, chat=인사·사용법 등 Tool 불필요")
    reason: str = Field(description="그렇게 분류한 이유 한 문장")
    tools: list[str] = Field(description="부를 Tool 이름들. 필요 없으면 빈 리스트")
    amount: int | None = Field(None, description="목돈(원)")
    monthly: int | None = Field(None, description="매월 저축액(원)")
    target: int | None = Field(None, description="목표 금액(원)")
    months: int | None = Field(None, description="기간(개월). '1년'은 12")
    currency: str | None = Field(None, description="통화 코드. 말하지 않았으면 null")

def _llm():
    key = os.getenv("GMS_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("MISSING_LLM_KEY")
    return ChatOpenAI(model=os.getenv("GMS_MODEL", "gpt-4o-mini"), api_key=key,
                      base_url=os.getenv("GMS_BASE_URL") or os.getenv("OPENAI_BASE_URL")
                      or "https://gms.ssafy.io/gmsapi/api.openai.com/v1", timeout=30, max_retries=1)

def llm_plan(user: str, prev: dict) -> dict:
    planner = _llm().with_structured_output(Plan)
    prompt = ("금융 플래너 에이전트의 분석 단계다. 요청에서 의도와 조건을 뽑고, 아래 Tool 설명을 보고 필요한 Tool만 고른다.\n"
              "후속 요청('기간만 6개월로')이면 이전 의도를 유지하고 바뀐 조건만 채운다.\n"
              f"Tool 목록: {json.dumps(catalog(), ensure_ascii=False)}\n"
              f"이전 조건: {json.dumps(prev, ensure_ascii=False)}\n요청: {user}")
    return planner.invoke(prompt).model_dump()

def llm_answer(user: str, facts: dict) -> str:
    prompt = ("아래 '확정된 사실'만 사용해 한국어로 친절하고 간결하게 답하라. 숫자를 바꾸거나 지어내지 말고, "
              "사실에 없는 상품·환율·미래 전망은 말하지 않는다. 사실이 비어 있으면(인사 등) 짧게 응대하고 "
              "저축 계획·목표액 달성·환전 vs 예금 비교를 도울 수 있다고 안내한다.\n"
              f"사용자: {user}\n확정된 사실: {json.dumps(facts, ensure_ascii=False, default=str)}")
    return _llm().invoke(prompt).content

# ── 대역 배우 (키 없이 그래프 리허설) ─────────────────────────
def fake_plan(user, prev):
    num = lambda p: (int(m.group(1)) if (m := re.search(p, user)) else None)
    months = num(r"(\d+)\s*개월") or ((num(r"(\d+)\s*년") or 0) * 12 or None)
    if "안녕" in user:
        return {"intent": "chat", "reason": "인사", "tools": []}
    if "환전" in user or "달러" in user:
        return {"intent": "fx_compare", "reason": "환전 비교", "tools": ["get_exchange_rate"],
                "amount": (num(r"(\d+)\s*만원") or 0) * 10000 or None, "months": months, "currency": "USD"}
    if "매월" in user:
        return {"intent": "monthly_goal", "reason": "월 저축", "tools": ["get_deposit_rates"],
                "monthly": (num(r"매월\s*(\d+)") or 0) * 10000 or None,
                "target": (num(r"(\d+)\s*만원\s*(?:을|를)?\s*모") or 0) * 10000 or None, "months": months}
    return {"intent": "lump_deposit", "reason": "목돈 예치", "tools": ["get_deposit_rates"],
            "amount": (num(r"(\d+)\s*만원") or 0) * 10000 or None, "months": months}

def fake_answer(user, facts):
    return "(대역 응답) " + json.dumps(facts.get("판단", {}), ensure_ascii=False, default=str)[:200]
