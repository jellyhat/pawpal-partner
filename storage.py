"""Local JSON persistence for owners, pets, routines, and sitting jobs.

Everything is saved to one JSON file on this machine (default
``data/pawpal_db.json``; override with the ``PAWPAL_DB_PATH`` environment
variable). The file is organised like database tables:

    owners, pets, care_tasks, sitting_jobs, sitter_commitments

Each row has a UUID ``id`` and rows refer to each other by id (a pet's
``owner_id``, a task's ``pet_id``...). Writes go to a temporary file first
and then replace the real file, so a crash mid-save can't leave it half
written. A lock serialises writes from the Streamlit server's threads.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from sitter_planner import (
    WEEKDAYS,
    CareTask,
    Commitment,
    PetProfile,
    Routine,
    fmt,
)

logger = logging.getLogger("pawpal.storage")

DEFAULT_PATH = Path(__file__).parent / "data" / "pawpal_db.json"
SAMPLE_ROUTINE = Path(__file__).parent / "data" / "sample_routine.json"
TABLES = ("owners", "pets", "care_tasks", "sitting_jobs", "sitter_commitments")


class StorageError(RuntimeError):
    """Raised when the data file can't be read or written."""


def new_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class OwnerRecord:
    name: str
    phone: str = ""
    vet_name: str = ""
    vet_phone: str = ""
    vet_address: str = ""
    emergency_contact: str = ""
    emergency_procedures: str = ""
    house_notes: str = ""
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValueError("owner name cannot be empty")

    @property
    def contact(self) -> str:
        return self.phone

    def to_row(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) or "" for f in fields(self)}

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "OwnerRecord":
        names = {f.name for f in fields(cls)}
        return cls(**{k: (v or "") for k, v in row.items() if k in names})


@dataclass
class PetRecord:
    owner_id: str
    profile: PetProfile
    id: str = field(default_factory=new_id)

    @property
    def name(self) -> str:
        return self.profile.name

    def to_row(self) -> Dict[str, Any]:
        row = self.profile.to_dict()
        row.pop("vet_contact", None)  # vet info lives on the owner
        row.update({"id": self.id, "owner_id": self.owner_id})
        return row

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "PetRecord":
        profile_fields = {f.name for f in fields(PetProfile)}
        profile = PetProfile(
            **{k: (v or "") for k, v in row.items() if k in profile_fields}
        )
        return cls(owner_id=row["owner_id"], profile=profile, id=row["id"])


@dataclass
class TaskRecord:
    """A routine task as saved. ``after_ref`` is the saved id of the task it depends on."""

    pet_id: str
    task: CareTask
    id: str = field(default_factory=new_id)
    after_ref: str = ""
    after_minutes: int = 0

    def __post_init__(self) -> None:
        if self.after_ref:
            if self.after_ref == self.id:
                raise ValueError("a task can't depend on itself")
            if not 0 < int(self.after_minutes) < 24 * 60:
                raise ValueError("the gap after the other task must be between 1 minute and 24 hours")
        else:
            self.after_minutes = 0

    def to_row(self) -> Dict[str, Any]:
        t = self.task
        return {
            "id": self.id,
            "pet_id": self.pet_id,
            "title": t.title,
            "window_start": fmt(t.window_start),
            "window_end": fmt(t.window_end),
            "duration_minutes": t.duration_minutes,
            "priority": t.priority,
            "days": [WEEKDAYS[d] for d in t.days],
            "flex_minutes": t.flex_minutes,
            "description": t.description,
            "after_task_ref": self.after_ref,
            "after_minutes": int(self.after_minutes),
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any], pet_name: str) -> "TaskRecord":
        task = CareTask(
            pet_name=pet_name,
            title=row["title"],
            window_start=row["window_start"][:5],
            window_end=row["window_end"][:5],
            duration_minutes=int(row["duration_minutes"]),
            description=row.get("description") or "",
            priority=row.get("priority") or "medium",
            days=row.get("days") or "daily",
            flex_minutes=int(row.get("flex_minutes") or 0),
        )
        return cls(
            pet_id=row["pet_id"],
            task=task,
            id=row["id"],
            after_ref=row.get("after_task_ref") or "",
            after_minutes=int(row.get("after_minutes") or 0),
        )


@dataclass
class SittingJob:
    owner_id: str
    sitter_name: str
    start_date: date
    num_days: int
    pet_ids: List[str]
    commitments: List[Commitment] = field(default_factory=list)
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        self.sitter_name = (self.sitter_name or "").strip()
        if not self.sitter_name:
            raise ValueError("sitter name cannot be empty")
        if isinstance(self.start_date, str):
            self.start_date = date.fromisoformat(self.start_date)
        if not 1 <= int(self.num_days) <= 31:
            raise ValueError("a sitting job lasts 1 to 31 days")
        if not self.pet_ids:
            raise ValueError("choose at least one pet")

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "sitter_name": self.sitter_name,
            "start_date": self.start_date.isoformat(),
            "num_days": int(self.num_days),
            "pet_ids": list(self.pet_ids),
        }


# --------------------------------------------------------------------------- #
# Shared store logic
# --------------------------------------------------------------------------- #
class Store:
    """Owner/pet/routine/job operations, saved to a local JSON file."""

    _lock = threading.Lock()

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = Path(path)

    # -- file access ---------------------------------------------------------- #
    def _read(self) -> Dict[str, List[Dict[str, Any]]]:
        if not self.path.exists():
            return {t: [] for t in TABLES}
        try:
            data = json.loads(self.path.read_text() or "{}")
        except (OSError, json.JSONDecodeError) as error:
            logger.error("Can't read %s: %s", self.path, error)
            raise StorageError(
                f"The data file {self.path} can't be read ({error}). Fix or move it "
                "aside; PawPal+ will start a fresh one."
            ) from error
        if not isinstance(data, dict):
            raise StorageError(f"The data file {self.path} isn't in the expected format.")
        for t in TABLES:
            data.setdefault(t, [])
        return data

    def _write(self, data: Dict[str, List[Dict[str, Any]]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.path)
        except OSError as error:
            logger.error("Can't write %s: %s", self.path, error)
            raise StorageError(f"Couldn't save to {self.path}: {error}") from error

    @staticmethod
    def _match(row: Dict[str, Any], eq: Dict[str, Any]) -> bool:
        return all(row.get(k) == v for k, v in eq.items())

    def _select(self, table: str, **eq: Any) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._read()[table] if self._match(r, eq)]

    def _upsert(self, table: str, row: Dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            data[table] = [r for r in data[table] if r.get("id") != row["id"]] + [dict(row)]
            self._write(data)

    def _delete(self, table: str, **eq: Any) -> None:
        with self._lock:
            data = self._read()
            data[table] = [r for r in data[table] if not self._match(r, eq)]
            self._write(data)

    # -- owners --------------------------------------------------------------- #
    def list_owners(self) -> List[OwnerRecord]:
        owners = [OwnerRecord.from_row(r) for r in self._select("owners")]
        return sorted(owners, key=lambda o: o.name.casefold())

    def get_owner(self, owner_id: str) -> Optional[OwnerRecord]:
        rows = self._select("owners", id=owner_id)
        return OwnerRecord.from_row(rows[0]) if rows else None

    def save_owner(self, owner: OwnerRecord) -> OwnerRecord:
        self._upsert("owners", owner.to_row())
        logger.info("Saved owner %s (%s)", owner.name, owner.id)
        return owner

    def delete_owner(self, owner_id: str) -> None:
        for pet in self.list_pets(owner_id):
            self.delete_pet(pet.id)
        for job in self._select("sitting_jobs", owner_id=owner_id):
            self.delete_job(job["id"])
        self._delete("owners", id=owner_id)
        logger.info("Deleted owner %s", owner_id)

    # -- pets ----------------------------------------------------------------- #
    def list_pets(self, owner_id: str) -> List[PetRecord]:
        pets = [PetRecord.from_row(r) for r in self._select("pets", owner_id=owner_id)]
        return sorted(pets, key=lambda p: p.name.casefold())

    def get_pet(self, pet_id: str) -> Optional[PetRecord]:
        rows = self._select("pets", id=pet_id)
        return PetRecord.from_row(rows[0]) if rows else None

    def save_pet(self, pet: PetRecord) -> PetRecord:
        for other in self.list_pets(pet.owner_id):
            if other.id != pet.id and other.name.casefold() == pet.name.casefold():
                raise ValueError(f"you already have a pet named {pet.name}")
        # Tasks reference pets by id, so renaming a pet needs no other updates.
        self._upsert("pets", pet.to_row())
        logger.info("Saved pet %s (%s)", pet.name, pet.id)
        return pet

    def delete_pet(self, pet_id: str) -> None:
        for row in self._select("care_tasks", pet_id=pet_id):
            self._clear_dependents(row["id"])
        self._delete("care_tasks", pet_id=pet_id)
        self._delete("pets", id=pet_id)
        logger.info("Deleted pet %s", pet_id)

    # -- routine tasks -------------------------------------------------------- #
    def list_tasks(self, pet: PetRecord) -> List[TaskRecord]:
        tasks = [TaskRecord.from_row(r, pet.name) for r in self._select("care_tasks", pet_id=pet.id)]
        return sorted(tasks, key=lambda t: (t.task.window_start, t.task.title))

    def save_task(self, record: TaskRecord) -> TaskRecord:
        pet = self.get_pet(record.pet_id)
        if pet is None:
            raise ValueError("that pet no longer exists")
        for other in self.list_tasks(pet):
            if other.id != record.id and other.task.title.casefold() == record.task.title.casefold():
                raise ValueError(f"{pet.name} already has a task called '{record.task.title}'")
        if record.after_ref:
            self._check_dependency(pet.owner_id, record)
        self._upsert("care_tasks", record.to_row())
        logger.info("Saved task %s for %s", record.task.title, pet.name)
        return record

    def owner_tasks(self, owner_id: str) -> List[TaskRecord]:
        """Every routine task across all of an owner's pets."""
        return [r for pet in self.list_pets(owner_id) for r in self.list_tasks(pet)]

    def _check_dependency(self, owner_id: str, record: TaskRecord) -> None:
        by_id = {r.id: r for r in self.owner_tasks(owner_id)}
        if record.after_ref not in by_id:
            raise ValueError("the task this depends on no longer exists")
        by_id[record.id] = record
        seen = {record.id}
        current = record
        while current.after_ref:
            current = by_id.get(current.after_ref)
            if current is None:
                break
            if current.id in seen:
                raise ValueError(
                    f"that would make a loop: {current.task.title} already depends on {record.task.title}"
                )
            seen.add(current.id)

    def _clear_dependents(self, task_id: str) -> None:
        for row in self._select("care_tasks", after_task_ref=task_id):
            row.update(after_task_ref="", after_minutes=0)
            self._upsert("care_tasks", row)
            logger.info("Cleared dependency of task %s on deleted task %s", row["id"], task_id)

    def delete_task(self, task_id: str) -> None:
        self._clear_dependents(task_id)
        self._delete("care_tasks", id=task_id)

    # -- routine for the planner --------------------------------------------- #
    def load_routine(self, owner_id: str, pet_ids: Optional[Sequence[str]] = None) -> Routine:
        owner = self.get_owner(owner_id)
        if owner is None:
            raise ValueError("owner not found")
        routine = Routine(
            owner_name=owner.name,
            owner_contact=owner.contact,
            vet_name=owner.vet_name,
            vet_phone=owner.vet_phone,
            vet_address=owner.vet_address,
            emergency_contact=owner.emergency_contact,
            emergency_procedures=owner.emergency_procedures,
            house_notes=owner.house_notes,
        )
        wanted = set(pet_ids) if pet_ids is not None else None
        records: List[TaskRecord] = []
        for pet in self.list_pets(owner_id):
            if wanted is not None and pet.id not in wanted:
                continue
            routine.add_pet(pet.profile)
            for record in self.list_tasks(pet):
                routine.add_task(record.task)
                records.append(record)
        # Dependencies are saved by record id; the planner uses task_id. A task
        # whose anchor belongs to a pet that isn't loaded just has no dependency.
        planner_ids = {r.id: r.task.task_id for r in records}
        for record in records:
            if record.after_ref in planner_ids:
                record.task.after_task_id = planner_ids[record.after_ref]
                record.task.after_minutes = record.after_minutes
        routine.check_dependencies()
        return routine

    # -- sitting jobs --------------------------------------------------------- #
    def list_jobs(self, owner_id: Optional[str] = None, sitter_name: Optional[str] = None) -> List[SittingJob]:
        filters = {"owner_id": owner_id} if owner_id else {}
        jobs = []
        for row in self._select("sitting_jobs", **filters):
            if sitter_name and row["sitter_name"].casefold() != sitter_name.strip().casefold():
                continue
            jobs.append(self._job_from_row(row))
        return sorted(jobs, key=lambda j: j.start_date, reverse=True)

    def _job_from_row(self, row: Dict[str, Any]) -> SittingJob:
        commitments = [
            Commitment(c["title"], c["on_date"][:10], c["start_time"][:5], c["end_time"][:5])
            for c in self._select("sitter_commitments", job_id=row["id"])
        ]
        return SittingJob(
            owner_id=row["owner_id"],
            sitter_name=row["sitter_name"],
            start_date=row["start_date"][:10],
            num_days=int(row["num_days"]),
            pet_ids=list(row.get("pet_ids") or []),
            commitments=sorted(commitments, key=lambda c: (c.on_date, c.start)),
            id=row["id"],
        )

    def save_job(self, job: SittingJob) -> SittingJob:
        self._upsert("sitting_jobs", job.to_row())
        self._delete("sitter_commitments", job_id=job.id)
        for c in job.commitments:
            self._upsert(
                "sitter_commitments",
                {
                    "id": new_id(),
                    "job_id": job.id,
                    "title": c.title,
                    "on_date": c.on_date.isoformat(),
                    "start_time": fmt(c.start),
                    "end_time": fmt(c.end),
                },
            )
        logger.info("Saved sitting job %s (%s, %d commitments)", job.id, job.sitter_name, len(job.commitments))
        return job

    def delete_job(self, job_id: str) -> None:
        self._delete("sitter_commitments", job_id=job_id)
        self._delete("sitting_jobs", id=job_id)

    # -- sample data ---------------------------------------------------------- #
    def seed_sample(self) -> OwnerRecord:
        """Create the sample owner (Jordan, Mochi, Luna) unless it already exists."""
        data = json.loads(SAMPLE_ROUTINE.read_text())
        existing = next((o for o in self.list_owners() if o.name == data["owner_name"]), None)
        if existing:
            return existing
        phone = data.get("owner_contact", "")
        owner = self.save_owner(
            OwnerRecord(
                name=data["owner_name"],
                phone=phone,
                **{k: data.get(k, "") for k in (
                    "vet_name", "vet_phone", "vet_address", "emergency_contact",
                    "emergency_procedures", "house_notes",
                )},
            )
        )
        pets = {}
        for pet in data["pets"]:
            pet = dict(pet, vet_contact="")
            record = self.save_pet(PetRecord(owner_id=owner.id, profile=PetProfile(**pet)))
            pets[record.name] = record
        saved: Dict[str, TaskRecord] = {}
        for task in data["tasks"]:
            care = CareTask.from_dict(task)
            anchor = saved.get(care.after_task_id)
            record = TaskRecord(
                pet_id=pets[care.pet_name].id,
                task=care,
                after_ref=anchor.id if anchor else "",
                after_minutes=care.after_minutes if anchor else 0,
            )
            saved[care.task_id] = self.save_task(record)
        return owner


def get_store() -> Store:
    """The app's store: ``PAWPAL_DB_PATH`` if set, else data/pawpal_db.json."""
    path = Path(os.environ.get("PAWPAL_DB_PATH", DEFAULT_PATH))
    logger.info("Using local data file %s", path)
    return Store(path)
