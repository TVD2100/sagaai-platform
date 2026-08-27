"""
ui.pages.welcome - welcome page content, shown inside the standard layout.
Displays platform introduction and user guide loaded from language-specific MD files.
"""
import os
import streamlit as st
from core.i18n import t
from core.paths import LANGS_DIR
from core.orchestrators import DEVAGENT_SLUG
from core.version import __version__ as PLATFORM_VERSION


def _guide_filename(lang_display: str) -> str:
    """Map the user-facing language display name to the guide MD filename."""
    mapping = {
        "Русский": "ru_guide.md",
        "English": "en_guide.md",
        "简体中文": "zh_CN_guide.md",
    }
    return mapping.get(lang_display, "en_guide.md")


def _goto(page: str):
    """Switch to another top-level page."""
    st.session_state["current_page"] = page
    st.rerun()


def page_welcome():
    """Render the welcome page with intro and a detailed user guide from MD files."""
    lang = st.session_state.get("ui_lang", "")

    # ── Hero: title + intro (compact, no large empty space) ──────────────
    st.markdown(
        f"""
    <div style="max-width:820px;margin:0 auto;padding:0.5rem 1rem 0 1rem;text-align:center;">
        <h1 style="font-size:2.2rem;font-weight:700;margin:0.25rem 0 0.4rem 0;">
            🏡 {t('page_welcome_title', lang=lang)}
        </h1>
        <p style="font-size:1.05rem;margin:0 0 0.6rem 0;line-height:1.6;color:#a0a0a0;">
            🤖 {t('page_welcome_intro', lang=lang)}
        </p>
        <p style="font-size:0.82rem;margin:0;color:#7a7a7a;">
            v{PLATFORM_VERSION} (pre-release)
        </p>
    </div>
    """,
        unsafe_allow_html=True,
    )

    # ── "How to start" - 4 steps as cards with navigation buttons ────────
    st.markdown(
        f"""
    <div style="max-width:820px;margin:0.8rem auto 0.2rem auto;">
        <h2 style="font-size:1.4rem;font-weight:700;margin:0.5rem 0 0.6rem 0;text-align:center;">
            🚀 {t('page_welcome_howto', lang=lang)}
        </h2>
    </div>
    """,
        unsafe_allow_html=True,
    )

    col1, col2, col3, col4 = st.columns(4)

    steps = [
        ("settings",
         t("page_welcome_step1_title", lang=lang),
         t("page_welcome_step1_desc", lang=lang),
         t("page_welcome_btn_llm", lang=lang)),
        (f"orchestrator_settings:{DEVAGENT_SLUG}",
         t("page_welcome_step2_title", lang=lang),
         t("page_welcome_step2_desc", lang=lang),
         t("page_welcome_btn_devagent", lang=lang)),
        ("skills",
         t("page_welcome_step3_title", lang=lang),
         t("page_welcome_step3_desc", lang=lang),
         t("page_welcome_btn_skills", lang=lang)),
        ("orchestrators",
         t("page_welcome_step4_title", lang=lang),
         t("page_welcome_step4_desc", lang=lang),
         t("page_welcome_btn_orchs", lang=lang)),
    ]

    cols = [col1, col2, col3, col4]
    for col, (page, title, desc, btn_label) in zip(cols, steps):
        with col:
            st.markdown(
                f"""
            <div style="border:1px solid rgba(128,128,128,0.25);border-radius:10px;
                        padding:0.9rem 0.7rem;text-align:center;height:150px;
                        display:flex;flex-direction:column;justify-content:center;">
                <div>
                    <div style="font-weight:600;font-size:1.05rem;margin-bottom:0.35rem;">{title}</div>
                    <div style="font-size:0.78rem;color:#7a7a7a;line-height:1.35;">{desc}</div>
                </div>
            </div>
            """,
                unsafe_allow_html=True,
            )
            if st.button(btn_label, key=f"welcome_step_{page}", use_container_width=True):
                _goto(page)

    # ── Detailed guide (language-specific MD) ────────────────────────────
    guide_file = _guide_filename(lang)
    guide_path = os.path.join(LANGS_DIR, guide_file)
    guide_text = ""
    if os.path.isfile(guide_path):
        try:
            with open(guide_path, encoding="utf-8") as f:
                guide_text = f.read()
        except Exception:
            guide_text = ""

    if guide_text:
        st.markdown("---")
        st.markdown(guide_text)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
