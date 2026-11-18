"""PawPal+ Sitter — Streamlit entry point.

First screen asks whether you're an owner or a sitter; each role has its own UI:

* views/owner_view.py  — owner profile, pets (traits, requirements, health,
  daily routine), vet contacts, emergency plan.
* views/sitter_view.py — pick a saved owner and pets, add your own plans,
  get a daily/weekly schedule with conflict alerts, ask care questions.

Data is saved locally to data/pawpal_db.json (see storage.py).
"""

from __future__ import annotations

import os

import streamlit as st

from llm import MODEL, get_client
from pawpal_logging import read_recent_logs, setup_logging
from storage import Store, StorageError, get_store
from views import owner_view, sitter_view

setup_logging()
st.set_page_config(page_title="PawPal+ Sitter", page_icon="🐾", layout="wide")


def _secret(name: str) -> str:
    """Read a value from .streamlit/secrets.toml, falling back to the environment."""
    try:
        value = st.secrets.get(name)
    except Exception:  # no secrets file
        value = None
    return value or os.environ.get(name, "")


def store() -> Store:
    # Not cached: a Store is just a file path, and caching it would keep an
    # old Store object alive across code reloads.
    return get_store()


@st.cache_resource
def claude():
    key = _secret("ANTHROPIC_API_KEY")
    if key:
        os.environ.setdefault("ANTHROPIC_API_KEY", key)
    return get_client()


ss = st.session_state
db = store()
client = claude()

with st.sidebar:
    st.header("🐾 PawPal+ Sitter")
    if ss.get("role"):
        st.write(f"Signed in as: **{'Owner' if ss.role == 'owner' else 'Sitter'}**")
        if st.button("Switch role", width="stretch"):
            for key in ("role", "resolution", "plan_cache", "qa_history"):
                ss.pop(key, None)
            st.rerun()
    st.divider()
    st.caption(f"💾 Saved on this computer: {db.path.name}")
    st.caption(f"🤖 AI: Claude ({MODEL})" if client else "🤖 AI: offline mode (set ANTHROPIC_API_KEY to enable Claude)")
    with st.expander("Activity log"):
        st.code(read_recent_logs(60) or "No activity yet.", language=None)

if not ss.get("role"):
    st.title("🐾 PawPal+ Sitter")
    st.markdown("#### Who's using PawPal+ today?")
    owner_col, sitter_col = st.columns(2)
    with owner_col.container(border=True):
        st.markdown("### 🏠 I'm an owner")
        st.write("Add your pets, their routines, health needs, vet, and emergency plan for your sitter.")
        if st.button("Continue as owner", type="primary", width="stretch"):
            ss.role = "owner"
            st.rerun()
    with sitter_col.container(border=True):
        st.markdown("### 🧑 I'm a sitter")
        st.write("Pick who you're sitting for, add your own plans, and get a schedule that fits both.")
        if st.button("Continue as sitter", type="primary", width="stretch"):
            ss.role = "sitter"
            st.rerun()
else:
    try:
        if ss.role == "owner":
            owner_view.render(db)
        else:
            sitter_view.render(db, client)
    except StorageError as error:
        st.error(f"Couldn't load your saved data. {error}")
