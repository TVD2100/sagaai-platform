"""
ui/pages/storage.py - RAG knowledge base viewing page.

This page is READ-ONLY by design:
  - list existing bases (manifest + index stats);
  - run a test semantic search over a ready base;
  - browse, search, view, edit and delete individual chunks of a base;
  - delete the whole base with inline confirmation.

Bases themselves are NOT created here: creation and population are done by
orchestrators (DevAgent) through the "RAG Base Creator" skill, which uses
core.rag / core.rag_index / core.rag_embeddings.

All persistence goes through core.rag (folder-based manifests + local SQLite
index). No provider data leaves the machine except the embedding requests
made during agent-side re-embedding (BYOK).
"""
import streamlit as st

from core.i18n import t
from core import rag
from core.rag_search import search_base


_STATUS_LABELS = {
    "draft": "storage_status_draft",
    "indexing": "storage_status_indexing",
    "ready": "storage_status_ready",
    "error": "storage_status_error",
}

_CHUNK_PAGE_SIZE = 10
_CHUNK_PREVIEW_CHARS = 160


def _status_label(status: str, lang: str) -> str:
    key = _STATUS_LABELS.get(status or "draft", "storage_status_draft")
    return t(key, lang=lang)


def _render_test_search(slug: str, lang: str) -> None:
    """Render the test semantic search block for base *slug*."""
    st.markdown("---")
    st.markdown(f"**{t('storage_test_search_title', lang=lang)}**")
    query = st.text_input(
        t("storage_search_query", lang=lang), key=f"storage_tq_{slug}",
        help=t("storage_search_query_help", lang=lang),
    )
    if query and st.button(t("storage_search_btn", lang=lang),
                           key=f"storage_tr_{slug}"):
        try:
            hits = search_base(slug, query, top_k=5)
        except Exception as e:
            st.error(str(e))
            hits = []
        if not hits:
            st.caption(t("storage_search_empty", lang=lang))
        for hit in hits:
            st.markdown(
                f"**{hit.get('source') or '?'}** · "
                f"{t('storage_score', lang=lang)} `{hit.get('score', 0.0):.3f}`"
            )
            st.markdown(hit.get("text", ""))
            st.markdown("---")


def _render_chunk_editor(slug: str, chunk_id: int, chunk: dict,
                         lang: str) -> None:
    """Render the inline edit form for one chunk."""
    st.markdown(f"**{t('storage_chunk_edit_title', lang=lang, chunk_id=chunk_id)}**")
    with st.form(f"storage_chunk_edit_form_{slug}_{chunk_id}"):
        new_text = st.text_area(
            t("storage_chunk_text", lang=lang),
            value=str(chunk.get("text") or ""),
            key=f"storage_ce_area_{slug}_{chunk_id}",
            height=220,
            help=t("storage_chunk_text_help", lang=lang),
        )
        save_clicked = st.form_submit_button(
            t("storage_chunk_save", lang=lang)
        )
    if save_clicked:
        try:
            outcome = rag.update_chunk(
                slug, chunk_id, new_text, reembed=True
            )
        except Exception as e:
            st.error(str(e))
            return
        if outcome.get("ok"):
            st.session_state[f"storage_edit_chunk_{slug}"] = None
            if outcome.get("reembedded"):
                st.success(t("storage_chunk_saved_reembedded", lang=lang))
            else:
                st.warning(
                    t(
                        "storage_chunk_saved_no_embedding",
                        lang=lang,
                        warning=str(outcome.get("warning") or ""),
                    )
                )
            st.rerun()
        else:
            st.error(t("storage_chunk_save_error", lang=lang))


def _render_chunks_section(slug: str, lang: str) -> None:
    """Render chunk management (list/search/view/edit/delete) for a base."""
    st.markdown("---")
    st.markdown(f"**{t('storage_chunks_title', lang=lang)}**")

    qkey = f"storage_chunk_query_{slug}"
    lkey = f"storage_chunk_loaded_{slug}"
    current_query = st.session_state.get(qkey, "")
    query = st.text_input(
        t("storage_chunks_search_placeholder", lang=lang),
        key=f"storage_cq_{slug}",
        value=current_query,
        label_visibility="collapsed",
        help=t("storage_chunks_search_help", lang=lang),
    )
    if query != current_query:
        st.session_state[qkey] = query
        st.session_state[lkey] = _CHUNK_PAGE_SIZE
        st.rerun()

    loaded = int(st.session_state.get(lkey, _CHUNK_PAGE_SIZE))
    try:
        page = rag.list_chunks(
            slug, query=st.session_state.get(qkey, ""),
            limit=loaded, offset=0,
        )
    except Exception:
        page = {"total": 0, "chunks": []}
    total = int(page.get("total", 0))
    chunks = page.get("chunks", []) or []

    st.caption(t("storage_chunks_total", lang=lang, total=total))
    if not chunks:
        st.info(t("storage_chunks_empty", lang=lang))
        return

    for chunk in chunks:
        cid = int(chunk.get("chunk_id", 0))
        source = chunk.get("source") or "?"
        cidx = int(chunk.get("chunk_index", 0) or 0)
        has_embedding = bool(chunk.get("has_embedding", False))
        text = str(chunk.get("text") or "")
        embed_mark = "" if has_embedding else " · ⚠ " + t(
            "storage_chunk_no_embedding", lang=lang
        )
        title = (
            f"#{cid} · {source} · "
            f"{t('storage_chunk_index_label', lang=lang)} {cidx}"
            f"{embed_mark}"
        )
        with st.expander(title, expanded=False):
            preview = text[: _CHUNK_PREVIEW_CHARS]
            if len(text) > _CHUNK_PREVIEW_CHARS:
                preview += "…"
            st.markdown(text)
            edit_key = f"storage_edit_chunk_{slug}"
            confirm_key = f"storage_confirm_delete_chunk_{slug}"

            if st.session_state.get(edit_key) == cid:
                _render_chunk_editor(slug, cid, chunk, lang)
            btn_cols = st.columns(4)
            with btn_cols[0]:
                if st.button(t("storage_chunk_edit", lang=lang),
                             key=f"storage_ce_{slug}_{cid}",
                             use_container_width=True):
                    st.session_state[edit_key] = cid
                    st.rerun()
            with btn_cols[1]:
                if st.session_state.get(confirm_key) == cid:
                    yes_col, no_col = st.columns(2)
                    with yes_col:
                        if st.button(t("btn_yes_delete", lang=lang),
                                     key=f"storage_cd_yes_{slug}_{cid}",
                                     use_container_width=True):
                            if rag.delete_chunk(slug, cid):
                                st.session_state[confirm_key] = None
                                st.success(t("storage_chunk_deleted", lang=lang))
                                st.rerun()
                            else:
                                st.error(t("storage_chunk_delete_error", lang=lang))
                    with no_col:
                        if st.button(t("btn_cancel", lang=lang),
                                     key=f"storage_cd_no_{slug}_{cid}",
                                     use_container_width=True):
                            st.session_state[confirm_key] = None
                            st.rerun()
                else:
                    if st.button(t("storage_chunk_delete", lang=lang),
                                 key=f"storage_cd_{slug}_{cid}",
                                 use_container_width=True):
                        st.session_state[confirm_key] = cid
                        st.rerun()

    if loaded < total:
        if st.button(
            t("storage_chunks_show_more", lang=lang),
            key=f"storage_more_{slug}",
        ):
            st.session_state[lkey] = loaded + _CHUNK_PAGE_SIZE
            st.rerun()
    elif loaded > _CHUNK_PAGE_SIZE and total:
        if st.button(
            t("storage_chunks_collapse", lang=lang),
            key=f"storage_less_{slug}",
        ):
            st.session_state[lkey] = _CHUNK_PAGE_SIZE
            st.rerun()


def _render_base_card(base: dict, lang: str) -> None:
    """Render an expander card for one RAG base with viewing controls."""
    slug = base.get("slug", "")
    name = base.get("name") or slug
    status = base.get("status") or "draft"
    active = bool(base.get("active", True))
    stats = base.get("index_stats") or {}
    chunks = stats.get("chunks", 0)
    embeddings = stats.get("embeddings", 0)

    inactive_mark = " · ⚠️ " + t("storage_inactive", lang=lang) if not active else ""
    header = (
        f"**{name}** · {base.get('provider', '')} · "
        f"{_status_label(status, lang)} · "
        f"{t('storage_stats_chunks', lang=lang, chunks=chunks, embeddings=embeddings)}"
        f"{inactive_mark}"
    )
    with st.expander(header, expanded=False):
        if not active:
            st.warning(t("storage_inactive_hint", lang=lang))
        st.caption(f"slug: `{slug}` · "
                   f"{t('storage_embedding_model', lang=lang)}: "
                   f"`{base.get('embedding_model', '')}`")
        if base.get("description"):
            st.markdown(base["description"])

        actions = st.columns(4)
        with actions[0]:
            if st.button(t("storage_search_btn", lang=lang),
                         key=f"storage_ts_{slug}",
                         use_container_width=True):
                st.session_state["storage_test_search"] = slug
                st.session_state.pop("storage_test_results", None)
        with actions[1]:
            show_key = f"storage_show_chunks_{slug}"
            label = (
                t("storage_chunks_hide_btn", lang=lang)
                if st.session_state.get(show_key) else
                t("storage_chunks_show_btn", lang=lang)
            )
            if st.button(label, key=f"storage_chunks_btn_{slug}",
                         use_container_width=True):
                st.session_state[show_key] = not st.session_state.get(show_key)
                st.rerun()
        with actions[2]:
            if st.session_state.get("storage_confirm_delete") == slug:
                second_col = st.columns(2)
                with second_col[0]:
                    if st.button(t("btn_yes_delete", lang=lang),
                                 key=f"storage_del_yes_{slug}",
                                 use_container_width=True):
                        try:
                            rag.delete_base(slug)
                            st.session_state["storage_confirm_delete"] = None
                            st.success(t("storage_deleted", lang=lang))
                            st.rerun()
                        except Exception:
                            st.error(t("storage_error", lang=lang))
                with second_col[1]:
                    if st.button(t("btn_cancel", lang=lang),
                                 key=f"storage_del_no_{slug}",
                                 use_container_width=True):
                        st.session_state["storage_confirm_delete"] = None
                        st.rerun()
            else:
                if st.button(t("btn_delete", lang=lang),
                             key=f"storage_del_{slug}",
                             use_container_width=True):
                    st.session_state["storage_confirm_delete"] = slug
                    st.rerun()

        if st.session_state.get("storage_test_search") == slug:
            _render_test_search(slug, lang)

        if st.session_state.get(f"storage_show_chunks_{slug}"):
            _render_chunks_section(slug, lang)


def page_storage() -> None:
    """RAG storage viewing page (dispatched from ui.app).

    Shows existing bases only. Base creation/population is performed by
    DevAgent through the RAG Base Creator skill.
    """
    lang = st.session_state.get("ui_lang", "en")
    st.title(t("page_storage_title", lang=lang))
    st.markdown(t("storage_readonly_hint", lang=lang))

    bases = rag.list_bases_with_activity()
    if not bases:
        st.info(t("storage_empty", lang=lang))
        return

    for base in bases:
        _render_base_card(base, lang)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
