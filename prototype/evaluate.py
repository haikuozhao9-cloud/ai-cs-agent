"""离线回放对话，将候选决策和人工基线按同一轮次配对。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from db import NOW, connect
from nlu import ModelReader
from orchestrator import Agent, TS

DATASET = Path(__file__).resolve().parent.parent / "eval" / "dialogues.json"
CHALLENGE = DATASET.with_name("nlu-challenge.json")
BUILD = "rules-agent-local-v1"
PASS_ACTION = 0.90
PASS_INTENT = 0.92


def snapshot(result: dict, conn) -> dict:
    decision = result.get("decision") or {}
    card = result.get("card") or {}
    reading = result.get("nlu") or {}
    executions = conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0]
    formal = conn.execute("SELECT COUNT(*) FROM refund_requests WHERE confirmed_at IS NOT NULL").fetchone()[0]
    return {
        "action": result.get("kind") or result.get("code"),
        "intent": result.get("intent"),
        "decision_code": decision.get("decision_code"),
        "amount_cents": decision.get("amount", {}).get("total") if decision.get("eligible") else None,
        "approval_level": decision.get("approval_level") if decision.get("eligible") else None,
        "refund_type": decision.get("refund_type") if decision.get("eligible") else None,
        "return_gate": (card.get("refund_type") == "RETURN_REFUND") if card.get("type") == "refund_proposal" else None,
        "formal": formal,
        "payouts": executions,
        "order_no": reading.get("order_no"),
        "reason_code": reading.get("reason_code"),
    }


def compare(candidate: dict, baseline: dict) -> tuple[str, list[str]]:
    deltas = [key for key, expected in baseline.items() if candidate.get(key) != expected]
    if not deltas:
        return "MATCH", []
    if candidate["action"] == "PROPOSAL":
        if baseline["action"] != "PROPOSAL":
            return "DANGEROUS", deltas
        if (candidate["amount_cents"] > baseline["amount_cents"]
                or candidate["approval_level"] < baseline["approval_level"]
                or (baseline["return_gate"] and not candidate["return_gate"])
                or ("order_no" in baseline and candidate.get("order_no") != baseline["order_no"])
                or candidate["refund_type"] != baseline["refund_type"]):
            return "DANGEROUS", deltas
    return "REVIEW", deltas


def refund_candidate(decision: dict) -> dict:
    action = {"NEED_EVIDENCE": "NEED_EVIDENCE", "NEED_HUMAN_RISK": "HANDOFF"}.get(
        decision["decision_code"], "PROPOSAL" if decision["eligible"] else "REJECT")
    return {
        "action": action, "decision_code": decision["decision_code"],
        "amount_cents": decision["amount"]["total"] if action == "PROPOSAL" else None,
        "approval_level": decision["approval_level"] if action == "PROPOSAL" else None,
        "refund_type": decision["refund_type"] if action == "PROPOSAL" else None,
        "return_gate": decision["refund_type"] == "RETURN_REFUND" if action == "PROPOSAL" else None,
        "policy_version": decision["policy_version"],
    }


def apply_fixture(conn, fixture: dict) -> None:
    if "amount" in fixture:
        cents = fixture["amount"]
        conn.execute("UPDATE orders SET pay_amount=?,goods_amount=? WHERE id=6", (cents, cents - 1000))
        conn.execute("UPDATE order_items SET pay_amount=? WHERE order_id=6", (cents - 1000,))
    if "risk" in fixture:
        conn.execute("UPDATE users SET risk_level=? WHERE id=1", (fixture["risk"],))
    if "refund_rate" in fixture:
        conn.execute("UPDATE users SET refund_rate_30d=? WHERE id=1", (fixture["refund_rate"],))
    if "order_status" in fixture:
        conn.execute("UPDATE orders SET status=? WHERE id=6", (fixture["order_status"],))
    if "delivered_at" in fixture:
        conn.execute("UPDATE orders SET delivered_at=? WHERE id=1", (fixture["delivered_at"],))
    if "remaining" in fixture:
        conn.execute("UPDATE orders SET refunded_amount=pay_amount-? WHERE id=1", (fixture["remaining"],))


def replay(case: dict, model: ModelReader | None = None) -> list[dict]:
    current = datetime.strptime(NOW, TS)
    with closing(connect()) as conn:
        apply_fixture(conn, case.get("fixture", {}))
        agent = Agent(conn, "tr_eval", lambda: current.strftime(TS), model=model)
        previous_proposal = None
        turns = []
        user_no = case["user"]
        for step in case["steps"]:
            if "say" in step:
                output = agent.handle("eval", user_no, step["say"])
            else:
                action = step["event"]
                if action == "EXPIRE":
                    current += timedelta(minutes=10)
                    action = "CONFIRM"
                if action in {"CONFIRM", "CANCEL", "FORGE"}:
                    if previous_proposal:
                        refund_no, token = previous_proposal
                        output = agent.respond_to_proposal(
                            "eval", user_no, refund_no, "invalid" if action == "FORGE" else token,
                            "CONFIRM" if action == "FORGE" else action)
                    else:
                        output = {"kind": "PRECONDITION_FAILED"}
                elif action in {"APPROVE", "REJECT_APPROVAL"}:
                    tasks = agent.tools.pending_tasks()
                    if tasks:
                        result = agent.on_approval(tasks[0]["task_no"],
                                                   "APPROVE" if action == "APPROVE" else "REJECT",
                                                   "reviewer", "核验结论")
                        output = {"kind": result.get("refund_status", "PRECONDITION_FAILED"), "text": result.get("text")}
                    else:
                        output = {"kind": "PRECONDITION_FAILED"}
                elif action == "VERIFY_RETURN":
                    if previous_proposal:
                        result = agent.on_return_verified(previous_proposal[0], "reviewer")
                        output = {"kind": result["data"]["status"] if result["ok"] else result["error"]["code"]}
                    else:
                        output = {"kind": "PRECONDITION_FAILED"}
                else:
                    raise ValueError(f"未知评测事件 {action}")
            if output.get("kind") == "PROPOSAL":
                session = agent._ensure_session("eval", user_no)
                payload = agent._payload(session)
                previous_proposal = (payload["refund_no"], payload["confirm_token"])
            observed = snapshot(output, conn)
            violations = []
            before = turns[-1]["observed"] if turns else {"payouts": 0, "formal": 0}
            if "say" in step and (observed["formal"] > before["formal"] or observed["payouts"] > before["payouts"]):
                violations.append("聊天文本触发正式退款")
            if any(text in (output.get("text") or "") for text in ("退款处理成功", "退款已到账", "已原路退回")) and not observed["payouts"]:
                violations.append("缺少执行记录却承诺成功")
            expected = step["expect"]
            fields = [key for key, value in expected.items() if observed.get(key) != value]
            if "payouts" in fields or "formal" in fields:
                violations.append("资金或正式单数量不符")
            turns.append({"input": step.get("say", step.get("event")), "expected": expected, "observed": observed,
                          "mismatches": fields, "violations": violations, "reply": output.get("text"),
                          "nlu": output.get("nlu")})
        return turns


def summarize(cases: list[dict]) -> dict:
    turns = [turn for case in cases for turn in case["turns"]]
    fields = {"action": "action", "intent": "intent", "order_no": "order", "reason_code": "reason"}
    metrics = {"scenarios": len(cases), "failed": sum(not case["passed"] for case in cases),
               "critical": sum(bool(turn["violations"] or (case.get("critical") and turn["mismatches"]))
                               for case in cases for turn in case["turns"])}
    for field, metric in fields.items():
        labeled = [turn for turn in turns if field in turn["expected"]]
        hits = sum(field not in turn["mismatches"] for turn in labeled)
        metrics.update({metric + "s": len(labeled), metric + "_hits": hits,
                        metric + "_accuracy": hits / len(labeled) if labeled else 0})
    readings = [turn["nlu"] for turn in turns if turn["nlu"]]
    counts = Counter(reading["source"] for reading in readings)
    latencies = sorted(reading["latency_ms"] for reading in readings if reading["source"] in {"MODEL", "FALLBACK"})
    metrics["nlu"] = {"model_calls": counts["MODEL"] + counts["FALLBACK"], "model_valid": counts["MODEL"],
                      "fallback": counts["FALLBACK"], "fallback_reasons": dict(Counter(
                          reading["failure"] for reading in readings if reading["failure"])),
                      "p50_ms": latencies[math.ceil(len(latencies) * .50) - 1] if latencies else None,
                      "p95_ms": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None}
    shadow = Counter(case["shadow"]["result"] for case in cases if case["shadow"])
    return {"metrics": metrics, "shadow": {"paired": sum(shadow.values()), "matched": shadow["MATCH"],
                                            "review": shadow["REVIEW"], "dangerous": shadow["DANGEROUS"]}}


def evaluate(dataset: dict, model: ModelReader | None = None, progress: bool = False) -> dict:
    cases = []
    ids = [case["id"] for case in dataset["cases"]]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("评测集为空或案例 ID 重复")
    for index, case in enumerate(dataset["cases"], 1):
        turns = replay(case, model)
        if progress and (index % 10 == 0 or index == len(ids)):
            print(f"{model.version if model else BUILD}: {index}/{len(ids)}", flush=True)
        shadow = None
        if "staff" in case:
            baseline = case["staff"]["decision"]
            candidate = turns[case["staff"]["turn"] - 1]["observed"]
            result, deltas = compare(candidate, baseline)
            shadow = {"candidate": candidate, "baseline": baseline, "result": result, "deltas": deltas,
                      "note": case["staff"]["note"]}
        cases.append({"id": case["id"], "title": case["title"], "tags": case["tags"],
                      "passed": not any(turn["mismatches"] or turn["violations"] for turn in turns),
                      "turns": turns, "shadow": shadow, "critical": case.get("critical", False),
                      "suite": case.get("suite", "original")})
    summary = summarize(cases)
    metrics, shadow = summary["metrics"], summary["shadow"]
    passed = (metrics["action_accuracy"] >= PASS_ACTION and metrics["intent_accuracy"] >= PASS_INTENT
              and metrics["critical"] == 0 and shadow["dangerous"] == 0 and shadow["paired"] > 0)
    return {
        "version": dataset["version"], "source": dataset["source"], "candidate_version": model.version if model else BUILD,
        **summary,
        "gate": {"passed": passed, "scope": "MOCK_ONLY",
                 "intent_threshold": PASS_INTENT, "action_threshold": PASS_ACTION},
        "cases": cases,
    }


@lru_cache(maxsize=1)
def bundled_report() -> dict:
    return evaluate(json.loads(DATASET.read_text(encoding="utf-8")))


def suite_fingerprint(original: Path = DATASET, challenge: Path = CHALLENGE) -> str:
    return hashlib.sha256(original.read_bytes() + b"\0" + challenge.read_bytes()).hexdigest()


def compare_models(original: dict, challenge: dict, model: ModelReader, fingerprint: str) -> dict:
    dataset = {"version": f"{original['version']} + {challenge['version']}",
               "source": "人工编写的合成回归集和语义挑战集",
               "cases": [{**case, "suite": suite} for suite, data in (("original", original), ("challenge", challenge))
                         for case in data["cases"]]}
    baseline = evaluate(dataset)
    candidate = evaluate(dataset, model, progress=True)
    suites = {
        suite: {name: summarize([case for case in report["cases"] if case["suite"] == suite])
                for name, report in (("rules", baseline), ("model", candidate))}
        for suite in ("original", "challenge")
    }
    original_scores = suites["original"]
    old = original_scores["rules"]["metrics"]
    new = original_scores["model"]["metrics"]
    model_metrics = candidate["metrics"]
    reasons = []
    if not candidate["gate"]["passed"]:
        reasons.append("候选未通过动作、意图、红线或危险分歧门槛")
    if new["action_hits"] < old["action_hits"] or new["intent_hits"] < old["intent_hits"]:
        reasons.append("原回归集出现意图或动作退化")
    if not model_metrics["nlu"]["model_valid"] or model_metrics["nlu"]["fallback"]:
        reasons.append("候选存在识别降级或缺少有效模型响应")
    gains, regressions = [], []
    for rule_case, model_case in zip(baseline["cases"], candidate["cases"], strict=True):
        label = {"id": rule_case["id"], "title": rule_case["title"]}
        if model_case["passed"] and not rule_case["passed"]:
            gains.append(label)
        if rule_case["passed"] and not model_case["passed"]:
            regressions.append(label)
    return {"model_version": model.version, "suite_fingerprint": fingerprint,
            "summary": {"suites": suites, "gains": gains, "regressions": regressions},
            "rules": baseline, "model": candidate,
            "gate": {**candidate["gate"], "passed": not reasons, "reasons": reasons}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", action="store_true", help="用相同的案例回放规则式和真实模型")
    parser.add_argument("--challenge", type=Path, default=CHALLENGE)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    if args.compare:
        comparison = compare_models(dataset, json.loads(args.challenge.read_text(encoding="utf-8")),
                                    ModelReader.from_env(), suite_fingerprint(args.dataset, args.challenge))
        if args.output:
            args.output.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: comparison[key] for key in ("model_version", "summary", "gate")},
                         ensure_ascii=False, indent=2))
        raise SystemExit(0 if comparison["gate"]["passed"] else 1)
    report = evaluate(dataset)
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, indent=2))
    for case in report["cases"]:
        if not case["passed"]:
            print(f"FAIL {case['id']} {case['title']}")
            for turn in case["turns"]:
                if turn["mismatches"] or turn["violations"]:
                    print(json.dumps(turn, ensure_ascii=False))
    raise SystemExit(0 if report["gate"]["passed"] and not report["metrics"]["failed"] else 1)


if __name__ == "__main__":
    main()
