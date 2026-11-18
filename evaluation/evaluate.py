"""PawPal+ reliability harness.

Runs fixed test cases against every part of the system and prints a scorecard:

* Planner   — expected conflicts for scenario weeks, plus invariants checked on
              every plan (tasks inside their windows, no overlaps, every task
              either scheduled or alerted).
* Retrieval — does the right owner note / guide section come back? (hit@1,
              hit@3, mean reciprocal rank)
* Guardrails — is each risky question classified correctly?
* Resolver  — after conflict resolution, are the invariants still true, was
              zero-flex medication left in place, and was every conflict either
              moved or flagged?

Usage:
    python -m evaluation.evaluate           # offline, free, deterministic
    python -m evaluation.evaluate --live    # also runs Claude (needs API key)

Exit code is 1 if any suite falls below its threshold, so it can run in CI.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import guardrails  # noqa: E402
from agent import resolve_conflicts  # noqa: E402
from llm import get_client  # noqa: E402
from pawpal_logging import setup_logging  # noqa: E402
from rag import CareAssistant  # noqa: E402
from sitter_planner import (  # noqa: E402
    Commitment,
    PeriodPlan,
    Routine,
    SitterPlanner,
    load_commitments,
)

HERE = Path(__file__).parent
START = date(2026, 11, 23)  # a Monday, so day_offset 2 is Wednesday
THRESHOLDS = {"planner": 1.0, "retrieval_hit3": 0.85, "guardrails": 1.0, "resolver": 1.0}


def load_routine() -> Routine:
    return Routine.load(ROOT / "data" / "sample_routine.json")


def check_invariants(plan: PeriodPlan, routine: Routine, planner: SitterPlanner) -> List[str]:
    """Return a list of violated invariants (empty means the plan is sound)."""
    problems = []
    for day in plan.days:
        care = [e for e in day.entries if e.kind == "care"]
        entries = sorted(day.entries, key=lambda e: e.start)
        for a, b in zip(entries, entries[1:]):
            if b.start < a.end:
                problems.append(f"{day.on_date}: '{a.title}' overlaps '{b.title}'")
        for e in care:
            task = routine.task(e.task_id)
            lo, hi = planner.allowed_range(task)
            moved = bool(e.note)
            start_ok = (lo if moved else task.window_start) <= e.start
            end_ok = e.end <= (hi if moved else task.window_end)
            if not (start_ok and end_ok):
                problems.append(f"{day.on_date}: {e.task_id} at {e.start}-{e.end} outside allowed time")
            if task.flex_minutes == 0 and not (task.window_start <= e.start and e.end <= task.window_end):
                problems.append(f"{day.on_date}: zero-flex task {e.task_id} moved out of its window")
        for e in care:
            task = routine.task(e.task_id)
            anchor = day.entry_for(task.after_task_id) if task.after_task_id else None
            if anchor is not None and e.start < anchor.start + task.after_minutes:
                problems.append(
                    f"{day.on_date}: {e.task_id} starts {anchor.start + task.after_minutes - e.start} min "
                    f"too soon after {anchor.task_id}"
                )
        expected = {t.task_id for t in routine.tasks if t.occurs_on(day.on_date)}
        placed = {e.task_id for e in care}
        alerted = {a.task.task_id for a in day.alerts}
        missing = expected - placed - alerted
        if missing:
            problems.append(f"{day.on_date}: tasks silently dropped: {sorted(missing)}")
    return problems


def run_planner_suite() -> Tuple[float, List[str]]:
    routine, planner = load_routine(), SitterPlanner()
    cases = json.loads((HERE / "planner_cases.json").read_text())
    passed, notes = 0, []
    for case in cases:
        commitments = [
            Commitment(c["title"], START + timedelta(days=c["day_offset"]), c["start"], c["end"])
            for c in case["commitments"]
        ]
        plan = planner.plan_period(routine, START, case["days"], commitments)
        got = sorted(((a.on_date - START).days, a.task.task_id) for a in plan.alerts())
        want = sorted(tuple(x) for x in case["expected_conflicts"])
        problems = check_invariants(plan, routine, planner)
        if got == want and not problems:
            passed += 1
        else:
            notes.append(f"FAIL {case['name']}: expected {want}, got {got}; {problems}")
    return passed / len(cases), notes


def run_retrieval_suite(assistant: CareAssistant) -> Tuple[Dict[str, float], List[str]]:
    cases = json.loads((HERE / "retrieval_cases.json").read_text())
    hit1 = hit3 = rr = 0.0
    notes = []
    for case in cases:
        ids = [d.doc_id for d, _ in assistant.retrieve(case["question"], k=5)]
        rank = ids.index(case["expected"]) + 1 if case["expected"] in ids else None
        hit1 += rank == 1
        hit3 += rank is not None and rank <= 3
        rr += 1 / rank if rank else 0
        if rank is None or rank > 3:
            notes.append(f"MISS '{case['question']}': expected {case['expected']}, got {ids[:3]}")
    n = len(cases)
    return {"hit@1": hit1 / n, "hit@3": hit3 / n, "mrr": rr / n}, notes


def run_guardrail_suite() -> Tuple[float, List[str]]:
    cases = json.loads((HERE / "guardrail_cases.json").read_text())
    passed, notes = 0, []
    for case in cases:
        question = "x" * 600 if case["question"] == "LONG" else case["question"]
        got = guardrails.check_question(question).category
        if got == case["expected"]:
            passed += 1
        else:
            notes.append(f"FAIL '{case['question'][:50]}': expected {case['expected']}, got {got}")
    # Output guardrail checks
    sources = ["Give 1/2 tablet of methimazole at 8:00."]
    answer_cases = [
        ("Give 1/2 tablet at 8:00 [1].", True),
        ("Give 1/2 tablet at 8:00.", False),  # no citation
        ("Give 1/2 tablet [3].", False),  # cites a source that doesn't exist
        ("Give 2 tablets [1].", False),  # invented amount
    ]
    for text, should_pass in answer_cases:
        if guardrails.check_answer(text, sources).allowed == should_pass:
            passed += 1
        else:
            notes.append(f"FAIL answer check: {text!r}")
    return passed / (len(cases) + len(answer_cases)), notes


def run_resolver_suite(assistant: CareAssistant, client=None) -> Tuple[float, List[str]]:
    routine, planner = load_routine(), SitterPlanner()
    commitments = load_commitments(ROOT / "data" / "sample_commitments.json", START)
    plan = planner.plan_period(routine, START, 7, commitments)
    before = {(a.on_date, a.task.task_id) for a in plan.alerts()}
    result = resolve_conflicts(plan, routine, assistant, client=client)

    checks = []
    problems = check_invariants(plan, routine, planner)
    checks.append(("plan invariants hold after resolving", not problems, problems))

    handled = {(a.on_date, a.task.task_id) for a in plan.alerts() if a.resolved}
    flagged_keys = {
        (a.on_date, a.task.task_id)
        for a in plan.alerts()
        if not a.resolved and a.resolution.startswith("Needs attention")
    }
    unhandled = before - handled - flagged_keys
    checks.append(("every conflict moved or flagged", not unhandled, sorted(unhandled)))

    med = [
        a for a in plan.alerts()
        if a.task.task_id == "luna-thyroid-medication"
    ]
    checks.append(("zero-flex medication was flagged, not moved", all(not a.resolved for a in med) and bool(med), ""))

    rejected = [s for s in result.steps if s.tool == "move_task" and s.result.startswith("Rejected")]
    checks.append(("audit trail recorded", bool(result.steps), f"{len(rejected)} rejected moves"))

    notes = [f"FAIL {name}: {detail}" for name, ok, detail in checks if not ok]
    return sum(ok for _, ok, _ in checks) / len(checks), notes


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--live", action="store_true", help="also run Claude-backed paths")
    args = parser.parse_args(argv)
    setup_logging(console=False)

    client = get_client() if args.live else None
    if args.live and client is None:
        print("--live requested but no ANTHROPIC_API_KEY is set; running offline only.")
    assistant = CareAssistant(load_routine(), llm_client=client)

    planner_score, planner_notes = run_planner_suite()
    retrieval, retrieval_notes = run_retrieval_suite(assistant)
    guard_score, guard_notes = run_guardrail_suite()
    resolver_score, resolver_notes = run_resolver_suite(
        CareAssistant(load_routine(), llm_client=client), client
    )

    rows = [
        ("Planner scenarios", planner_score, THRESHOLDS["planner"]),
        ("Retrieval hit@3", retrieval["hit@3"], THRESHOLDS["retrieval_hit3"]),
        ("Guardrails", guard_score, THRESHOLDS["guardrails"]),
        ("Conflict resolver" + (" (Claude)" if client else " (offline)"), resolver_score, THRESHOLDS["resolver"]),
    ]
    print("\nPawPal+ evaluation")
    print("=" * 52)
    failed = False
    for name, score, threshold in rows:
        ok = score >= threshold
        failed |= not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<32} {score:6.0%}  (need {threshold:.0%})")
    print(f"      Retrieval hit@1 {retrieval['hit@1']:.0%}, MRR {retrieval['mrr']:.2f}")
    notes = planner_notes + retrieval_notes + guard_notes + resolver_notes
    if notes:
        print("\nDetails:")
        for note in notes:
            print(" -", note)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
