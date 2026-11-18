"""Sitter screen: pick an owner and pets, add your own plans, get a schedule."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

import streamlit as st

from sitter_planner import Commitment, fmt, fmt_date
from storage import OwnerRecord, SittingJob, Store
from views.common import ask_view, half_hour_select, owner_info_card, schedule_view

NEW_JOB = "Start a new sitting job"


def _reset_job(owner_id: str, pet_names: list) -> None:
    ss = st.session_state
    ss.job_owner_id = owner_id
    ss.job_id = None
    ss.job_pets = list(pet_names)
    ss.job_start = date.today()
    ss.job_view = "Weekly"
    ss.job_commitments = []
    ss.resolution = None


def _load_job(job: SittingJob, pet_names: dict) -> None:
    ss = st.session_state
    ss.job_id = job.id
    ss.job_pets = [pet_names[p] for p in job.pet_ids if p in pet_names]
    ss.job_start = job.start_date
    ss.job_view = "Daily" if job.num_days == 1 else "Weekly"
    ss.job_commitments = list(job.commitments)
    ss.resolution = None


def _job_label(job: SittingJob, names: dict) -> str:
    pets = ", ".join(names.get(p, "?") for p in job.pet_ids)
    length = "1 day" if job.num_days == 1 else f"{job.num_days} days"
    return f"{fmt_date(job.start_date)} · {length} · {pets}"


def render(store: Store, client: Optional[Any]) -> None:
    ss = st.session_state
    st.title("🧑 Sitter")
    st.caption("Pick who you're sitting for, add your own plans, and get a schedule that works for both.")

    sitter = st.text_input("Your name", key="sitter_name", placeholder="So your saved jobs can be found later")

    owners = store.list_owners()
    if not owners:
        st.info("No owners have set up a profile yet. Ask the owner to open PawPal+ and choose **I'm an owner**.")
        return

    names = [o.name for o in owners]
    owner_labels = {
        (o.name if names.count(o.name) == 1 else f"{o.name} ({o.contact or o.id[:6]})"): o for o in owners
    }
    current = next((label for label, o in owner_labels.items() if o.id == ss.get("job_owner_id")), None)
    label = st.selectbox(
        "Are you sitting for one of these owners?",
        list(owner_labels),
        index=list(owner_labels).index(current) if current else None,
        placeholder="Choose an owner",
    )
    if label is None:
        st.caption("Don't see them? The owner needs to create a profile first.")
        return
    owner: OwnerRecord = owner_labels[label]
    pets = store.list_pets(owner.id)
    pet_names = {p.id: p.name for p in pets}
    pet_ids = {p.name: p.id for p in pets}
    if not pets:
        st.info(f"{owner.name} hasn't added any pets yet.")
        return
    if ss.get("job_owner_id") != owner.id:
        _reset_job(owner.id, list(pet_ids))

    # -- saved jobs --------------------------------------------------------- #
    jobs = store.list_jobs(owner_id=owner.id, sitter_name=sitter) if sitter.strip() else []
    if jobs:
        job_labels = {NEW_JOB: None}
        for j in jobs:
            job_labels[_job_label(j, pet_names)] = j
        current = next((k for k, j in job_labels.items() if j and j.id == ss.get("job_id")), NEW_JOB)
        choice = st.selectbox(
            "Continue a saved sitting job?",
            list(job_labels),
            index=list(job_labels).index(current),
        )
        picked = job_labels[choice]
        if (picked.id if picked else None) != ss.get("job_id"):
            if picked is None:
                _reset_job(owner.id, list(pet_ids))
            else:
                _load_job(picked, pet_names)
            st.rerun()

    # -- job setup ---------------------------------------------------------- #
    with st.container(border=True):
        st.markdown("#### Sitting details")
        ss.job_pets = [p for p in ss.job_pets if p in pet_ids]  # drop pets the owner removed
        st.multiselect("Which pets are you looking after?", list(pet_ids), key="job_pets")
        a, b = st.columns(2)
        a.date_input("Starts", key="job_start")
        b.radio("Plan", ["Daily", "Weekly"], key="job_view", horizontal=True,
                help="Daily plans one day; weekly plans seven days from the start date.")

    num_days = 1 if ss.job_view == "Daily" else 7
    start: date = ss.job_start
    end = start + timedelta(days=num_days - 1)
    if not ss.job_pets:
        st.warning("Choose at least one pet.")
        return

    with st.container(border=True):
        st.markdown(f"#### Your plans ({fmt_date(start)} – {fmt_date(end)})")
        st.caption("Class, appointments, work shifts — anything that takes you away from the pets.")
        with st.form("add_commitment", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
            title = c1.text_input("What")
            on = c2.date_input("Date", value=start, min_value=start, max_value=end)
            c_start = half_hour_select(c3, "From", default=12 * 60)
            c_end = half_hour_select(c4, "Until", default=13 * 60, end_of_day=True)
            if st.form_submit_button("Add"):
                try:
                    ss.job_commitments.append(Commitment(title, on, c_start, c_end))
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
        in_period = sorted(
            [c for c in ss.job_commitments if start <= c.on_date <= end],
            key=lambda c: (c.on_date, c.start),
        )
        for index, c in enumerate(in_period):
            k1, k2 = st.columns([5, 1])
            k1.write(f"**{fmt_date(c.on_date)}** · {fmt(c.start)}–{fmt(c.end)} · {c.title}")
            if k2.button("Remove", key=f"rm_commit_{index}"):
                ss.job_commitments.remove(c)
                st.rerun()
        hidden = len(ss.job_commitments) - len(in_period)
        if hidden:
            st.caption(f"{hidden} plan(s) fall outside these dates and are ignored.")

    s1, s2 = st.columns([1, 3])
    if s1.button("💾 Save sitting job", type="primary", disabled=not sitter.strip()):
        try:
            job = SittingJob(
                owner.id, sitter, start, num_days, [pet_ids[p] for p in ss.job_pets],
                [c for c in ss.job_commitments if start <= c.on_date <= end],
                **({"id": ss.job_id} if ss.get("job_id") else {}),
            )
            store.save_job(job)
            ss.job_id = job.id
            s2.success("Saved. Pick it from “Continue a saved sitting job” next time.")
        except ValueError as error:
            s2.error(str(error))
    elif not sitter.strip():
        s2.caption("Enter your name to save this job.")

    routine = store.load_routine(owner.id, [pet_ids[p] for p in ss.job_pets])
    commitments = [c for c in ss.job_commitments if start <= c.on_date <= end]

    schedule_tab, info_tab, ask_tab = st.tabs(["📅 Schedule", "🐾 Pets & emergency info", "💬 Ask about care"])
    with schedule_tab:
        schedule_view(routine, commitments, start, num_days, client)
    with info_tab:
        owner_info_card(routine)
    with ask_tab:
        ask_view(routine, client)
