"""UI pieces shared by the owner and sitter screens."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, timedelta
from typing import Any, List, Optional, Sequence

import streamlit as st

from agent import resolve_conflicts
from rag import CareAssistant
from sitter_planner import (
    Commitment,
    Routine,
    SitterPlanner,
    describe_days,
    fmt_date,
    gap_label,
    plan_to_rows,
)

FIVE_MIN = timedelta(minutes=5)
SPECIES = ["dog", "cat", "rabbit", "bird", "fish", "reptile", "other"]


def fmt12(minutes: int) -> str:
    """Minutes after midnight as 12-hour time: 0 -> '12:00 AM', 810 -> '1:30 PM', 1440 -> '12:00 AM (midnight)'."""
    if minutes == 24 * 60:
        return "12:00 AM (midnight)"
    hour, minute = divmod(minutes, 60)
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def half_hour_select(container: Any, label: str, default: int, end_of_day: bool = False) -> int:
    """A dropdown of 30-minute times shown as 12-hour clock; returns minutes after midnight.

    ``end_of_day`` offers 12:30 AM through midnight (for "until" fields)
    instead of 12:00 AM through 11:30 PM.
    """
    first = 30 if end_of_day else 0
    options = {fmt12(m): m for m in range(first, first + 24 * 60, 30)}
    labels = list(options)
    choice = container.selectbox(label, labels, index=labels.index(fmt12(default)))
    return options[choice]


def routine_table(routine: Routine) -> List[dict]:
    return [
        {
            "Pet": t.pet_name,
            "Task": t.title,
            "Window": t.window_label,
            "Minutes": t.duration_minutes,
            "Flex": f"±{t.flex_minutes}" if t.flex_minutes else "none",
            "Priority": t.priority,
            "Days": describe_days(t.days),
            "Depends on": (
                f"≥ {gap_label(t.after_minutes)} after {routine.task(t.after_task_id).pet_name}: "
                f"{routine.task(t.after_task_id).title}"
                if t.after_task_id and routine.task(t.after_task_id)
                else ""
            ),
            "Instructions": t.description,
        }
        for t in sorted(routine.tasks, key=lambda t: (t.window_start, t.pet_name))
    ]


def _field(label: str, value: str) -> None:
    if value:
        st.markdown(f"**{label}:** {value}")


def owner_info_card(routine: Routine) -> None:
    """Emergency info first, then contacts and each pet's profile."""
    if routine.emergency_procedures:
        st.error(f"**🚨 Emergency plan**\n\n{routine.emergency_procedures}")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Contacts")
        _field("Owner", f"{routine.owner_name} · {routine.owner_contact}".strip(" ·"))
        _field("Emergency contact", routine.emergency_contact)
    with c2:
        st.markdown("#### Vet")
        _field("Clinic", routine.vet_name)
        _field("Phone", routine.vet_phone)
        _field("Address", routine.vet_address)
    if routine.house_notes:
        st.info(f"**🏠 House notes:** {routine.house_notes}")

    for pet in routine.pets:
        title = f"🐾 {pet.name} — {pet.breed or pet.species}{', ' + pet.age if pet.age else ''}"
        with st.expander(title, expanded=True):
            a, b = st.columns(2)
            with a:
                _field("Personality & habits", pet.traits)
                _field("Requirements & rules", pet.requirements)
                _field("Other notes", pet.notes)
            with b:
                _field("Health", pet.health_conditions)
                _field("Medications", pet.medications)
                _field("Allergies", pet.allergies)
            tasks = [r for r in routine_table(routine) if r["Pet"] == pet.name]
            if tasks:
                st.dataframe(
                    [{k: v for k, v in r.items() if k != "Pet"} for r in tasks],
                    hide_index=True,
                    width="stretch",
                )


def _fingerprint(routine: Routine, commitments: Sequence[Commitment], start: date, days: int) -> str:
    payload = {
        "routine": routine.to_dict(),
        "commitments": [c.to_dict() for c in commitments],
        "start": start.isoformat(),
        "days": days,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def schedule_view(
    routine: Routine,
    commitments: Sequence[Commitment],
    start: date,
    num_days: int,
    client: Optional[Any],
) -> None:
    """Plan, conflict alerts, AI fix-up with audit trail, and the per-day timeline."""
    ss = st.session_state
    if not routine.tasks:
        st.info("The owner hasn't added any routine tasks for these pets yet.")
        return

    current = _fingerprint(routine, commitments, start, num_days)
    cached = ss.get("plan_cache")
    if cached is None or cached[0] != current:
        cached = (current, SitterPlanner().plan_period(routine, start, num_days, commitments))
        ss.plan_cache = cached
    plan = cached[1]
    result = None
    resolution = ss.get("resolution")
    if resolution and resolution[0] == current:
        _, plan, result = resolution

    alerts = plan.alerts()
    open_alerts = [a for a in alerts if not a.resolved]
    m1, m2, m3 = st.columns(3)
    m1.metric("Care tasks scheduled", sum(1 for d in plan.days for e in d.entries if e.kind == "care"))
    m2.metric("Conflicts", len(alerts))
    m3.metric("Still need a decision", len(open_alerts))

    for alert in open_alerts:
        box = st.error if alert.severity == "critical" else st.warning
        icon = "🚨" if alert.severity == "critical" else "⚠️"
        suggestion = alert.resolution[len("Needs attention: "):] if alert.resolution else ""
        box(f"{icon} {alert.message}" + (f"\n\n**Suggestion:** {suggestion}" if suggestion else ""))

    if alerts and result is None:
        label = "Fix conflicts with Claude" if client else "Fix conflicts (rule-based)"
        if st.button(label, type="primary"):
            with st.spinner("Working through conflicts…"):
                working = copy.deepcopy(plan)
                outcome = resolve_conflicts(
                    working, routine, CareAssistant(routine, llm_client=client), client=client
                )
            ss.resolution = (current, working, outcome)
            st.rerun()
    elif not alerts:
        st.success("No conflicts. Every task fits around your plans.")

    if result is not None:
        st.subheader("Assistant's changes")
        st.markdown(result.summary)
        for line in result.moved:
            st.success(f"✅ {line}")
        if result.stopped_early:
            st.caption("The assistant stopped early; remaining items were handled by built-in rules.")
        with st.expander(f"Audit trail ({len(result.steps)} steps)"):
            for i, step in enumerate(result.steps, start=1):
                st.markdown(f"**{i}. {step.tool}** `{json.dumps(step.input)}`")
                st.code(step.result, language=None)
        if st.button("Undo assistant changes"):
            ss.resolution = None
            st.rerun()

    st.subheader("Schedule")
    rows = plan_to_rows(plan)
    for day in plan.days:
        day_alerts = [a for a in day.alerts if not a.resolved]
        badge = f" — 🚨 {len(day_alerts)} conflict(s)" if day_alerts else ""
        with st.expander(f"{fmt_date(day.on_date)}{badge}", expanded=num_days == 1 or bool(day_alerts)):
            day_rows = [r for r in rows if r["Day"] == fmt_date(day.on_date)]
            if day_rows:
                st.dataframe(
                    [{k: v for k, v in r.items() if k != "Day"} for r in day_rows],
                    hide_index=True,
                    width="stretch",
                )
            for a in day_alerts:
                st.caption(f"Not scheduled: {a.task.pet_name}'s {a.task.title} ({a.task.window_label})")


def ask_view(routine: Routine, client: Optional[Any]) -> None:
    ss = st.session_state
    st.caption(
        "Answers come only from the owner's notes and the built-in care guides, with sources. "
        "Medication changes and emergencies are routed to the vet."
    )
    with st.form("ask", clear_on_submit=True):
        question = st.text_input("Your question", placeholder="How much does Mochi eat for breakfast?")
        asked = st.form_submit_button("Ask")
    history = ss.setdefault("qa_history", [])
    if asked:
        history.insert(0, (question, CareAssistant(routine, llm_client=client).answer(question)))

    for q, a in history[:10]:
        st.markdown(f"**Q: {q}**")
        if a.urgent:
            st.error(a.text)
        elif a.guardrail in {"medication", "human_medication", "too_long", "empty"}:
            st.warning(a.text)
        else:
            st.info(a.text)
        if a.sources:
            with st.expander(f"Sources ({'Claude' if a.used_llm else 'quoted directly'})"):
                for i, d in enumerate(a.sources, start=1):
                    st.markdown(f"**[{i}] {d.title}**  \n{d.full_text}")
        st.divider()
