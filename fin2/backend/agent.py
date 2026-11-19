"""메인 Agent (분석 → 실행 → 판단 → [재계획] → 응답)

  analyze   모델: 의도 · 조건 추출, 쓸 Tool 고르기 (계획서 Plan)
  dispatch  코드: Tool 실행 (이미 받은 결과는 재사용)
  judge     코드: 세금 · 목표액 계산과 충족 판단, 재계획 여부
  replan    코드: 해당 기간 상품이 없을 때 가까운 기간으로 바꿔 다시 (상한 MAX_REPLAN)
  respond   모델: 확정된 사실을 문장으로 + 근거 + 면책 안내(코드)

모델과 이벤트 발행은 make_graph 에 주입한다. 계산은 모두 코드가 한다.
실행(리허설):  python -m backend.agent --fake
"""
import os
import time
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from backend.tools import TOOLS

TAX = 0.154
TERMS = [6, 12, 24, 36]
DISCLAIMER = ("※ 공개 API 데이터를 바탕으로 한 참고용 계산이며 전문가의 금융 조언을 대신하지 않습니다. "
              "실제 가입 전 각 금융사 공시를 확인하세요.")
NEED = {"lump_deposit": ["amount", "months"], "monthly_goal": ["monthly", "target", "months"],
        "fx_compare": ["amount", "months"]}
LABEL = {"amount": "예치 금액", "monthly": "월 저축액", "target": "목표 금액", "months": "기간"}
DEFAULT_TOOLS = {"lump_deposit": ["get_deposit_rates"], "monthly_goal": ["get_deposit_rates", "get_base_rate"],
                 "fx_compare": ["get_exchange_rate", "get_deposit_rates"]}
KIND = {"lump_deposit": "deposit", "monthly_goal": "saving", "fx_compare": "deposit"}
MEMORY: dict[str, dict] = {}            # 세션별 누적 조건. Tool 원본 응답은 보관하지 않는다

class State(TypedDict, total=False):
    user: str; sid: str; cond: dict; intent: str; tools: list; term: int | None
    alt_terms: list; replans: int; results: dict; notes: list
    verdict: dict; answer: str; plan_draft: dict | None

# ── 계산 (코드가 한다) ───────────────────────────────────────
def interest_after_tax(principal, rate_pct, months):
    return principal * rate_pct / 100 * months / 12 * (1 - TAX)

def monthly_factor(rate_pct, months):          # 적금(단리): 월 납입액 1원당 세후 만기 수령액
    return months + rate_pct / 100 * months * (months + 1) / 24 * (1 - TAX)

def find(results, tool, term=None):
    for v in results.values():
        if v["tool"] == tool and (term is None or v["args"].get("term_months") == term):
            return v["result"]
    return None

def make_graph(llm_plan, llm_answer, emit=lambda *a, **k: None):
    max_replan = int(os.getenv("MAX_REPLAN", 3))

    def run_tool(name, **args):                       # 호출 앞뒤로 기록
        emit("tool.call", tool=name, args=args)
        t0 = time.time()
        r = TOOLS[name].run(**args)
        emit("tool.result", tool=name, ok=r["ok"], code=None if r["ok"] else r["error"]["code"],
             ms=int((time.time() - t0) * 1000))
        return r

    def analyze(s):                                   # ① 모델: 무엇을 부를까
        sid, prev = s["sid"], MEMORY.get(s["sid"], {})
        try:
            plan = llm_plan(s["user"], prev)
        except Exception as e:
            missing_key = str(e) == "MISSING_LLM_KEY"
            emit("error", code="MISSING_LLM_KEY" if missing_key else "LLM_ERROR", kind=type(e).__name__)
            return {"answer": "설정 안내: .env 에 GMS_API_KEY 를 넣어 주세요." if missing_key
                    else "요청을 분석하지 못했어요. 잠시 후 다시 시도해 주세요."}
        intent = plan.get("intent") or "chat"
        cond = {**prev, **{k: plan[k] for k in ("amount", "monthly", "target", "months", "currency")
                           if plan.get(k) is not None}}
        names = [t for t in plan.get("tools", []) if t in TOOLS] if intent != "chat" else []
        for t in DEFAULT_TOOLS.get(intent, []):       # 모델이 빼먹은 필수 Tool 은 보충하고 기록
            if t not in names:
                names.append(t)
                emit("decision", step="analyze", note=f"필수 Tool 보충: {t}")
        missing = [k for k in NEED.get(intent, []) if not cond.get(k)]
        if intent != "chat":
            cond["_intent"] = intent
        MEMORY[sid] = cond
        emit("decision", step="analyze", intent=intent, reason=plan.get("reason"), tools=names,
             cond={k: v for k, v in cond.items() if not k.startswith("_")}, missing=missing)
        out = {"cond": cond, "intent": intent, "tools": names, "term": cond.get("months"), "replans": 0,
               "results": {}, "notes": [],
               "alt_terms": sorted([t for t in TERMS if t != cond.get("months")],
                                   key=lambda t: abs(t - (cond.get("months") or 12)))}
        if missing:
            out["answer"] = f"계산에 필요한 정보가 부족해요: {', '.join(LABEL[k] for k in missing)}. 알려주시면 바로 계산할게요."
        return out

    def build_args(name, s):
        if name == "get_deposit_rates":
            return {"kind": KIND[s["intent"]], "term_months": s["term"]}
        if name == "get_exchange_rate":
            return {"currency": s["cond"].get("currency") or "USD"}
        return {}

    def dispatch(s):                                  # ② 코드: Tool 실행
        results = dict(s.get("results", {}))
        for name in s["tools"]:
            args = build_args(name, s)
            key = f"{name}:{sorted(args.items())}"
            if key not in results:                    # 재계획 시 이미 받은 결과는 다시 부르지 않는다
                results[key] = {"tool": name, "args": args, "result": run_tool(name, **args)}
        return {"results": results}

    def judge(s):                                     # ③ 코드: 제약 판단
        if s["intent"] == "chat":
            return {"verdict": {"kind": "chat"}}
        c, term, res, notes = s["cond"], s["term"], s["results"], list(s["notes"])
        dep = find(res, "get_deposit_rates", term)
        if not dep["ok"]:
            code = dep["error"]["code"]
            if code == "NO_DATA" and s["replans"] < max_replan and s["alt_terms"]:
                return {"verdict": {"kind": "retry"}}
            reason = (f"{c.get('months')}개월 및 가까운 기간 상품을 찾지 못했고 재계획 상한({max_replan}회)에 도달했어요. 기간을 조정해 볼까요?"
                      if code == "NO_DATA" else f"금리 데이터를 가져오지 못했어요(코드 {code}). 키 설정이나 잠시 후 재시도를 확인하세요.")
            return {"verdict": {"kind": "fatal", "reason": reason}}
        p, excluded = dep["data"]["products"][0], dep["data"]["excluded_no_rate"]
        if term != c.get("months"):
            notes.append(f"요청한 {c['months']}개월 상품이 없어 {term}개월 상품으로 대체했습니다(재계획 {s['replans']}회).")
        if p["rate"] is None:
            notes.append("기본금리가 공개되지 않아 우대 최고금리 기준으로만 계산했습니다.")
        v = {"kind": s["intent"], "product": p, "term": term, "excluded_no_rate": excluded}
        if s["intent"] == "lump_deposit":
            a = c["amount"]
            v["max"] = {"after_tax": round(interest_after_tax(a, p["max_rate"], term))}
            v["base"] = {"after_tax": round(interest_after_tax(a, p["rate"], term))} if p["rate"] is not None else None
            for k in ("max", "base"):
                if v[k]:
                    v[k]["total"] = a + v[k]["after_tax"]
            v["amount"] = a
        elif s["intent"] == "monthly_goal":
            m, tgt = c["monthly"], c["target"]
            fmax = monthly_factor(p["max_rate"], term)
            fbase = monthly_factor(p["rate"], term) if p["rate"] is not None else None
            ref = fbase or fmax
            v.update(monthly=m, target=tgt, max_total=round(m * fmax),
                     base_total=round(m * fbase) if fbase else None, achieved=m * ref >= tgt,
                     need_monthly=round(tgt / ref), gap=round(tgt - m * ref))
            br = find(res, "get_base_rate")
            if br and br["ok"]:
                v["base_rate"] = br["data"]["base_rate"]
            else:
                notes.append("기준금리를 확인하지 못해 제외했습니다.")
        else:
            fx = find(res, "get_exchange_rate")
            if not fx["ok"]:
                return {"verdict": {"kind": "fatal", "reason": f"환율을 가져오지 못했어요(코드 {fx['error']['code']})."}}
            f, a = fx["data"], c["amount"]
            spread = (f["buy_tts"] - f["base"]) / f["base"] * 100
            gain = interest_after_tax(a, p["rate"] if p["rate"] is not None else p["max_rate"], term)
            v.update(amount=a, tts=f["buy_tts"], fx_date=f["basis_date"], fx_estimated=f["is_estimated"],
                     spread=round(spread, 2), krw_gain=round(gain), breakeven_pct=round(gain / a * 100 + 2 * spread, 2))
        emit("decision", step="judge", kind=v["kind"], product=f"{p['bank']} {p['product']}", term=term)
        return {"verdict": v, "notes": notes}

    def route(s):
        return "replan" if s["verdict"]["kind"] == "retry" else "respond"

    def replan(s):                                    # 코드: 가까운 기간으로 다시
        n, alt = s["replans"] + 1, list(s["alt_terms"])
        new = alt.pop(0)
        emit("replan", count=n, reason=f"{s['term']}개월 상품 없음", from_term=s["term"], to_term=new)
        return {"replans": n, "term": new, "alt_terms": alt}

    def after_analyze(s):
        return "respond" if s.get("answer") else "dispatch"

    def respond(s):                                   # ④ 모델: 문장으로 (+ 코드가 근거·면책을 덧붙임)
        if s.get("answer"):
            return {"answer": s["answer"] + "\n\n" + DISCLAIMER, "plan_draft": None}
        v, c, notes = s["verdict"], s["cond"], s.get("notes", [])
        facts = {"조건": {k: x for k, x in c.items() if not k.startswith("_")}, "판단": v, "참고": notes}
        if v["kind"] == "fatal":
            return {"answer": v["reason"] + "\n\n" + DISCLAIMER, "plan_draft": None}
        try:
            text = llm_answer(s["user"], facts)
        except Exception as e:
            emit("error", code="LLM_ERROR", kind=type(e).__name__)
            text = "\n".join(summary(v))
        parts = [text]
        if v["kind"] != "chat":
            parts += ["", "[판단 근거]"] + evidence(v, notes, s["results"])
        parts += ["", DISCLAIMER]
        draft = None if v["kind"] == "chat" else {
            "title": TITLES[v["kind"]], "kind": v["kind"], "amount": c.get("amount") or c.get("monthly"),
            "months": v["term"], "summary": "\n".join(summary(v))}
        return {"answer": "\n".join(parts), "plan_draft": draft}

    def traced(name, fn):                             # 단계 진입 · 종료 기록
        def wrapper(s):
            emit("step.start", step=name)
            out = fn(s)
            emit("step.end", step=name)
            return out
        return wrapper

    g = StateGraph(State)
    for n, f in [("analyze", analyze), ("dispatch", dispatch), ("judge", judge),
                 ("replan", replan), ("respond", respond)]:
        g.add_node(n, traced(n, f))
    g.add_edge(START, "analyze")
    g.add_conditional_edges("analyze", after_analyze, {"respond": "respond", "dispatch": "dispatch"})
    g.add_edge("dispatch", "judge")
    g.add_conditional_edges("judge", route, {"replan": "replan", "respond": "respond"})
    g.add_edge("replan", "dispatch")
    g.add_edge("respond", END)
    return g.compile()

TITLES = {"lump_deposit": "목돈 예치 계획", "monthly_goal": "월 저축 목표 계획", "fx_compare": "환전 vs 예금 비교"}

def won(n):
    return f"{n:,}원"

def summary(v):
    if v["kind"] == "chat":
        return ["저축 계획, 목표액 달성 여부, 환전 vs 예금 비교를 도와드려요."]
    p = v["product"]
    name = f"{p['bank']} {p['product']} ({v['term']}개월)"
    if v["kind"] == "lump_deposit":
        lines = [f"추천: {name}"]
        if v["base"]:
            lines.append(f"· 기본금리 {p['rate']}%: 세후 이자 {won(v['base']['after_tax'])}, 만기 {won(v['base']['total'])}")
        lines.append(f"· 우대 모두 받으면 {p['max_rate']}%: 세후 이자 {won(v['max']['after_tax'])}, 만기 {won(v['max']['total'])}")
        return lines + [f"우대조건: {p.get('special') or '정보 없음'}"]
    if v["kind"] == "monthly_goal":
        r = "달성 가능합니다." if v["achieved"] else f"{won(v['gap'])} 부족합니다. 월 {won(v['need_monthly'])} 이상 넣어야 달성해요."
        return [f"{name} 기준 월 {won(v['monthly'])} 저축 → 목표 {won(v['target'])}: {r}"]
    return [f"오늘({v['fx_date']}) 달러 살 때 환율 {v['tts']:,}원, 환전 스프레드 약 {v['spread']}%.",
            f"국내 예금({name}) 세후 이자 약 {won(v['krw_gain'])}. 달러 예금이 더 낫려면 환전 왕복 비용을 포함해 "
            f"약 {v['breakeven_pct']}% 이상의 수익(이자+환율 변동)이 필요합니다. 달러 예금 금리와 미래 환율은 API로 확인할 수 없어 가정하지 않았어요."]

def evidence(v, notes, results):
    p = v["product"]
    out = [f"· 금리: 금융감독원 — {p['bank']} {p['product']}, 기본 {p['rate'] if p['rate'] is not None else '미공개'}% / 최고 {p['max_rate']}%"]
    if v.get("excluded_no_rate"):
        out.append(f"· 제외: 금리 정보가 없는 상품 {v['excluded_no_rate']}개는 판단하지 않았습니다.")
    if v["kind"] == "fx_compare":
        out.append(f"· 환율: 수출입은행 {v['fx_date']}" + (" (추정: 최근 영업일 값)" if v["fx_estimated"] else ""))
    if v.get("base_rate") is not None:
        out.append(f"· 기준금리: 한국은행 {v['base_rate']}%")
    return out + [f"· 참고: {n}" for n in notes]

# ── 리허설: python -m backend.agent --fake ───────────────────
if __name__ == "__main__":
    import sys
    if "--fake" in sys.argv:
        from backend import fake_finance; fake_finance.install()
    from backend.llm import fake_plan, fake_answer
    from backend.events import bus
    log = []
    bus.subscribe(log.append)
    for i, q in enumerate(["안녕", "500만원을 12개월 뒤에 써야 해", "100만원을 6개월 달러 환전 vs 예금",
                           "매월 50만원씩 모아 3년 뒤 2000만원을 모을 수 있을까", "500만원 18개월 예금",
                           "예금 알려줘"]):
        log.clear()
        app = make_graph(fake_plan, fake_answer, lambda e, **p: bus.publish(e, f"t{i}", **p))
        out = app.invoke({"user": q, "sid": f"t{i}"})
        print("\n■", q, "\n", out["answer"].split("\n\n")[0][:200])
        print("  이벤트:", [m["event"] + (":" + str(m["payload"].get("tool") or m["payload"].get("step") or "")) for m in log if m["event"] != "step.end"])
