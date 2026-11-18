"""Tests for the local JSON store."""

import json
from datetime import date

import pytest

from sitter_planner import CareTask, Commitment, PetProfile, SitterPlanner
from storage import (
    OwnerRecord,
    PetRecord,
    SittingJob,
    StorageError,
    Store,
    TaskRecord,
)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db.json")


def test_owner_pet_task_round_trip(store) -> None:
    owner = store.save_owner(OwnerRecord("Sam", phone="555-0100", vet_name="Oak Vet", vet_phone="555-0111",
                                         emergency_procedures="Call the vet first."))
    pet = store.save_pet(PetRecord(owner.id, PetProfile("Rex", "dog", breed="Lab", traits="Loves fetch",
                                                        medications="None", allergies="Beef")))
    store.save_task(TaskRecord(pet.id, CareTask("Rex", "Breakfast", "07:00", "08:00", 10, priority="high",
                                                days=["Mon", "Wed"], flex_minutes=15, description="1 cup")))

    assert [o.name for o in store.list_owners()] == ["Sam"]
    loaded_pet = store.list_pets(owner.id)[0]
    assert loaded_pet.profile.allergies == "Beef" and loaded_pet.profile.traits == "Loves fetch"
    task = store.list_tasks(loaded_pet)[0].task
    assert (task.window_start, task.days, task.flex_minutes) == (420, (0, 2), 15)

    routine = store.load_routine(owner.id)
    assert routine.vet_summary == "Oak Vet · 555-0111"
    assert routine.emergency_procedures == "Call the vet first."
    assert [t.task_id for t in routine.tasks] == ["rex-breakfast"]


def test_editing_and_renaming_pet_keeps_tasks(store) -> None:
    owner = store.save_owner(OwnerRecord("Sam"))
    pet = store.save_pet(PetRecord(owner.id, PetProfile("Rex", "dog")))
    store.save_task(TaskRecord(pet.id, CareTask("Rex", "Walk", "09:00", "10:00", 20)))
    pet.profile = PetProfile("Rexy", "dog", health_conditions="Arthritis")
    store.save_pet(pet)
    pets = store.list_pets(owner.id)
    assert len(pets) == 1 and pets[0].name == "Rexy"
    assert store.list_tasks(pets[0])[0].task.pet_name == "Rexy"


def test_duplicates_rejected(store) -> None:
    owner = store.save_owner(OwnerRecord("Sam"))
    pet = store.save_pet(PetRecord(owner.id, PetProfile("Rex", "dog")))
    with pytest.raises(ValueError):
        store.save_pet(PetRecord(owner.id, PetProfile("rex", "cat")))
    store.save_task(TaskRecord(pet.id, CareTask("Rex", "Walk", "09:00", "10:00", 20)))
    with pytest.raises(ValueError):
        store.save_task(TaskRecord(pet.id, CareTask("Rex", "walk", "11:00", "12:00", 20)))


def test_load_routine_for_selected_pets_only(store) -> None:
    owner = store.seed_sample()
    pets = {p.name: p for p in store.list_pets(owner.id)}
    routine = store.load_routine(owner.id, [pets["Luna"].id])
    assert [p.name for p in routine.pets] == ["Luna"]
    assert all(t.pet_name == "Luna" for t in routine.tasks)


def test_seed_is_idempotent_and_plans(store) -> None:
    first = store.seed_sample()
    second = store.seed_sample()
    assert first.id == second.id and len(store.list_owners()) == 1
    routine = store.load_routine(first.id)
    assert len(routine.tasks) == 9
    plan = SitterPlanner().plan_day(routine, date(2026, 11, 23))
    assert not plan.alerts


def test_sitting_job_with_commitments(store) -> None:
    owner = store.seed_sample()
    pet_ids = [p.id for p in store.list_pets(owner.id)]
    job = SittingJob(owner.id, "Alex", date(2026, 11, 23), 7, pet_ids,
                     [Commitment("Lab", date(2026, 11, 23), "11:30", "13:30")])
    store.save_job(job)
    job.commitments.append(Commitment("Dentist", date(2026, 11, 24), "07:30", "09:00"))
    store.save_job(job)  # re-saving replaces commitments, no duplicates

    jobs = store.list_jobs(owner_id=owner.id, sitter_name="alex")
    assert len(jobs) == 1
    assert [c.title for c in jobs[0].commitments] == ["Lab", "Dentist"]
    assert jobs[0].start_date == date(2026, 11, 23) and set(jobs[0].pet_ids) == set(pet_ids)
    assert store.list_jobs(sitter_name="someone else") == []


def test_delete_owner_cascades(store) -> None:
    owner = store.seed_sample()
    store.save_job(SittingJob(owner.id, "Alex", date(2026, 11, 23), 1,
                              [p.id for p in store.list_pets(owner.id)]))
    store.delete_owner(owner.id)
    assert store.list_owners() == []
    assert store._select("pets") == [] and store._select("care_tasks") == []
    assert store._select("sitting_jobs") == [] and store._select("sitter_commitments") == []


def test_validation() -> None:
    with pytest.raises(ValueError):
        OwnerRecord("  ")
    with pytest.raises(ValueError):
        SittingJob("o", "Alex", date(2026, 11, 23), 7, [])
    with pytest.raises(ValueError):
        SittingJob("o", "", date(2026, 11, 23), 7, ["p"])


def test_data_survives_a_new_store_instance(tmp_path) -> None:
    path = tmp_path / "db.json"
    owner = Store(path).seed_sample()
    reopened = Store(path)
    assert [o.id for o in reopened.list_owners()] == [owner.id]
    assert len(reopened.load_routine(owner.id).tasks) == 9
    assert set(json.loads(path.read_text())) == {
        "owners", "pets", "care_tasks", "sitting_jobs", "sitter_commitments"
    }


def test_corrupt_file_gives_clear_error_and_is_not_overwritten(tmp_path) -> None:
    path = tmp_path / "db.json"
    path.write_text("{not json")
    store = Store(path)
    with pytest.raises(StorageError, match="can't be read"):
        store.list_owners()
    with pytest.raises(StorageError):
        store.save_owner(OwnerRecord("Sam"))
    assert path.read_text() == "{not json"  # left alone so nothing is lost


def test_owner_without_phone(store) -> None:
    from rag import CareAssistant
    from views.common import owner_info_card  # noqa: F401  (imports cleanly)

    owner = store.save_owner(OwnerRecord("Sam"))
    pet = store.save_pet(PetRecord(owner.id, PetProfile("Rex", "dog")))
    store.save_task(TaskRecord(pet.id, CareTask("Rex", "Walk", "09:00", "10:00", 20)))
    routine = store.load_routine(owner.id)
    assert routine.owner_contact == ""
    answer = CareAssistant(routine, guides=[]).answer("When is Rex's walk?")
    assert "Owner contact" not in answer.text


def test_task_dependencies_saved_by_id(store) -> None:
    owner = store.save_owner(OwnerRecord("Sam"))
    rex = store.save_pet(PetRecord(owner.id, PetProfile("Rex", "dog")))
    tom = store.save_pet(PetRecord(owner.id, PetProfile("Tom", "cat")))
    breakfast = store.save_task(TaskRecord(rex.id, CareTask("Rex", "Breakfast", "07:00", "08:00", 10)))
    dinner = store.save_task(TaskRecord(rex.id, CareTask("Rex", "Dinner", "17:00", "19:00", 10),
                                        after_ref=breakfast.id, after_minutes=600))
    store.save_task(TaskRecord(tom.id, CareTask("Tom", "Meds", "07:00", "09:00", 5),
                               after_ref=breakfast.id, after_minutes=30))

    # Renaming the anchor keeps the link (it's stored by id, not title).
    breakfast.task = CareTask("Rex", "Morning meal", "07:00", "08:00", 10)
    store.save_task(breakfast)
    routine = store.load_routine(owner.id)
    assert routine.task("rex-dinner").after_task_id == "rex-morning-meal"
    assert routine.task("tom-meds").after_minutes == 30

    # A sitter looking after only Tom: the link to Rex's task is dropped, not broken.
    only_tom = store.load_routine(owner.id, [tom.id])
    assert only_tom.task("tom-meds").after_task_id == ""

    # Loops are rejected.
    breakfast.after_ref, breakfast.after_minutes = dinner.id, 60
    with pytest.raises(ValueError, match="loop"):
        store.save_task(breakfast)

    # Deleting the anchor clears the dependents.
    store.delete_task(breakfast.id)
    routine = store.load_routine(owner.id)
    assert routine.task("rex-dinner").after_task_id == "" and routine.task("tom-meds").after_task_id == ""


def test_seeded_sample_keeps_dependencies(store) -> None:
    owner = store.seed_sample()
    routine = store.load_routine(owner.id)
    assert routine.task("mochi-dinner").after_task_id == "mochi-breakfast"
    assert routine.task("luna-breakfast").after_task_id == "luna-thyroid-medication"
