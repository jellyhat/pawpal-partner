"""Pet-sitting planner: merges an owner's pet routine with a sitter's own commitments.

The owner posts a routine of care tasks. Each task has a time window it must
happen in (e.g. breakfast between 07:00 and 08:00), a duration, a priority,
written instructions, and the weekdays it repeats on. The sitter adds the
things they need to do on specific dates (class, dentist, work shift).

``SitterPlanner`` builds a day-by-day plan for the sitting period. Every care
task is placed at the earliest free time inside its window. When a sitter
commitment (or another task) leaves no room inside the window, the planner
raises a ``ConflictAlert`` instead of silently dropping or moving the task.

This module is deterministic and has no AI dependency, so the agent and the
evaluation harness can rely on it as the source of truth.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger("pawpal.planner")

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ALL_DAYS: Tuple[int, ...] = tuple(range(7))
PRIORITIES = ("high", "medium", "low")
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
MINUTES_PER_DAY = 24 * 60
MAX_FLEX_MINUTES = 240
MAX_DESCRIPTION_CHARS = 1000

TimeLike = Union[str, time, int]


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def to_minutes(value: TimeLike) -> int:
    """Convert ``"HH:MM"``, a ``datetime.time``, or minutes into minutes after midnight.

    ``"24:00"`` is accepted as the end of the day.
    """
    if isinstance(value, bool):
        raise TypeError("time value cannot be a bool")
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, time):
        minutes = value.hour * 60 + value.minute
    elif isinstance(value, str):
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
        if not match:
            raise ValueError(f"time '{value}' must use HH:MM format")
        hours, mins = int(match.group(1)), int(match.group(2))
        if mins > 59 or hours > 24 or (hours == 24 and mins != 0):
            raise ValueError(f"time '{value}' is not a valid clock time")
        minutes = hours * 60 + mins
    else:
        raise TypeError("time must be 'HH:MM', datetime.time, or minutes")
    if not 0 <= minutes <= MINUTES_PER_DAY:
        raise ValueError("time must fall between 00:00 and 24:00")
    return minutes


def fmt(minutes: int) -> str:
    """Format minutes after midnight as ``HH:MM``."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def fmt_date(on_date: date) -> str:
    """Format a date like ``Tue Nov 17``."""
    return f"{WEEKDAYS[on_date.weekday()]} {on_date:%b} {on_date.day}"


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def parse_days(value: Union[str, Sequence[Union[int, str]], None]) -> Tuple[int, ...]:
    """Parse weekdays given as ``"daily"``, ``["Mon", "Wed"]``, or ``[0, 2]``."""
    if value is None or value == "daily":
        return ALL_DAYS
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    days = []
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool):
            index = item
        elif isinstance(item, str) and item[:3].title() in WEEKDAYS:
            index = WEEKDAYS.index(item[:3].title())
        else:
            raise ValueError(f"unknown weekday '{item}'")
        if not 0 <= index <= 6:
            raise ValueError("weekday numbers must be 0 (Mon) through 6 (Sun)")
        days.append(index)
    if not days:
        raise ValueError("a task must repeat on at least one day")
    return tuple(sorted(set(days)))


def gap_label(minutes: int) -> str:
    """``600`` -> ``"10 h"``, ``570`` -> ``"9 h 30 min"``, ``45`` -> ``"45 min"``."""
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    return f"{hours} h" if hours else f"{mins} min"


def describe_days(days: Sequence[int]) -> str:
    """Return ``"every day"`` or a list such as ``"Wed, Sun"``."""
    if tuple(days) == ALL_DAYS:
        return "every day"
    return ", ".join(WEEKDAYS[d] for d in days)


# --------------------------------------------------------------------------- #
# Owner-side models
# --------------------------------------------------------------------------- #
PET_TEXT_FIELDS = (
    "breed", "age", "traits", "requirements",
    "health_conditions", "medications", "allergies", "notes",
)


@dataclass
class PetProfile:
    """A pet the sitter will look after, with the details the owner wants to share."""

    name: str
    species: str
    notes: str = ""
    vet_contact: str = ""
    breed: str = ""
    age: str = ""
    traits: str = ""  # personality, habits ("shy with strangers", "pulls on leash")
    requirements: str = ""  # diet, exercise, house rules
    health_conditions: str = ""
    medications: str = ""
    allergies: str = ""

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        self.species = self.species.strip()
        if not self.name:
            raise ValueError("pet name cannot be empty")
        if not self.species:
            raise ValueError("species cannot be empty")
        for name in PET_TEXT_FIELDS:
            value = getattr(self, name) or ""
            setattr(self, name, value)
            if len(value) > MAX_DESCRIPTION_CHARS:
                raise ValueError(
                    f"{name.replace('_', ' ')} must be {MAX_DESCRIPTION_CHARS} characters or fewer"
                )

    def to_dict(self) -> dict:
        data = {"name": self.name, "species": self.species, "vet_contact": self.vet_contact}
        data.update({name: getattr(self, name) for name in PET_TEXT_FIELDS})
        return data


@dataclass
class CareTask:
    """One recurring item in the owner's routine.

    ``window_start``/``window_end`` bound when the task must happen.
    ``flex_minutes`` is how far outside that window the owner says is still
    acceptable if the sitter is stuck (0 means "never move this", which is
    typical for medication).

    ``after_task_id``/``after_minutes`` make this task depend on another one:
    it must start at least ``after_minutes`` after that task *starts* on the
    same day (e.g. dinner at least 10 h after breakfast). The task's own
    window still applies on top of the gap.
    """

    pet_name: str
    title: str
    window_start: int
    window_end: int
    duration_minutes: int
    description: str = ""
    priority: str = "medium"
    days: Tuple[int, ...] = ALL_DAYS
    flex_minutes: int = 0
    task_id: str = ""
    after_task_id: str = ""
    after_minutes: int = 0

    def __post_init__(self) -> None:
        self.pet_name = self.pet_name.strip()
        self.title = self.title.strip()
        self.window_start = to_minutes(self.window_start)
        self.window_end = to_minutes(self.window_end)
        self.days = parse_days(self.days)
        if not self.pet_name:
            raise ValueError("task must name a pet")
        if not self.title:
            raise ValueError("task title cannot be empty")
        if self.window_end <= self.window_start:
            raise ValueError(
                f"'{self.title}': window end must be after window start"
            )
        if self.duration_minutes <= 0:
            raise ValueError(f"'{self.title}': duration must be greater than zero")
        if self.duration_minutes > self.window_end - self.window_start:
            raise ValueError(
                f"'{self.title}': {self.duration_minutes} min does not fit in the "
                f"{fmt(self.window_start)}–{fmt(self.window_end)} window"
            )
        if self.priority not in PRIORITIES:
            raise ValueError("priority must be 'high', 'medium', or 'low'")
        if not 0 <= self.flex_minutes <= MAX_FLEX_MINUTES:
            raise ValueError(
                f"flex minutes must be between 0 and {MAX_FLEX_MINUTES}"
            )
        if len(self.description) > MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"description must be {MAX_DESCRIPTION_CHARS} characters or fewer"
            )
        if not self.task_id:
            self.task_id = f"{_slug(self.pet_name)}-{_slug(self.title)}"
        self.after_task_id = (self.after_task_id or "").strip()
        self.after_minutes = int(self.after_minutes or 0)
        if self.after_task_id:
            if self.after_task_id == self.task_id:
                raise ValueError(f"'{self.title}' can't depend on itself")
            if not 0 < self.after_minutes < MINUTES_PER_DAY:
                raise ValueError("the gap after the other task must be between 1 minute and 24 hours")
        else:
            self.after_minutes = 0

    def occurs_on(self, on_date: date) -> bool:
        return on_date.weekday() in self.days

    @property
    def window_label(self) -> str:
        return f"{fmt(self.window_start)}–{fmt(self.window_end)}"

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "pet_name": self.pet_name,
            "title": self.title,
            "window_start": fmt(self.window_start),
            "window_end": fmt(self.window_end),
            "duration_minutes": self.duration_minutes,
            "description": self.description,
            "priority": self.priority,
            "days": [WEEKDAYS[d] for d in self.days],
            "flex_minutes": self.flex_minutes,
            "after_task": self.after_task_id,
            "after_minutes": self.after_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CareTask":
        return cls(
            pet_name=data["pet_name"],
            title=data["title"],
            window_start=data["window_start"],
            window_end=data["window_end"],
            duration_minutes=int(data["duration_minutes"]),
            description=data.get("description", ""),
            priority=data.get("priority", "medium"),
            days=data.get("days", "daily"),
            flex_minutes=int(data.get("flex_minutes", 0)),
            task_id=data.get("task_id", ""),
            after_task_id=data.get("after_task", ""),
            after_minutes=int(data.get("after_minutes", 0)),
        )


ROUTINE_TEXT_FIELDS = (
    "owner_name", "owner_contact", "vet_name", "vet_phone", "vet_address",
    "emergency_contact", "emergency_procedures", "house_notes",
)


@dataclass
class Routine:
    """Everything the owner hands over: pets, contact info, and care tasks."""

    owner_name: str = ""
    owner_contact: str = ""
    pets: List[PetProfile] = field(default_factory=list)
    tasks: List[CareTask] = field(default_factory=list)
    vet_name: str = ""
    vet_phone: str = ""
    vet_address: str = ""
    emergency_contact: str = ""
    emergency_procedures: str = ""
    house_notes: str = ""

    @property
    def vet_summary(self) -> str:
        return " · ".join(p for p in (self.vet_name, self.vet_phone, self.vet_address) if p)

    def pet(self, name: str) -> Optional[PetProfile]:
        key = name.strip().casefold()
        return next((p for p in self.pets if p.name.casefold() == key), None)

    def task(self, task_id: str) -> Optional[CareTask]:
        return next((t for t in self.tasks if t.task_id == task_id), None)

    def add_pet(self, pet: PetProfile) -> None:
        if self.pet(pet.name) is not None:
            raise ValueError(f"{pet.name} is already in the routine")
        self.pets.append(pet)

    def add_task(self, task: CareTask) -> None:
        if self.pet(task.pet_name) is None:
            raise ValueError(f"add {task.pet_name} as a pet before giving it tasks")
        if self.task(task.task_id) is not None:
            raise ValueError(
                f"{task.pet_name} already has a task called '{task.title}'"
            )
        self.tasks.append(task)
        logger.info("Routine task added: %s (%s)", task.task_id, task.window_label)

    def remove_task(self, task_id: str) -> None:
        self.tasks = [t for t in self.tasks if t.task_id != task_id]
        for t in self.tasks:
            if t.after_task_id == task_id:
                t.after_task_id, t.after_minutes = "", 0

    def check_dependencies(self) -> None:
        """Raise ValueError if a task depends on a missing task or there's a cycle."""
        for task in self.tasks:
            if task.after_task_id and self.task(task.after_task_id) is None:
                raise ValueError(
                    f"{task.pet_name}'s {task.title} depends on a task that isn't in the routine"
                )
            seen = {task.task_id}
            current = task
            while current.after_task_id:
                current = self.task(current.after_task_id)
                if current is None:
                    break
                if current.task_id in seen:
                    raise ValueError(
                        f"{task.pet_name}'s {task.title} is part of a dependency loop"
                    )
                seen.add(current.task_id)

    def to_dict(self) -> dict:
        data = {name: getattr(self, name) for name in ROUTINE_TEXT_FIELDS}
        data["pets"] = [p.to_dict() for p in self.pets]
        data["tasks"] = [t.to_dict() for t in self.tasks]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Routine":
        routine = cls(**{name: data.get(name, "") for name in ROUTINE_TEXT_FIELDS})
        for pet in data.get("pets", []):
            routine.add_pet(PetProfile(**pet))
        for task in data.get("tasks", []):
            routine.add_task(CareTask.from_dict(task))
        routine.check_dependencies()
        return routine

    def save(self, path: Union[str, Path]) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Routine":
        return cls.from_dict(json.loads(Path(path).read_text()))


# --------------------------------------------------------------------------- #
# Sitter-side model
# --------------------------------------------------------------------------- #
@dataclass
class Commitment:
    """Something the sitter has to do on a given date (class, appointment, shift)."""

    title: str
    on_date: date
    start: int
    end: int

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        self.start = to_minutes(self.start)
        self.end = to_minutes(self.end)
        if isinstance(self.on_date, str):
            self.on_date = date.fromisoformat(self.on_date)
        if not self.title:
            raise ValueError("commitment needs a title")
        if self.end <= self.start:
            raise ValueError(f"'{self.title}': end time must be after start time")

    @property
    def label(self) -> str:
        return f"{self.title} ({fmt(self.start)}–{fmt(self.end)})"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "on_date": self.on_date.isoformat(),
            "start": fmt(self.start),
            "end": fmt(self.end),
        }


# --------------------------------------------------------------------------- #
# Plan models
# --------------------------------------------------------------------------- #
@dataclass
class PlanEntry:
    """A block of time on the plan: either a care task or a sitter commitment."""

    kind: str  # "care" or "sitter"
    title: str
    start: int
    end: int
    pet_name: str = ""
    task_id: str = ""
    priority: str = ""
    description: str = ""
    note: str = ""
    after_task_id: str = ""
    after_minutes: int = 0
    task: Optional["CareTask"] = None  # set for care entries

    @property
    def label(self) -> str:
        who = f"{self.pet_name}: " if self.pet_name else ""
        return f"{fmt(self.start)}–{fmt(self.end)} {who}{self.title}"


@dataclass
class ConflictAlert:
    """A care task that could not be placed inside its window."""

    on_date: date
    task: CareTask
    blocking: List[str]
    severity: str  # "critical" for high-priority tasks, else "warning"
    message: str
    resolved: bool = False
    resolution: str = ""


@dataclass
class DayPlan:
    on_date: date
    entries: List[PlanEntry] = field(default_factory=list)
    alerts: List[ConflictAlert] = field(default_factory=list)

    def sorted_entries(self) -> List[PlanEntry]:
        return sorted(self.entries, key=lambda e: (e.start, e.kind != "sitter"))

    def entry_for(self, task_id: str) -> Optional[PlanEntry]:
        return next((e for e in self.entries if e.task_id == task_id), None)

    def is_free(self, start: int, end: int, ignore_task_id: str = "") -> bool:
        return not any(
            _overlaps(start, end, e.start, e.end)
            for e in self.entries
            if not (ignore_task_id and e.task_id == ignore_task_id)
        )

    def blockers(self, start: int, end: int) -> List[PlanEntry]:
        return [e for e in self.entries if _overlaps(start, end, e.start, e.end)]


@dataclass
class PeriodPlan:
    """A plan covering one day (daily view) or several days (weekly view)."""

    start_date: date
    days: List[DayPlan] = field(default_factory=list)

    def day(self, on_date: date) -> Optional[DayPlan]:
        return next((d for d in self.days if d.on_date == on_date), None)

    def alerts(self) -> List[ConflictAlert]:
        return [a for d in self.days for a in d.alerts]

    def unresolved_alerts(self) -> List[ConflictAlert]:
        return [a for a in self.alerts() if not a.resolved]


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #
class SitterPlanner:
    """Places routine tasks around sitter commitments and reports conflicts."""

    def plan_period(
        self,
        routine: Routine,
        start_date: date,
        num_days: int,
        commitments: Sequence[Commitment] = (),
    ) -> PeriodPlan:
        if not 1 <= num_days <= 31:
            raise ValueError("a sitting period must be between 1 and 31 days")
        plan = PeriodPlan(start_date=start_date)
        for offset in range(num_days):
            plan.days.append(
                self.plan_day(routine, start_date + timedelta(days=offset), commitments)
            )
        logger.info(
            "Planned %d day(s) from %s: %d alert(s)",
            num_days,
            start_date.isoformat(),
            len(plan.alerts()),
        )
        return plan

    def plan_day(
        self,
        routine: Routine,
        on_date: date,
        commitments: Sequence[Commitment] = (),
    ) -> DayPlan:
        day = DayPlan(on_date=on_date)
        for c in commitments:
            if c.on_date == on_date:
                day.entries.append(
                    PlanEntry(kind="sitter", title=c.title, start=c.start, end=c.end)
                )

        todays_tasks = [t for t in routine.tasks if t.occurs_on(on_date)]
        # Tightest deadline first, then priority, then the least flexible window.
        todays_tasks.sort(
            key=lambda t: (
                t.window_end,
                PRIORITY_RANK[t.priority],
                t.window_end - t.window_start - t.duration_minutes,
                t.window_start,
            )
        )
        for task in self._dependency_order(todays_tasks):
            anchor = day.entry_for(task.after_task_id) if task.after_task_id else None
            earliest = task.window_start
            note = ""
            if anchor is not None:
                earliest = max(earliest, anchor.start + task.after_minutes)
            elif task.after_task_id and any(t.task_id == task.after_task_id for t in todays_tasks):
                note = (
                    f"Should start at least {gap_label(task.after_minutes)} after "
                    f"{self._task_name(routine, task.after_task_id)}, which isn't scheduled yet."
                )
            start = self._earliest_fit(day, earliest, task.window_end, task.duration_minutes)
            if start is None:
                day.alerts.append(self._conflict(day, task, anchor, earliest))
                continue
            day.entries.append(self._care_entry(task, start, note=note))
        return day

    @staticmethod
    def _task_name(routine: Routine, task_id: str) -> str:
        t = routine.task(task_id)
        return f"{t.pet_name}'s {t.title}" if t else "another task"

    @staticmethod
    def _dependency_order(tasks: List[CareTask]) -> List[CareTask]:
        """Keep the given order, but always place a task after the task it depends on."""
        todays = {t.task_id for t in tasks}
        ordered: List[CareTask] = []
        placed = set()
        pending = list(tasks)
        while pending:
            progress = False
            for task in list(pending):
                anchor = task.after_task_id
                if not anchor or anchor not in todays or anchor in placed:
                    ordered.append(task)
                    placed.add(task.task_id)
                    pending.remove(task)
                    progress = True
                    break  # restart so earlier-sorted tasks keep priority
            if not progress:  # a loop slipped through validation: fall back to given order
                ordered.extend(pending)
                break
        return ordered

    # -- placement helpers -------------------------------------------------- #
    @staticmethod
    def _earliest_fit(
        day: DayPlan,
        earliest: int,
        latest_end: int,
        duration: int,
        ignore_task_id: str = "",
    ) -> Optional[int]:
        candidates = {earliest} | {
            e.end
            for e in day.entries
            if earliest <= e.end and not (ignore_task_id and e.task_id == ignore_task_id)
        }
        for start in sorted(candidates):
            if start + duration > latest_end:
                break
            if day.is_free(start, start + duration, ignore_task_id):
                return start
        return None

    @staticmethod
    def _care_entry(task: CareTask, start: int, note: str = "") -> PlanEntry:
        return PlanEntry(
            kind="care",
            title=task.title,
            start=start,
            end=start + task.duration_minutes,
            pet_name=task.pet_name,
            task_id=task.task_id,
            priority=task.priority,
            description=task.description,
            note=note,
            after_task_id=task.after_task_id,
            after_minutes=task.after_minutes,
            task=task,
        )

    @staticmethod
    def _conflict(
        day: DayPlan,
        task: CareTask,
        anchor: Optional[PlanEntry] = None,
        earliest: Optional[int] = None,
    ) -> ConflictAlert:
        earliest = task.window_start if earliest is None else earliest
        blockers = day.blockers(earliest, task.window_end) if earliest < task.window_end else []
        labels = [
            f"{b.title} ({fmt(b.start)}–{fmt(b.end)})"
            if b.kind == "sitter"
            else f"{b.pet_name}'s {b.title} ({fmt(b.start)}–{fmt(b.end)})"
            for b in blockers
        ]
        severity = "critical" if task.priority == "high" else "warning"
        if anchor is not None and earliest > task.window_start:
            when = (
                f"must start at least {gap_label(task.after_minutes)} after "
                f"{anchor.pet_name}'s {anchor.title} ({fmt(anchor.start)}), so between "
                f"{fmt(earliest)} and {fmt(task.window_end)} on {fmt_date(day.on_date)}"
            )
        else:
            when = f"must happen {task.window_label} on {fmt_date(day.on_date)}"
        if labels:
            reason = f"but that time is taken by {', '.join(labels)}."
        else:
            reason = "but that leaves no time inside its window."
        message = f"{task.pet_name}'s {task.title} {when}, {reason}"
        logger.info("Conflict [%s] %s", severity, message)
        return ConflictAlert(
            on_date=day.on_date,
            task=task,
            blocking=labels,
            severity=severity,
            message=message,
        )

    # -- moves (used by the agent and the offline resolver) ----------------- #
    @staticmethod
    def allowed_range(task: CareTask) -> Tuple[int, int]:
        """Earliest start and latest end the owner allows, including flex."""
        return (
            max(0, task.window_start - task.flex_minutes),
            min(MINUTES_PER_DAY, task.window_end + task.flex_minutes),
        )

    def validate_move(self, day: DayPlan, task: CareTask, new_start: int) -> Optional[str]:
        """Return why moving ``task`` to ``new_start`` is not allowed, or None if it is."""
        if not task.occurs_on(day.on_date):
            return f"{task.title} does not happen on {fmt_date(day.on_date)}."
        lo, hi = self.allowed_range(task)
        new_end = new_start + task.duration_minutes
        if new_start < lo or new_end > hi:
            if task.flex_minutes == 0:
                return (
                    f"The owner set 0 flex minutes for {task.title}: it must stay "
                    f"inside {task.window_label}."
                )
            return (
                f"{fmt(new_start)}–{fmt(new_end)} is outside what the owner allows "
                f"for {task.title} ({fmt(lo)}–{fmt(hi)})."
            )
        blockers = [
            b for b in day.blockers(new_start, new_end) if b.task_id != task.task_id
        ]
        if blockers:
            return f"{fmt(new_start)}–{fmt(new_end)} overlaps " + ", ".join(
                b.label for b in blockers
            )
        anchor = day.entry_for(task.after_task_id) if task.after_task_id else None
        if anchor is not None and new_start < anchor.start + task.after_minutes:
            return (
                f"{task.title} must start at least {gap_label(task.after_minutes)} after "
                f"{anchor.pet_name}'s {anchor.title} ({fmt(anchor.start)}), so no earlier "
                f"than {fmt(anchor.start + task.after_minutes)}."
            )
        _, error = self._dependent_shifts(day, task, new_start)
        return error

    def _dependent_shifts(
        self, day: DayPlan, task: CareTask, new_start: int
    ) -> Tuple[List[Tuple[PlanEntry, int]], Optional[str]]:
        """Tasks that depend on ``task`` and would now start too soon after it.

        Returns the later start each one must move to (the earliest free time
        that keeps its gap and stays inside its window + flex), or an error if
        one of them has nowhere to go.
        """
        shifts = []
        # Look for room as if ``task`` were already at its new time.
        trial = DayPlan(
            on_date=day.on_date,
            entries=[e for e in day.entries if e.task_id != task.task_id]
            + [self._care_entry(task, new_start)],
        )
        for dep in day.entries:
            if dep.after_task_id != task.task_id or dep.start >= new_start + dep.after_minutes:
                continue
            if dep.task is None:
                return [], f"Moving {task.title} would break the timing of {dep.title}."
            lo, hi = self.allowed_range(dep.task)
            earliest = max(lo, new_start + dep.after_minutes)
            start = self._earliest_fit(trial, earliest, hi, dep.task.duration_minutes, ignore_task_id=dep.task_id)
            if start is None or any(
                d.after_task_id == dep.task_id and d.start < start + d.after_minutes for d in day.entries
            ):
                return [], (
                    f"Moving {task.title} to {fmt(new_start)} would leave no time for "
                    f"{dep.pet_name}'s {dep.title}, which must start at least "
                    f"{gap_label(dep.after_minutes)} after it."
                )
            shifts.append((dep, start))
        return shifts, None

    def apply_move(self, day: DayPlan, task: CareTask, new_start: int, note: str) -> PlanEntry:
        """Place a validated move on the plan and mark the matching alert resolved."""
        error = self.validate_move(day, task, new_start)
        if error:
            raise ValueError(error)
        shifts, _ = self._dependent_shifts(day, task, new_start)
        day.entries = [e for e in day.entries if e.task_id != task.task_id]
        entry = self._care_entry(task, new_start, note=note)
        day.entries.append(entry)
        for dep, dep_start in shifts:
            day.entries.remove(dep)
            day.entries.append(
                self._care_entry(
                    dep.task,
                    dep_start,
                    note=f"Shifted to stay {gap_label(dep.after_minutes)} after {task.title}.",
                )
            )
            logger.info("Shifted dependent %s to %s", dep.task_id, fmt(dep_start))
        for alert in day.alerts:
            if alert.task.task_id == task.task_id and not alert.resolved:
                alert.resolved = True
                alert.resolution = note
        logger.info(
            "Moved %s on %s to %s (%s)",
            task.task_id,
            day.on_date.isoformat(),
            fmt(new_start),
            note,
        )
        return entry

    def suggest_flex_start(self, day: DayPlan, task: CareTask) -> Optional[int]:
        """Find the start inside the flex range closest to the original window."""
        lo, hi = self.allowed_range(task)
        best: Optional[int] = None
        best_distance = None
        for start in range(lo, hi - task.duration_minutes + 1, 5):
            if self.validate_move(day, task, start) is not None:
                continue
            end = start + task.duration_minutes
            distance = max(0, task.window_start - start, end - task.window_end)
            if best_distance is None or distance < best_distance:
                best, best_distance = start, distance
        return best


def plan_to_rows(plan: PeriodPlan) -> List[Dict[str, str]]:
    """Flatten a plan into table rows (used by the UI and the CLI demo)."""
    rows = []
    for day in plan.days:
        for e in day.sorted_entries():
            rows.append(
                {
                    "Day": fmt_date(day.on_date),
                    "Time": f"{fmt(e.start)}–{fmt(e.end)}",
                    "Who": "You (sitter)" if e.kind == "sitter" else e.pet_name,
                    "Task": e.title,
                    "Priority": e.priority or "—",
                    "Instructions": e.description,
                    "Note": e.note,
                }
            )
    return rows


def load_commitments(path: Union[str, Path], start_date: date) -> List[Commitment]:
    """Load commitments from JSON. Entries use ``on_date`` or a ``day_offset`` from ``start_date``."""
    items = json.loads(Path(path).read_text())
    commitments = []
    for item in items:
        on_date = (
            date.fromisoformat(item["on_date"])
            if "on_date" in item
            else start_date + timedelta(days=int(item["day_offset"]))
        )
        commitments.append(Commitment(item["title"], on_date, item["start"], item["end"]))
    return commitments
