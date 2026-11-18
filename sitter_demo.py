"""Command-line demo of PawPal+ Sitter.

    python sitter_demo.py            # weekly view
    python sitter_demo.py --daily    # first day only

Uses Claude when ANTHROPIC_API_KEY is set, otherwise the offline paths.
"""

import argparse
from datetime import date, timedelta

from agent import resolve_conflicts
from llm import get_client
from pawpal_logging import setup_logging
from rag import CareAssistant
from sitter_planner import Routine, SitterPlanner, fmt, fmt_date, load_commitments


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily", action="store_true", help="plan one day instead of a week")
    args = parser.parse_args()
    setup_logging(console=False)

    client = get_client()
    routine = Routine.load("data/sample_routine.json")
    start = date(2026, 11, 23)  # Monday
    commitments = load_commitments("data/sample_commitments.json", start)
    days = 1 if args.daily else 7
    plan = SitterPlanner().plan_period(routine, start, days, commitments)

    print(f"PawPal+ Sitter — {routine.owner_name}'s pets, {fmt_date(start)} "
          f"to {fmt_date(start + timedelta(days=days - 1))}")
    print(f"AI: {'Claude' if client else 'offline mode'}\n")

    print("CONFLICT ALERTS")
    for alert in plan.alerts():
        icon = "!!" if alert.severity == "critical" else " !"
        print(f" {icon} {alert.message}")
    if not plan.alerts():
        print("  none")

    assistant = CareAssistant(routine, llm_client=client)
    result = resolve_conflicts(plan, routine, assistant, client=client)
    print("\nRESOLVER")
    print(" ", result.summary)
    for line in result.moved:
        print("  moved:  ", line)
    for line in result.flagged:
        print("  flagged:", line)

    print("\nSCHEDULE")
    for day in plan.days:
        print(f"  {fmt_date(day.on_date)}")
        for e in day.sorted_entries():
            who = "YOU" if e.kind == "sitter" else e.pet_name
            note = "  (moved)" if e.note else ""
            print(f"    {fmt(e.start)}-{fmt(e.end)}  {who:<6} {e.title}{note}")
        for a in day.alerts:
            if not a.resolved:
                print(f"    -- NOT COVERED: {a.task.pet_name}'s {a.task.title} ({a.task.window_label})")

    print("\nASK ABOUT CARE")
    for q in ["How much does Mochi eat for breakfast?", "Can I give Luna an extra pill tonight?"]:
        answer = assistant.answer(q)
        print(f"  Q: {q}\n  A: {answer.text.splitlines()[0]}")
        for line in answer.text.splitlines()[1:2]:
            print(f"     {line[:110]}...")


if __name__ == "__main__":
    main()
