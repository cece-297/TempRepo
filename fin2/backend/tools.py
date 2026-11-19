"""외부 API Tool 3종 (금융 플래너)

실행:  python -m backend.tools            진짜 API 로 시험
       python -m backend.tools --fake     키·네트워크 없이 연습용 응답으로

  ① ok / fail      성공도 실패도 같은 모양
  ② Tool           이름 · 설명 · 입력 규격(JSON Schema) · 함수. run() 이 예외를 한 곳에서 처리
  ③ call_api       공통 호출(타임아웃 · 오류 문구에서 키 가리기)
  ④ 외부 API Tool  get_deposit_rates · get_exchange_rate · get_base_rate
  ⑤ TOOLS·catalog  등록과, 모델에게 넘기는 목록
실패 코드: BAD_ARGUMENT INVALID_KEY NOT_FOUND NO_DATA UPSTREAM_ERROR
"""
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import requests
from dotenv import load_dotenv

load_dotenv()

# ── ① 약속 ───────────────────────────────────────────────
def ok(data, **meta):
    return {"ok": True, "data": data, **meta}

def fail(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}

# ── ② Tool 명함 ──────────────────────────────────────────
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable

    def run(self, **kwargs):
        try:
            return self.func(**kwargs)
        except TypeError:
            return fail("BAD_ARGUMENT", "입력값이 규약과 다릅니다.")
        except Exception:
            return fail("UPSTREAM_ERROR", "Tool 실행 중 오류입니다.")

# ── 키 가리기 ────────────────────────────────────────────
KEY_ENVS = ["FINLIFE_API_KEY", "KOREAEXIM_API_KEY", "ECOS_API_KEY"]
PARAM = re.compile(r"(?i)\b(key|apikey|token|auth|authkey)=([^&\s'\"]+)")

def scrub(text) -> str:
    text = str(text)
    for name in KEY_ENVS:
        secret = os.getenv(name, "")
        if secret:
            text = text.replace(secret, "***")
    return PARAM.sub(lambda m: f"{m.group(1)}=***", text)

# ── ③ 공통 호출 ──────────────────────────────────────────
def call_api(url, params=None):
    """(body, error) 를 돌려준다. 오류 원문은 화면이 아니라 로그에만, 키는 가려서."""
    try:
        res = requests.get(url, params=params or {}, timeout=10)
        res.raise_for_status()
        return res.json(), None
    except (requests.RequestException, ValueError) as e:
        print("[log]", scrub(e))
        return None, fail("UPSTREAM_ERROR", "외부 API를 부르지 못했습니다.")

def _key(env):
    return os.getenv(env) or None

# ── ④ 외부 API Tool ──────────────────────────────────────
def get_deposit_rates(kind: str, term_months: int | None = None):
    """금융감독원 금융상품한눈에: 예금/적금. 금리를 모르는 상품은 값을 채우지 않고 제외 개수만 알린다."""
    if kind not in ("deposit", "saving"):
        return fail("BAD_ARGUMENT", "kind는 deposit 또는 saving 입니다.")
    key = _key("FINLIFE_API_KEY")
    if not key:
        return fail("INVALID_KEY", "FINLIFE_API_KEY가 없습니다.")
    ep = "depositProductsSearch" if kind == "deposit" else "savingProductsSearch"
    body, error = call_api(f"https://finlife.fss.or.kr/finlifeapi/{ep}.json",
                           {"auth": key, "topFinGrpNo": "020000", "pageNo": 1})
    if error:
        return error
    result = (body or {}).get("result") or {}
    if result.get("err_cd") != "000":
        return fail("INVALID_KEY" if result.get("err_cd") in ("010", "011", "012") else "UPSTREAM_ERROR",
                    "금융감독원 API 응답을 확인하세요.")
    names = {p["fin_prdt_cd"]: p for p in result.get("baseList", [])}
    rows, excluded = [], 0
    for o in result.get("optionList", []):
        if term_months and int(o.get("save_trm") or 0) != term_months:
            continue
        if o.get("intr_rate2") is None:            # 최고금리를 모르면 판단하지 않고 제외
            excluded += 1
            continue
        b = names.get(o.get("fin_prdt_cd"), {})
        base = o.get("intr_rate")
        rows.append({"bank": b.get("kor_co_nm"), "product": (b.get("fin_prdt_nm") or "").strip(),
                     "term": int(o["save_trm"]),
                     "rate": float(base) if base is not None else None,   # 기본금리: 모르면 None
                     "max_rate": float(o["intr_rate2"]),
                     "special": b.get("spcl_cnd")})
    if not rows:
        return fail("NO_DATA", f"{term_months or '전체'}개월 {kind} 상품이 없습니다.")
    rows.sort(key=lambda x: -x["max_rate"])
    return ok({"products": rows[:10], "excluded_no_rate": excluded, "term_months": term_months})

def get_exchange_rate(currency: str = "USD"):
    """한국수출입은행 환율. 오늘 값이 없으면 직전 영업일로 찾고 is_estimated 로 밝힌다."""
    if not currency:
        return fail("BAD_ARGUMENT", "currency는 필수입니다.")
    key = _key("KOREAEXIM_API_KEY")
    if not key:
        return fail("INVALID_KEY", "KOREAEXIM_API_KEY가 없습니다.")
    today = date.today()
    for back in range(6):
        day = today - timedelta(days=back)
        body, error = call_api("https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON",
                               {"authkey": key, "searchdate": day.strftime("%Y%m%d"), "data": "AP01"})
        if error:
            return error
        if not isinstance(body, list) or not body:
            continue
        if not any("cur_unit" in r for r in body):
            if body[0].get("result") == 3:
                return fail("INVALID_KEY", "KOREAEXIM_API_KEY를 확인하세요.")
            continue
        for r in body:
            if str(r.get("cur_unit", "")).startswith(currency):
                try:
                    f = lambda v: float(str(v).replace(",", ""))
                    return ok({"currency": currency, "base": f(r["deal_bas_r"]), "buy_tts": f(r["tts"]),
                               "sell_ttb": f(r["ttb"]), "basis_date": day.isoformat(),
                               "is_estimated": back > 0})
                except (KeyError, ValueError):
                    return fail("NO_DATA", f"{currency} 환율 값이 비어 있습니다.")
        return fail("NOT_FOUND", f"{currency} 통화를 찾을 수 없습니다.")
    return fail("NO_DATA", "최근 6일간 환율 데이터가 없습니다.")

def get_base_rate():
    """한국은행 ECOS 기준금리 (최근 값)."""
    key = _key("ECOS_API_KEY")
    if not key:
        return fail("INVALID_KEY", "ECOS_API_KEY가 없습니다.")
    end = date.today()
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/400/722Y001/D/"
           f"{(end - timedelta(days=400)):%Y%m%d}/{end:%Y%m%d}/0101000")
    body, error = call_api(url)
    if error:
        return error
    if "StatisticSearch" not in (body or {}):
        code = ((body or {}).get("RESULT") or {}).get("CODE", "")
        return fail("INVALID_KEY" if code in ("INFO-100", "INFO-200") else "NO_DATA", "기준금리를 가져오지 못했습니다.")
    rows = body["StatisticSearch"].get("row") or []
    if not rows or not rows[-1].get("DATA_VALUE"):
        return fail("NO_DATA", "기준금리 값이 비어 있습니다.")
    return ok({"base_rate": float(rows[-1]["DATA_VALUE"]), "as_of": rows[-1]["TIME"]})

# ── ⑤ 등록 ───────────────────────────────────────────────
TOOLS = {t.name: t for t in [
    Tool(name="get_deposit_rates",
         description="예금·적금 상품의 금리(기본/최고)와 우대조건이 필요할 때 사용한다. "
                     "목돈 예치, 매월 저축 목표, 국내 예금 비교 요청에서 금리 근거를 얻을 때 부른다.",
         parameters={"type": "object",
                     "properties": {"kind": {"type": "string", "enum": ["deposit", "saving"],
                                             "description": "deposit=목돈 예치, saving=매월 적립"},
                                    "term_months": {"type": "integer", "description": "가입 기간(개월). 6·12·24·36"}},
                     "required": ["kind"]},
         func=get_deposit_rates),
    Tool(name="get_exchange_rate",
         description="환전이나 달러 예금처럼 오늘 환율(매매기준율, 살 때 환율)이 필요할 때 사용한다.",
         parameters={"type": "object",
                     "properties": {"currency": {"type": "string", "description": "통화 코드. 예: USD, EUR"}}},
         func=get_exchange_rate),
    Tool(name="get_base_rate",
         description="현재 한국은행 기준금리가 필요할 때 사용한다. 금리 수준을 설명하는 배경 정보로 쓴다.",
         parameters={"type": "object", "properties": {}},
         func=get_base_rate),
]}

def catalog():
    """모델에게 넘기는 것은 이름 · 설명 · 입력 규격뿐"""
    return [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in TOOLS.values()]

if __name__ == "__main__":
    if "--fake" in sys.argv:
        from backend import fake_finance; fake_finance.install()
    for c in catalog():
        print("[Tool]", c["name"], "—", c["description"][:40], "…")

    def trial(name, tool, **args):
        r = TOOLS[tool].run(**args)
        d = r["data"] if r["ok"] else r["error"]["code"]
        print(f"{r['ok']!s:<5} {tool:<18} {name} →", str(d)[:90])

    trial("정상", "get_deposit_rates", kind="deposit", term_months=12)
    trial("결과 없음(99개월)", "get_deposit_rates", kind="deposit", term_months=99)
    trial("잘못된 kind", "get_deposit_rates", kind="stock")
    trial("모르는 인자", "get_deposit_rates", kind="deposit", foo=1)
    trial("정상", "get_exchange_rate", currency="USD")
    trial("없는 통화", "get_exchange_rate", currency="ZZZ")
    trial("정상", "get_base_rate")
    saved = {k: os.environ.pop(k, "") for k in KEY_ENVS}
    trial("키 없음", "get_deposit_rates", kind="deposit")
    trial("키 없음", "get_exchange_rate")
    trial("키 없음", "get_base_rate")
    os.environ.update({k: v for k, v in saved.items() if v})
