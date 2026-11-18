"""Smoke test of the Streamlit UI (both roles) using Streamlit's AppTest."""

from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def _button(at, label):
    return next(b for b in at.button if b.label == label)


def _text(at, label):
    return next(t for t in at.text_input if t.label == label)


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PAWPAL_DB_PATH", str(tmp_path / "db.json"))
    monkeypatch.setenv("PAWPAL_DISABLE_LLM", "1")
    import streamlit as st

    st.cache_resource.clear()  # pick up the temp database path
    yield
    st.cache_resource.clear()


def test_owner_then_sitter_flow(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    _button(at, "Continue as owner").click().run()
    _button(at, "Add sample owner").click().run()
    assert not at.exception
    assert [r.options for r in at.radio] == [["🐾 Luna (cat)", "🐾 Mochi (dog)"]]

    _button(at, "Switch role").click().run()
    _button(at, "Continue as sitter").click().run()
    _text(at, "Your name").input("Alex").run()
    owners = next(s for s in at.selectbox if s.label.startswith("Are you sitting"))
    owners.select_index(owners.options.index("Jordan")).run()
    at.date_input[0].set_value(date(2026, 11, 23)).run()

    _text(at, "What").input("Dentist")
    at.date_input[1].set_value(date(2026, 11, 24))
    from_box = next(s for s in at.selectbox if s.label == "From")
    until_box = next(s for s in at.selectbox if s.label == "Until")
    assert from_box.options[15] == "7:30 AM" and "1:30 PM" in from_box.options
    assert until_box.options[-1] == "12:00 AM (midnight)"
    from_box.select_index(from_box.options.index("7:30 AM"))
    until_box.select_index(until_box.options.index("9:00 AM"))
    _button(at, "Add").click().run()
    assert not at.exception
    assert dict((m.label, m.value) for m in at.metric)["Conflicts"] == "2"
    assert any("Thyroid medication" in e.value for e in at.error)

    _button(at, "💾 Save sitting job").click().run()
    _button(at, "Fix conflicts (rule-based)").click().run()
    assert dict((m.label, m.value) for m in at.metric)["Still need a decision"] == "1"

    # A fresh session can resume the saved job.
    at2 = AppTest.from_file(APP, default_timeout=30).run()
    _button(at2, "Continue as sitter").click().run()
    _text(at2, "Your name").input("alex").run()
    owners = next(s for s in at2.selectbox if s.label.startswith("Are you sitting"))
    owners.select_index(owners.options.index("Jordan")).run()
    jobs = next(s for s in at2.selectbox if s.label.startswith("Continue a saved"))
    jobs.select_index(1).run()
    assert not at2.exception
    assert at2.date_input[0].value == date(2026, 11, 23)
    assert dict((m.label, m.value) for m in at2.metric)["Conflicts"] == "2"


def test_clicking_task_name_opens_it_for_editing(app_env) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    _button(at, "Continue as owner").click().run()
    _button(at, "Add sample owner").click().run()
    at.radio[0].set_value("🐾 Mochi (dog)").run()

    _button(at, "✏️ Dinner").click().run()
    assert not at.exception
    assert next(s for s in at.selectbox if s.label == "Add or edit a task").value == "Dinner"
    assert _text(at, "Task").value == "Dinner"

    _text(at, "Task").input("Supper")
    _button(at, "Save task").click().run()
    assert "✏️ Supper" in [b.label for b in at.button]
    assert next(s for s in at.selectbox if s.label == "Add or edit a task").value == "➕ New task"
