"""Owner screen: profile, pets (traits, requirements, health, routine), vet & emergency info."""

from __future__ import annotations

from datetime import time
from typing import Optional

import streamlit as st

from sitter_planner import WEEKDAYS, CareTask, PetProfile, fmt, gap_label, to_minutes
from storage import OwnerRecord, PetRecord, Store, TaskRecord
from views.common import FIVE_MIN, SPECIES

NEW = "➕ Create a new owner profile"


def _parse_time(value: str) -> time:
    minutes = to_minutes(value)
    return time(min(minutes // 60, 23), minutes % 60 if minutes < 1440 else 59)


# --------------------------------------------------------------------------- #
# Picking / creating the owner profile
# --------------------------------------------------------------------------- #
def _pick_owner(store: Store) -> Optional[OwnerRecord]:
    ss = st.session_state
    owners = store.list_owners()
    labels = [NEW] + [o.name for o in owners]
    current = next((o.name for o in owners if o.id == ss.get("owner_id")), None)

    c1, c2 = st.columns([3, 1])
    choice = c1.selectbox(
        "Which owner are you?",
        labels,
        index=labels.index(current) if current else (1 if owners else 0),
    )
    if not any(o.name == "Jordan" for o in owners):
        c2.write("")
        if c2.button("Add sample owner", help="Jordan, with dog Mochi and cat Luna"):
            ss.owner_id = store.seed_sample().id
            st.rerun()

    if choice != NEW:
        owner = next(o for o in owners if o.name == choice)
        if ss.get("owner_id") != owner.id:
            ss.owner_id = owner.id
            ss.pop("pet_id", None)
        return owner

    with st.form("new_owner"):
        st.markdown("#### Create your profile")
        a, b = st.columns(2)
        name = a.text_input("Your name")
        phone = b.text_input("Phone (optional)")
        if st.form_submit_button("Create profile", type="primary"):
            try:
                owner = store.save_owner(OwnerRecord(name, phone=phone))
            except ValueError as error:
                st.error(str(error))
            else:
                ss.owner_id = owner.id
                st.rerun()
    return None


# --------------------------------------------------------------------------- #
# Pets
# --------------------------------------------------------------------------- #
def _add_pet_form(store: Store, owner: OwnerRecord) -> None:
    with st.form("add_pet", clear_on_submit=True):
        a, b, c, d = st.columns([2, 1, 2, 1])
        name = a.text_input("Name")
        species = b.selectbox("Species", SPECIES)
        breed = c.text_input("Breed")
        age = d.text_input("Age", placeholder="4 years")
        if st.form_submit_button("Add pet"):
            try:
                pet = store.save_pet(PetRecord(owner.id, PetProfile(name, species, breed=breed, age=age)))
            except ValueError as error:
                st.error(str(error))
            else:
                st.session_state.pet_id = pet.id
                st.rerun()


def _profile_form(store: Store, pet: PetRecord) -> None:
    p = pet.profile
    with st.form(f"profile_{pet.id}"):
        a, b, c, d = st.columns([2, 1, 2, 1])
        name = a.text_input("Name", p.name, key=f"pet_name_{pet.id}")
        species = b.selectbox("Species", SPECIES, index=SPECIES.index(p.species) if p.species in SPECIES else len(SPECIES) - 1, key=f"pet_species_{pet.id}")
        breed = c.text_input("Breed", p.breed, key=f"pet_breed_{pet.id}")
        age = d.text_input("Age", p.age, key=f"pet_age_{pet.id}")
        traits = st.text_area(
            "Personality & habits", p.traits, max_chars=1000, key=f"pet_traits_{pet.id}",
            placeholder="Shy with strangers, pulls on the leash near squirrels, scared of thunder…",
        )
        requirements = st.text_area(
            "Requirements & house rules", p.requirements, max_chars=1000, key=f"pet_req_{pet.id}",
            placeholder="Indoor only, not allowed on the couch, needs an hour of walking a day…",
        )
        notes = st.text_area("Anything else", p.notes, max_chars=1000, key=f"pet_notes_{pet.id}")
        if st.form_submit_button("Save profile", type="primary"):
            try:
                pet.profile = PetProfile(
                    name, species, breed=breed, age=age, traits=traits, requirements=requirements,
                    notes=notes, health_conditions=p.health_conditions, medications=p.medications,
                    allergies=p.allergies,
                )
                store.save_pet(pet)
            except ValueError as error:
                st.error(str(error))
            else:
                st.success("Saved.")
                st.rerun()


def _health_form(store: Store, pet: PetRecord) -> None:
    p = pet.profile
    with st.form(f"health_{pet.id}"):
        conditions = st.text_area(
            "Health conditions", p.health_conditions, max_chars=1000, key=f"pet_health_{pet.id}",
            placeholder="Hyperthyroidism, arthritis, recent surgery…",
        )
        medications = st.text_area(
            "Medications", p.medications, max_chars=1000, key=f"pet_meds_{pet.id}",
            placeholder="Name, dose, and when. Add each dose to the daily routine too so it's scheduled.",
        )
        allergies = st.text_area("Allergies & foods to avoid", p.allergies, max_chars=1000, key=f"pet_allergies_{pet.id}")
        if st.form_submit_button("Save health info", type="primary"):
            try:
                profile = p.to_dict()
                profile.update(health_conditions=conditions, medications=medications, allergies=allergies)
                pet.profile = PetProfile(**profile)
                store.save_pet(pet)
            except ValueError as error:
                st.error(str(error))
            else:
                st.success("Saved.")
                st.rerun()


NO_DEPENDENCY = "— doesn't depend on another task —"


def _routine_editor(store: Store, pet: PetRecord) -> None:
    records = store.list_tasks(pet)
    all_tasks = store.owner_tasks(pet.owner_id)
    pet_names = {p.id: p.name for p in store.list_pets(pet.owner_id)}
    task_labels = {r.id: f"{pet_names.get(r.pet_id, '?')}: {r.task.title}" for r in all_tasks}

    def after_text(r: TaskRecord) -> str:
        if not r.after_ref or r.after_ref not in task_labels:
            return ""
        return f"≥ {gap_label(r.after_minutes)} after {task_labels[r.after_ref]}"

    pick_key = f"task_pick_{pet.id}"

    def choose(title: str) -> None:
        # Runs before the next rerun, so it can set the dropdown below.
        st.session_state[pick_key] = title

    if records:
        widths = [2.2, 1.3, 0.8, 0.8, 0.9, 1.4, 2.4]
        header = st.columns(widths)
        for col, name in zip(header, ["Task", "Window", "Minutes", "Flex", "Priority", "Days", "Depends on"]):
            col.caption(name)
        selected = st.session_state.get(pick_key)
        for r in records:
            t = r.task
            cols = st.columns(widths, vertical_alignment="center")
            cols[0].button(
                f"✏️ **{t.title}**" if t.title == selected else f"✏️ {t.title}",
                key=f"edit_task_{r.id}",
                type="tertiary",
                help=t.description or "Click to edit",
                on_click=choose,
                args=(t.title,),
            )
            cols[1].write(t.window_label)
            cols[2].write(str(t.duration_minutes))
            cols[3].write(f"±{t.flex_minutes}" if t.flex_minutes else "none")
            cols[4].write(t.priority)
            cols[5].write(", ".join(WEEKDAYS[d] for d in t.days) if len(t.days) < 7 else "every day")
            cols[6].write(after_text(r) or "—")
        st.caption("Click a task name to edit it.")
    else:
        st.caption(f"No routine yet for {pet.name}. Add feedings, walks, medication, grooming…")

    new_label = "➕ New task"
    options = [new_label] + [r.task.title for r in records]
    if st.session_state.get(pick_key) not in options:
        st.session_state.pop(pick_key, None)  # e.g. the task was renamed or deleted
    picked = st.selectbox("Add or edit a task", options, key=pick_key)
    existing = next((r for r in records if r.task.title == picked), None)
    t = existing.task if existing else None

    with st.form(f"task_form_{pet.id}_{existing.id if existing else 'new'}", clear_on_submit=existing is None):
        a, b = st.columns([3, 1])
        title = a.text_input("Task", t.title if t else "", placeholder="Breakfast, medication, walk…")
        priority = b.selectbox("Priority", ["high", "medium", "low"], index=["high", "medium", "low"].index(t.priority) if t else 1)
        w1, w2, w3, w4 = st.columns(4)
        start = w1.time_input("Window opens", _parse_time(fmt(t.window_start)) if t else time(8, 0), step=FIVE_MIN)
        end = w2.time_input("Must be done by", _parse_time(fmt(t.window_end)) if t else time(9, 0), step=FIVE_MIN)
        duration = w3.number_input("Takes (minutes)", 1, 600, t.duration_minutes if t else 10)
        flex = w4.number_input(
            "Flex (minutes)", 0, 240, t.flex_minutes if t else 0,
            help="How far outside the window is still OK if the sitter is stuck. Use 0 for medication.",
        )
        days = st.multiselect("Repeats on", WEEKDAYS, [WEEKDAYS[d] for d in t.days] if t else WEEKDAYS)

        st.markdown("**Timing after another task** (optional)")
        anchor_options = {NO_DEPENDENCY: ""}
        anchor_options.update(
            {label: rid for rid, label in task_labels.items() if not existing or rid != existing.id}
        )
        anchor_labels = list(anchor_options)
        current_anchor = task_labels.get(existing.after_ref, NO_DEPENDENCY) if existing else NO_DEPENDENCY
        gap_hours, gap_mins = divmod(existing.after_minutes, 60) if existing else (0, 0)
        g1, g2, g3 = st.columns([3, 1, 1])
        anchor_label = g1.selectbox(
            "Must start after", anchor_labels,
            index=anchor_labels.index(current_anchor) if current_anchor in anchor_labels else 0,
            help="e.g. dinner at least 10 hours after breakfast. The gap is counted from when "
            "that task starts. This task's own window still applies.",
        )
        hours = g2.number_input("At least (hours)", 0, 23, gap_hours)
        minutes = g3.number_input("+ minutes", 0, 55, gap_mins, step=5)

        description = st.text_area(
            "Instructions", t.description if t else "", max_chars=1000,
            placeholder="Exact amounts, where supplies are, how your pet likes it done…",
        )
        save = st.form_submit_button("Save task" if existing else "Add task", type="primary")
        delete = st.form_submit_button("Delete task") if existing else False

    if save:
        try:
            task = CareTask(
                pet.name, title, to_minutes(start), to_minutes(end), int(duration),
                description=description, priority=priority, days=days, flex_minutes=int(flex),
            )
            after_ref = anchor_options[anchor_label]
            gap = int(hours) * 60 + int(minutes)
            if after_ref and gap == 0:
                raise ValueError("set how long after the other task this should start")
            record = TaskRecord(
                pet.id, task, after_ref=after_ref, after_minutes=gap,
                **({"id": existing.id} if existing else {}),
            )
            store.save_task(record)
        except ValueError as error:
            st.error(str(error))
        else:
            st.session_state.pop(f"task_pick_{pet.id}", None)
            st.rerun()
    if delete:
        dependents = [task_labels[r.id] for r in all_tasks if r.after_ref == existing.id]
        if dependents:
            st.toast(f"Removed the timing link from: {', '.join(dependents)}")
        store.delete_task(existing.id)
        st.session_state.pop(f"task_pick_{pet.id}", None)
        st.rerun()


def _pets_tab(store: Store, owner: OwnerRecord) -> None:
    ss = st.session_state
    pets = store.list_pets(owner.id)
    with st.expander("➕ Add a pet", expanded=not pets):
        _add_pet_form(store, owner)
    if not pets:
        return

    labels = {f"🐾 {p.name} ({p.profile.species})": p for p in pets}
    if ss.get("pet_id") not in {p.id for p in pets}:
        ss.pet_id = pets[0].id
    current = next(label for label, p in labels.items() if p.id == ss.pet_id)
    choice = st.radio(
        "Pet", list(labels), index=list(labels).index(current), horizontal=True,
        label_visibility="collapsed",
    )
    pet = labels[choice]
    ss.pet_id = pet.id

    profile_tab, health_tab, routine_tab = st.tabs(["Profile & traits", "Health", "Daily routine"])
    with profile_tab:
        _profile_form(store, pet)
        with st.popover(f"Remove {pet.name}"):
            st.write(f"This deletes {pet.name} and their whole routine.")
            if st.button(f"Yes, remove {pet.name}", type="primary", key=f"del_{pet.id}"):
                store.delete_pet(pet.id)
                ss.pop("pet_id", None)
                st.rerun()
    with health_tab:
        _health_form(store, pet)
    with routine_tab:
        _routine_editor(store, pet)


# --------------------------------------------------------------------------- #
# Owner-level info
# --------------------------------------------------------------------------- #
def _contacts_tab(store: Store, owner: OwnerRecord) -> None:
    with st.form("contacts"):
        st.markdown("#### You")
        a, b = st.columns(2)
        name = a.text_input("Name", owner.name, key=f"owner_name_{owner.id}")
        phone = b.text_input("Phone (optional)", owner.phone, key=f"owner_phone_{owner.id}")
        st.markdown("#### Vet")
        d, e = st.columns(2)
        vet_name = d.text_input("Clinic / vet name", owner.vet_name, key=f"owner_vet_name_{owner.id}")
        vet_phone = e.text_input("Vet phone", owner.vet_phone, key=f"owner_vet_phone_{owner.id}")
        vet_address = st.text_input("Vet address & hours", owner.vet_address, key=f"owner_vet_addr_{owner.id}")
        st.markdown("#### Backup")
        emergency_contact = st.text_input(
            "Emergency contacts", owner.emergency_contact, key=f"owner_em_contact_{owner.id}",
            placeholder="24-hour animal ER, a friend or family member who can help…",
        )
        if st.form_submit_button("Save contacts", type="primary"):
            _save_owner(store, owner, name=name, phone=phone, vet_name=vet_name,
                        vet_phone=vet_phone, vet_address=vet_address, emergency_contact=emergency_contact)


def _emergency_tab(store: Store, owner: OwnerRecord) -> None:
    with st.form("emergency"):
        procedures = st.text_area(
            "Emergency procedures", owner.emergency_procedures, height=180, max_chars=2000,
            key=f"owner_procedures_{owner.id}",
            placeholder="Who to call first, where the carrier and records are, payment arrangements with the vet…",
        )
        house = st.text_area(
            "House notes", owner.house_notes, max_chars=1000, key=f"owner_house_{owner.id}",
            placeholder="Spare key, alarm code location, gates to keep closed…",
        )
        if st.form_submit_button("Save emergency plan", type="primary"):
            _save_owner(store, owner, emergency_procedures=procedures, house_notes=house)

    with st.popover("Delete my owner profile"):
        st.write("This deletes your profile, all pets, routines, and sitting jobs.")
        if st.button("Yes, delete everything", type="primary"):
            store.delete_owner(owner.id)
            st.session_state.pop("owner_id", None)
            st.rerun()


def _save_owner(store: Store, owner: OwnerRecord, **changes: str) -> None:
    try:
        data = owner.to_row()
        data.update(changes)
        store.save_owner(OwnerRecord(**data))
    except ValueError as error:
        st.error(str(error))
    else:
        st.success("Saved.")
        st.rerun()


def render(store: Store) -> None:
    st.title("🏠 Owner")
    st.caption("Set up your pets and everything a sitter needs to know.")
    owner = _pick_owner(store)
    if owner is None:
        return

    missing = [label for label, value in (
        ("vet contact", owner.vet_phone or owner.vet_name),
        ("emergency procedures", owner.emergency_procedures),
    ) if not value]
    if missing:
        st.warning(f"Sitters will see your info. Still missing: {', '.join(missing)}.")

    pets_tab, contacts_tab, emergency_tab = st.tabs(["🐾 My pets", "📇 Contacts & vet", "🚨 Emergency plan"])
    with pets_tab:
        _pets_tab(store, owner)
    with contacts_tab:
        _contacts_tab(store, owner)
    with emergency_tab:
        _emergency_tab(store, owner)
