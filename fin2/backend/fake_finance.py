"""연습용 API 응답 — 키·네트워크 없이 Tool 과 에이전트를 돌려 볼 때 쓴다 (제출 코드에는 넣지 않는다)

    python -m backend.tools --fake
    FAKE_SCENARIO=empty python -m backend.agent --fake    (예금 상품이 하나도 없는 경우: 재계획 상한 시험)
requests.get 을 가로채 흉내 낸 응답을 돌려준다. 상품 C 는 기본금리가 비어 있다.
"""
import os
import requests

FAKE_KEY = "FAKEKEY123"

class _Resp:
    def __init__(self, body, code=200):
        self.body, self.status_code = body, code
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error for url: http://x/?auth={FAKE_KEY}")
    def json(self):
        return self.body

BASE = [{"fin_prdt_cd": c, "kor_co_nm": b, "fin_prdt_nm": n, "spcl_cnd": s} for c, b, n, s in [
    ("A", "가나은행", "특판 정기예금", "신규고객 우대 1.3%p"),
    ("B", "다라은행", "다이렉트 예금", "우대조건 없음"),
    ("C", "마바은행", "금리비공개 예금", "해당사항 없음")]]
OPTS = [("A", 12, 2.1, 4.1), ("B", 12, 3.5, 3.5), ("C", 12, None, 3.0),
        ("A", 6, 2.0, 3.8), ("A", 24, 2.3, 4.0), ("A", 36, 2.5, 3.9)]

def fake_get(url, params=None, timeout=None):
    p = params or {}
    if "finlife" in url:
        if p.get("auth") != FAKE_KEY:
            return _Resp({"result": {"err_cd": "010", "err_msg": "인증키 오류"}})
        opts = [] if os.getenv("FAKE_SCENARIO") == "empty" else [
            {"fin_prdt_cd": c, "save_trm": str(t), "intr_rate": r, "intr_rate2": m} for c, t, r, m in OPTS]
        return _Resp({"result": {"err_cd": "000", "baseList": BASE, "optionList": opts}})
    if "koreaexim" in url:
        if p.get("authkey") != FAKE_KEY:
            return _Resp([{"result": 3}])
        return _Resp([{"cur_unit": "USD", "deal_bas_r": "1,338.8", "tts": "1,352.18", "ttb": "1,325.41"},
                      {"cur_unit": "JPY(100)", "deal_bas_r": "900.1", "tts": "917.0", "ttb": "883.2"}])
    if "ecos.bok.or.kr" in url:
        if FAKE_KEY not in url:
            return _Resp({"RESULT": {"CODE": "INFO-100", "MESSAGE": "인증키 오류"}})
        return _Resp({"StatisticSearch": {"row": [{"TIME": "20261116", "DATA_VALUE": "3.0"}]}})
    return _Resp({}, 500)

def install():
    for k in ("FINLIFE_API_KEY", "KOREAEXIM_API_KEY", "ECOS_API_KEY"):
        os.environ[k] = FAKE_KEY
    requests.get = fake_get
