# -*- coding: utf-8 -*-
"""tests/test_devagent_instructions.py - canonical DevAgent instruction files.

Every instruction bundled under defaults/orchestrators/dev_agent/instructions/
must have a valid front-matter (id/name/description), a meaningful body and
(for connector instructions) the mandatory blocks of its contract. This
closes the class of regressions where an instruction is edited without
keeping its promised structure.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_INSTR_DIR = os.path.join(
    _REPO_ROOT, "defaults", "orchestrators", "dev_agent", "instructions"
)


def _read(rel: str) -> str:
    with open(os.path.join(_INSTR_DIR, rel), "r", encoding="utf-8") as f:
        return f.read()


def _front_matter_matches(text: str, instruction_id: str) -> bool:
    return text.startswith("---\n") and f"id: {instruction_id}\n" in text


# ─── connector_creator ────────────────────────────────────────────────────────


def test_connector_creator_structure():
    text = _read("connector_creator.md")
    assert _front_matter_matches(text, "connector_creator")
    assert "name: Connector Creator\n" in text
    assert "description:" in text
    assert "Golden rule: no credentials in the chat" in text
    assert "Step-by-step workflow" in text
    assert "Per-connection memory file (mandatory discipline)" in text
    assert "Safety & hygiene" in text


def test_connector_creator_draft_flow():
    """The default creation flow is a pending draft; credentials go to the
    Connectors page, not through the chat."""
    text = _read("connector_creator.md")
    assert "pending=true" in text
    assert "Connectors page" in text
    assert "set_enabled_connections" in text
    assert "get_enabled_connections" in text
    assert "list_connector_services" in text
    assert "test_connection" in text


def test_connector_creator_memory_discipline():
    text = _read("connector_creator.md")
    assert "get_connection_memory" in text
    assert "set_connection_memory" in text
    assert "Never store secrets" in text


def test_connector_creator_forbids_secrets_in_chat_and_reporting():
    text = _read("connector_creator.md")
    lowered = text.lower()
    assert "no credentials in the chat" in lowered
    assert "never repeat the secret back" in lowered
    assert "never write it into any file" in lowered
    assert "never persist it outside" in lowered


# ─── ssh_connector ────────────────────────────────────────────────────────────


def test_ssh_connector_documents_first_connection():
    text = _read("ssh_connector.md")
    assert "## First connection to an unknown server" in text
    for phrase in (
        "What kind of server is it",
        "provider-documented restrictions",
        "AUTOMATIC BLOCKING",
        "Do not guess",
    ):
        assert phrase in text


def test_ssh_connector_documents_memory_file():
    text = _read("ssh_connector.md")
    assert "## Connection memory file (mandatory)" in text
    assert "get_connection_memory" in text
    assert "set_connection_memory" in text
    assert "Never store" in text
    assert "passwords, private keys, passphrases" in text


# ─── github_connector ─────────────────────────────────────────────────────────


def test_github_connector_documents_memory_file():
    text = _read("github_connector.md")
    assert "## Connection memory file (mandatory)" in text
    assert "get_connection_memory" in text
    assert "set_connection_memory" in text
    assert "Never store secrets" in text
