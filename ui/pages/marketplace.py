# -*- coding: utf-8 -*-
"""
ui/pages/marketplace.py - витрина маркетплейса (навыки).

Страница показывает каталог навыков: список карточек, подробности одной
карточки и кнопку установки. Вся логика - в `core.marketplace.catalog`,
`core.marketplace.installer` и `core.marketplace.registry`; здесь только
отрисовка и обработка нажатий.

Манифест сущности скачивается лениво: список строится по индексу каталога
(имя, описание, версия), а полная карточка - только когда пользователь её
раскрыл. Иначе каждый рендер страницы стоил бы десятка запросов.
"""
import streamlit as st

from core.i18n import t
from core.marketplace import bundles as marketplace_bundles
from core.marketplace import catalog as marketplace_catalog
from core.marketplace import config as marketplace_config
from core.marketplace import conflicts as marketplace_conflicts
from core.marketplace import installer as marketplace_installer
from core.marketplace import registry as marketplace_registry
from core.marketplace import store as marketplace_store
from ui.components.flash import flash_error, flash_info, flash_success


#: session_state key holding the last rendered catalog answer.
_ITEMS_KEY = "mp_items"

#: Stateful tabs widget of the showcase (its value is the active label).
_TABS_KEY = "mp_tabs"

#: Subsection key shared with the sidebar: both write it, so the sidebar
#: highlight follows a tab click and a sidebar click opens the right tab.
SECTION_KEY = "mp_section"

#: Subsection requested by the sidebar, consumed once on the next render.
SECTION_TARGET_KEY = "mp_section_target"


def _marketplace_sections() -> list:
    """Return the ``(section key, type, agent kind, label key)`` subsections.

    The showcase lists the four catalog types and splits the agents by their
    kind: an employee (an orchestrator) and a helper (a one-shot profile)
    are different products that install into different places, so they get
    their own subsections instead of a shared agent list.
    """
    return [
        ("agents", "agent", "orchestrator", "nav_settings_employees"),
        ("helpers", "agent", "assistant", "nav_settings_assistants"),
        ("skills", "skill", "", "nav_settings_skills"),
        ("rag", "rag", "", "nav_storage"),
        ("mcp", "mcp", "", "nav_connectors"),
    ]


def _section_labels(lang: str) -> list:
    """Return the translated subsection labels in their fixed order."""
    return [t(section[3], lang=lang) for section in _marketplace_sections()]


def _apply_section_request(labels: list) -> str:
    """Consume the pending sidebar request and return the tabs ``default``.

    Streamlit allows a widget state to change only before the widget is
    created, so a sidebar click drops the stored tab value and hands the
    wanted label as ``default`` on this pass.
    """
    keys = [section[0] for section in _marketplace_sections()]
    target = st.session_state.pop(SECTION_TARGET_KEY, None)
    if target not in keys:
        return ""
    st.session_state.pop(_TABS_KEY, None)
    # The record of the section is kept here rather than waiting for the
    # tabs to report it back: the sidebar highlight must be right on the
    # very pass that honours the click.
    st.session_state[SECTION_KEY] = target
    return labels[keys.index(target)]


def _sync_section_from_tabs(labels: list) -> None:
    """Record the subsection chosen in the tabs under the shared key.

    After a tab click the stored tab value is the source of truth: writing
    it back keeps the sidebar highlight on the subsection being shown.
    """
    stored = st.session_state.get(_TABS_KEY)
    if stored not in labels:
        return
    sections = _marketplace_sections()
    st.session_state[SECTION_KEY] = sections[labels.index(stored)][0]


def _active_section(labels: list) -> tuple:
    """Return the subsection this render paints, tabs first, sidebar second."""
    sections = _marketplace_sections()
    stored = st.session_state.get(_TABS_KEY)
    if stored in labels:
        return sections[labels.index(stored)]
    target = str(st.session_state.get(SECTION_KEY) or "")
    for section in sections:
        if section[0] == target:
            return section
    return sections[0]


def page_marketplace() -> None:
    """Страница «Маркетплейс»: подразделы каталога и установка."""
    lang = st.session_state.get("ui_lang", "en")
    st.title(t("mp_title", lang=lang))

    if not marketplace_config.is_enabled():
        st.info(t("mp_disabled", lang=lang))
        return

    st.caption(t("mp_intro", lang=lang))
    # Мера R2 до появления подписи каталога (P7): пользователь должен
    # видеть, что описание и файлы приходят извне установки.
    st.warning(t("mp_external_notice", lang=lang))

    _render_toolbar(lang)
    labels = _section_labels(lang)
    default_label = _apply_section_request(labels)
    tabs_kwargs = {"key": _TABS_KEY, "on_change": "rerun"}
    if default_label:
        tabs_kwargs["default"] = default_label
    st.tabs(labels, **tabs_kwargs)
    _sync_section_from_tabs(labels)
    st.markdown("---")
    _render_list(lang, _active_section(labels))


def _filter_section(items: list, section: tuple) -> list:
    """Return the cards that belong to one subsection.

    A subsection pins a catalog type and, for agents, also the kind:
    employees and helpers share the ``agent`` type but install into
    different places, so a shared list would mix two different products.
    """
    _key, type_, kind, _label = section
    selected = []
    for item in items:
        if str(item.get("type") or "skill") != type_:
            continue
        if kind and str(item.get("agent_kind") or "orchestrator") != kind:
            continue
        selected.append(item)
    return selected


def _render_toolbar(lang: str) -> None:
    """Кнопка обновления индекса и поиск по каталогу.

    Поле поиска владеет своим ключом ``mp_query``: Streamlit сам пишет ввод
    в ``session_state`` и запрещает присваивать значение этому ключу после
    создания виджета. Поэтому значение читается из ``session_state`` там,
    где оно нужно, а не копируется обратно в тот же ключ.
    """
    col_refresh, col_search = st.columns([1, 3])
    with col_refresh:
        if st.button(t("mp_refresh", lang=lang), key="mp_refresh", use_container_width=True):
            st.session_state.pop(_ITEMS_KEY, None)
            _load_items(lang, force=True)
            st.rerun()
    with col_search:
        st.text_input(t("mp_search", lang=lang), key="mp_query")


def _query() -> str:
    """Return the normalized current search text."""
    return str(st.session_state.get("mp_query") or "").strip().lower()


def _load_items(lang: str, force: bool = False) -> dict:
    """Возвращает элементы каталога, кэшируя результат в session_state.

    Кэш живёт до явного обновления: повторный рендер страницы (любое
    нажатие) не должен заново ходить в каталог.
    """
    cached = st.session_state.get(_ITEMS_KEY)
    if isinstance(cached, dict) and not force:
        return cached
    with st.spinner(t("mp_loading", lang=lang)):
        answer = marketplace_catalog.list_entities(None, lang=lang, force=force)
    st.session_state[_ITEMS_KEY] = answer
    return answer


def _render_list(lang: str, section: tuple) -> None:
    """Список карточек активного подраздела каталога с поиском."""
    answer = _load_items(lang)
    if not answer.get("ok"):
        st.error(t(
            "mp_catalog_error",
            lang=lang,
            message=answer.get("message") or answer.get("error_code") or "",
        ))
        return

    if answer.get("stale"):
        st.info(t("mp_stale", lang=lang))

    items = _filter_section(list(answer.get("items") or []), section)
    query = _query()
    if query:
        items = [
            item for item in items
            if query in str(item.get("name", "")).lower()
            or query in str(item.get("description", "")).lower()
            or query in str(item.get("id", "")).lower()
        ]

    if not items:
        st.caption(t("mp_empty", lang=lang))
        return

    st.caption(t("mp_counts", lang=lang, total=len(items), installed=answer.get("installed", 0)))
    for item in items:
        _render_item(item, lang)


def _render_item(item: dict, lang: str) -> None:
    """Одна карточка: заголовок, подробности по раскрытию, установка.

    The expanded state lives in ``session_state`` instead of being read off
    the click of the current pass. Any action inside the card - install,
    reset, uninstall - starts a rerun on which no button is pressed, and a
    card that folded itself away there would drop the very action the user
    asked for.
    """
    entity_id = str(item.get("id") or "")
    title = f"{item.get('name') or entity_id} · {item.get('version') or '?'}"
    if item.get("installed"):
        title = f"✅ {title}"
    open_key = f"mp_open_{entity_id}"

    with st.expander(title, expanded=False):
        if item.get("description"):
            st.write(item["description"])
        if item.get("installed"):
            st.caption(t(
                "mp_installed_version",
                lang=lang,
                version=item.get("installed_version") or "?",
            ))
        is_open = bool(st.session_state.get(open_key))
        if st.button(
            t("mp_hide_details" if is_open else "mp_show_details", lang=lang),
            key=f"mp_details_{entity_id}",
        ):
            if is_open:
                st.session_state.pop(open_key, None)
                return
            st.session_state[open_key] = True
            is_open = True
        if not is_open:
            return
        view = _card_view(entity_id, item, lang)
        if not view.get("ok"):
            st.error(t(
                "mp_card_error",
                lang=lang,
                message=view.get("message") or view.get("error_code") or "",
            ))
            return
        _render_card(view, lang)


def _card_view(entity_id: str, item: dict, lang: str) -> dict:
    """Return the card of one entity, reusing the manifest across renders.

    A card that stays open is repainted on every click, and the manifest of
    an entity does not change while the user looks at it: fetching it again
    would cost a request per press. The cache is keyed by the entity, the
    language and the installed state, so a finished install or a removed
    entity still repaints the card with its fresh plan.

    Parameters:
        entity_id: catalog id of the entity.
        item: the list entry the card was opened from (type and state).
        lang: interface language of the labels.
    """
    type_ = str(item.get("type") or "skill")
    row = None
    try:
        row = marketplace_registry.get_installed(type_, entity_id)
    except Exception:
        row = None
    signature = (
        str(item.get("version") or ""),
        str(item.get("installed_version") or ""),
        str((row or {}).get("version") or ""),
        bool((row or {}).get("user_modified")),
    )
    key = f"mp_card_{entity_id}"
    cached = st.session_state.get(key)
    if isinstance(cached, dict) and cached.get("signature") == signature and cached.get("lang") == lang:
        return cached["view"]
    view = marketplace_catalog.fetch_card(entity_id, lang=lang, type_=type_)
    st.session_state[key] = {"signature": signature, "lang": lang, "view": view}
    return view


def _render_card(view: dict, lang: str) -> None:
    """Подробности сущности и кнопка установки."""
    st.markdown(
        t(
            "mp_card_facts",
            lang=lang,
            version=view.get("version") or "?",
            files=view.get("file_count", 0),
            size=_human_size(view.get("size_bytes", 0)),
        )
    )
    licence = view.get("license_spdx") or view.get("license_name") or "-"
    st.markdown(t("mp_license", lang=lang, spdx=licence))

    # An agent is either an employee (an orchestrator with instructions and
    # functions) or a helper (a one-shot profile): the two install into
    # different places, so the card states which one this is.
    kind = str(view.get("agent_kind") or "")
    if kind:
        kind_label = t(f"mp_agent_kind_{kind}", lang=lang)
        st.markdown(t("mp_agent_kind", lang=lang, kind=kind_label or kind))

    if view.get("requires_tools"):
        st.markdown(t(
            "mp_requests_tools",
            lang=lang,
            tools=", ".join(view["requires_tools"]),
        ))
    else:
        st.markdown(t("mp_requests_nothing", lang=lang))

    if view.get("executes_code"):
        st.markdown(t("mp_requests_code", lang=lang))
    if view.get("platform_min") or view.get("platform_max"):
        st.caption(t(
            "mp_platform",
            lang=lang,
            minimum=view.get("platform_min") or "-",
            maximum=view.get("platform_max") or "-",
        ))

    # The recommended package belongs to the card itself, not to one of its
    # actions: the main case is an agent that is already installed and now
    # needs its companion entities, which the ``noop`` branch below returns
    # from early.
    selection = _render_bundle(view, lang)
    members = list(selection.get("members") or [])
    bundle_accepted = bool(selection.get("accepted", True))

    plan = _plan_for(view)
    action = str(plan.get("action") or "")

    if action in ("noop", "modified_hold"):
        if action == "modified_hold":
            st.info(t("mp_modified_by_user", lang=lang))
            st.caption(t(
                "mp_conflict_kept",
                lang=lang,
                files=", ".join(plan.get("modified_files") or []),
            ))
            if st.button(t("mp_reset_to_catalog", lang=lang), key=f"mp_reset_{view.get('id')}"):
                _reset(view, lang)
        else:
            st.success(t("mp_installed", lang=lang))
        if view.get("skipped_code"):
            st.caption(t(
                "mp_skipped_code",
                lang=lang,
                files=", ".join(view["skipped_code"][:5]),
            ))
        # The entity itself is already in place, so the one button below
        # installs only the ticked extras; without extras there is nothing
        # left to press.
        if members:
            _install_button(view, lang, members, bundle_accepted, only_members=True)
        _render_uninstall(view, lang)
        return

    if action == "downgrade_blocked":
        st.warning(t(
            "mp_downgrade_blocked",
            lang=lang,
            installed=plan.get("installed_version") or "?",
            catalog=plan.get("version") or "?",
        ))
        confirmed = st.checkbox(t("mp_force_confirm", lang=lang), key=f"mp_force_{view.get('id')}")
        if st.button(
            t("mp_force_install", lang=lang),
            key=f"mp_force_install_{view.get('id')}",
            disabled=not confirmed,
        ):
            _install(view, lang)
        _render_uninstall(view, lang)
        return

    if action == "conflict_choice":
        st.warning(t("mp_conflict_title", lang=lang))
        st.caption(t(
            "mp_conflict_kept",
            lang=lang,
            files=", ".join(plan.get("modified_files") or []),
        ))
        entity_id = str(view.get("id") or "")
        col_keep, col_take, col_fork = st.columns(3)
        with col_keep:
            if st.button(t("mp_conflict_keep_mine", lang=lang), key=f"mp_keep_{entity_id}"):
                _resolve_choice("keep_mine", view, lang)
        with col_take:
            if st.button(t("mp_conflict_take_catalog", lang=lang), key=f"mp_take_{entity_id}"):
                _resolve_choice("take_catalog", view, lang)
        with col_fork:
            if st.button(t("mp_conflict_fork", lang=lang), key=f"mp_fork_{entity_id}"):
                _resolve_choice("fork", view, lang)
        _render_uninstall(view, lang)
        return

    if action == "suffix_install":
        st.caption(t("mp_suffix_notice", lang=lang, slug=plan.get("runtime_slug") or ""))

    if action == "adopt":
        st.info(t("mp_adopt_notice", lang=lang))
        if st.button(t("mp_install", lang=lang), key=f"mp_adopt_{view.get('id')}"):
            _adopt(view, lang)
        _render_uninstall(view, lang)
        return

    if action == "update":
        st.info(t(
            "mp_update_available",
            lang=lang,
            installed=plan.get("installed_version") or "?",
            available=plan.get("version") or "?",
        ))

    accepted = True
    if view.get("acceptance_required"):
        accepted = st.checkbox(
            t("mp_accept_license", lang=lang, spdx=licence),
            key=f"mp_accept_{view.get('id')}",
        )
    _install_button(view, lang, members, bool(accepted) and bundle_accepted)
    _render_uninstall(view, lang)


def _bundle_cache_key(entity_id: str) -> str:
    """Return the session key holding the cached bundle plan of a card."""
    return f"mp_bundle_plan_{entity_id}"


def _clear_bundle_cache() -> None:
    """Drop every cached bundle plan after the catalog state changed."""
    for key in [key for key in st.session_state.keys() if str(key).startswith("mp_bundle_plan_")]:
        st.session_state.pop(key, None)


def _bundle_plan(entity_id: str, entity: dict) -> dict:
    """Return the recommended-package plan of a card, cached per card.

    Building the plan reads one manifest per member, so a page re-render
    (every button click) must not repeat it. The cache lives until an
    install or a catalog refresh invalidates it.
    """
    key = _bundle_cache_key(entity_id)
    cached = st.session_state.get(key)
    if isinstance(cached, dict):
        return cached
    try:
        plan_data = marketplace_bundles.plan(entity)
    except Exception as exc:  # pragma: no cover - transport failure
        plan_data = {
            "ok": False,
            "items": [],
            "message": str(getattr(exc, "message", exc)),
        }
    st.session_state[key] = plan_data
    return plan_data


def _bundle_label(item: dict, lang: str) -> str:
    """Return the checkbox label of one package member, with its state.

    The type is spelled out in the reader's language rather than as a
    machine tag: a package mixes skills, knowledge bases and connectors,
    and ``[rag]`` says nothing to a user deciding what to tick.
    """
    state = str(item.get("state") or "")
    state_text = t(f"mp_bundle_state_{state}", lang=lang) if state else ""
    name = str(item.get("name") or item.get("id") or "")
    version = str(item.get("version") or "")
    kind = str(item.get("type") or "")
    head = f"{name} {version}".strip()
    if kind:
        kind_label = t(f"mp_type_{kind}", lang=lang)
        head = f"{head} ({kind_label or kind})"
    return f"{head} - {state_text}" if state_text else head


def _render_bundle(view: dict, lang: str) -> list:
    """Render the recommended package of a card and return the ticked members.

    The block appears only when the manifest declares a package. Members
    that are already installed or cannot be installed are shown disabled:
    the user still sees the intent of the package instead of a list that
    silently shrank.

    The block paints no button of its own: the card owns one install button,
    and the members returned here go in together with the entity on that
    single press.
    """
    entity = view.get("entity") or {}
    entity_id = str(view.get("id") or "")
    empty = {"members": [], "accepted": True, "size": 0}
    if not entity_id:
        return empty
    plan_data = _bundle_plan(entity_id, entity)
    items = list(plan_data.get("items") or [])
    if not items:
        return empty

    st.markdown(f"#### {t('mp_bundle_title', lang=lang)}")
    st.caption(t("mp_bundle_hint", lang=lang))

    chosen = []
    for item in items:
        state = str(item.get("state") or "")
        key = f"mp_bundle_pick_{entity_id}_{item.get('type')}_{item.get('id')}"
        if state in ("install", "update"):
            picked = st.checkbox(
                _bundle_label(item, lang),
                value=bool(item.get("default_selected")),
                key=key,
            )
            if picked:
                chosen.append({"type": item.get("type"), "id": item.get("id")})
        else:
            st.checkbox(_bundle_label(item, lang), value=False, key=key, disabled=True)

    st.caption(t(
        "mp_bundle_summary",
        lang=lang,
        count=len(chosen),
        size=_human_size(plan_data.get("size_bytes") or 0),
    ))

    accepted = True
    if plan_data.get("needs_license"):
        accepted = st.checkbox(
            t("mp_bundle_license", lang=lang),
            key=f"mp_bundle_license_{entity_id}",
        )
    return {"members": chosen, "accepted": bool(accepted), "size": plan_data.get("size_bytes") or 0}


def _flash_pending_credentials(entries: list, lang: str) -> None:
    """Warn about connector credentials a just-installed entity still needs.

    A connector installs as a connection without credentials: without the
    hint the user gets a tile that silently answers nothing, so the message
    says which fields to fill in and where.
    """
    pending = sorted({
        str(name)
        for entry in entries
        if str(entry.get('credentials_status') or '') != 'active'
        for name in (entry.get('requires_secrets') or [])
        if str(name).strip()
    })
    if pending:
        flash_info(t('mp_mcp_needs_credentials', lang=lang, fields=', '.join(pending)))


def _install_button(view: dict, lang: str, members: list, accepted: bool, only_members: bool = False) -> None:
    """Paint the single install button of a card and run what it covers.

    One press installs the entity together with the ticked extras of its
    package: a card never shows two competing install buttons.

    Parameters:
        view: the card being rendered.
        lang: interface language.
        members: ticked package members, possibly empty.
        accepted: whether the licence boxes are ticked.
        only_members: the entity is already installed, so the press covers
            just the extras.
    """
    entity_id = str(view.get('id') or '')
    if not st.button(
        t('mp_install', lang=lang),
        key=f'mp_install_{entity_id}',
        disabled=not accepted,
    ):
        return
    if only_members:
        if members:
            _install_bundle(view, members, lang)
        return
    _install_together(view, lang, members)


def _install_bundle(view: dict, chosen: list, lang: str) -> None:
    """Install the chosen package members and report the outcome.

    The module installs as one run: a failure rolls back everything this
    run had already put in place, so the message never claims a partial
    success.
    """
    entity = view.get("entity") or {}
    settings = marketplace_config.marketplace_settings()
    try:
        with st.spinner(t("mp_bundle_installing", lang=lang)):
            report = marketplace_bundles.install(
                entity,
                channel=str(settings.get("channel") or "main"),
                selected=chosen,
                accept_license=True,
            )
    except marketplace_store.MarketplaceError as exc:
        flash_error(t("mp_bundle_failed", lang=lang, message=exc.message))
        st.rerun()
        return
    except Exception as exc:  # pragma: no cover - unexpected failure
        flash_error(t("mp_bundle_failed", lang=lang, message=str(exc)))
        st.rerun()
        return

    st.session_state.pop(_ITEMS_KEY, None)
    _clear_bundle_cache()
    names = ", ".join(
        str(entry.get("id") or "") for entry in report.get("installed") or []
    )
    flash_success(t("mp_bundle_ok", lang=lang, names=names or "-"))
    _flash_pending_credentials(report.get("installed") or [], lang)
    st.rerun()


def _install_together(view: dict, lang: str, members: list) -> None:
    """Install the entity and the ticked extras behind one button press.

    The two steps answer different questions. The entity itself goes through
    the per-entity installer, which reports a no-op when this very version
    is already in place. The extras are a bundle run: all-or-nothing, so a
    failure there rolls the extras back and never touches the entity that
    was installed before it.

    Parameters:
        view: the card being rendered.
        lang: interface language.
        members: ticked package members, possibly empty.
    """
    entity = view.get("entity") or {}
    entity_id = str(view.get("id") or "")
    settings = marketplace_config.marketplace_settings()
    channel = str(settings.get("channel") or "main")

    try:
        with st.spinner(t("mp_installing", lang=lang)):
            result = marketplace_installer.install_entity(
                entity,
                channel=channel,
                accept_license=bool(view.get("acceptance_required")),
            )
    except marketplace_store.MarketplaceError as exc:
        flash_error(t("mp_install_failed", lang=lang, message=exc.message))
        st.rerun()
        return
    except Exception as exc:  # pragma: no cover - unexpected transport failure
        flash_error(t("mp_install_failed", lang=lang, message=str(exc)))
        st.rerun()
        return

    st.session_state.pop(_ITEMS_KEY, None)
    _clear_bundle_cache()
    if not result.get("noop"):
        flash_success(t(
            "mp_install_ok",
            lang=lang,
            name=view.get("name") or entity_id,
            version=result.get("version") or "",
        ))
        _flash_pending_credentials([result], lang)

    if members:
        try:
            with st.spinner(t("mp_bundle_installing", lang=lang)):
                report = marketplace_bundles.install(
                    entity,
                    channel=channel,
                    selected=members,
                    accept_license=True,
                )
        except marketplace_store.MarketplaceError as exc:
            flash_error(t("mp_bundle_failed", lang=lang, message=exc.message))
            st.rerun()
            return
        except Exception as exc:  # pragma: no cover - unexpected failure
            flash_error(t("mp_bundle_failed", lang=lang, message=str(exc)))
            st.rerun()
            return
        names = ", ".join(
            str(entry.get("id") or "") for entry in report.get("installed") or []
        )
        flash_success(t("mp_bundle_ok", lang=lang, names=names or "-"))
        _flash_pending_credentials(report.get("installed") or [], lang)
    st.rerun()


def _entity_type(view: dict) -> str:
    """Return the catalog type of a card (``skill`` when not stated)."""
    return str(view.get("type") or (view.get("entity") or {}).get("type") or "skill")


def _plan_for(view: dict) -> dict:
    """Return the conflict plan of a card, or an empty plan on failure.

    The card must render even when the plan cannot be computed (a broken
    registry, for instance), so a failure degrades to "install" instead of
    blanking the page.
    """
    entity = view.get("entity") or {}
    try:
        return marketplace_conflicts.resolve_plan(
            entity,
            type_=_entity_type(view),
            accept_license=bool(view.get("acceptance_required")),
        )
    except Exception:
        return {"action": "install"}


def _install(view: dict, lang: str) -> None:
    """Install just the entity itself, without its package.

    Used by the forced-downgrade path, where the package is a separate
    decision the user has not made.
    """
    _install_together(view, lang, [])


def _resolve_choice(choice: str, view: dict, lang: str) -> None:
    """Применить один из трёх путей разрешения конфликта.

    Путь выбирает пользователь, поэтому отчёт о прогоне показывается тем,
    что реально произошло с файлами: сохранены, заменены или отложены в форк.
    """
    entity = view.get("entity") or {}
    settings = marketplace_config.marketplace_settings()
    try:
        with st.spinner(t("mp_installing", lang=lang)):
            report = marketplace_conflicts.resolve_three_way(
                choice,
                entity,
                channel=str(settings.get("channel") or "main"),
                type_=_entity_type(view),
                accept_license=bool(view.get("acceptance_required")),
            )
    except marketplace_store.MarketplaceError as exc:
        flash_error(t("mp_install_failed", lang=lang, message=exc.message))
        st.rerun()
        return
    except Exception as exc:  # pragma: no cover - unexpected failure
        flash_error(t("mp_install_failed", lang=lang, message=str(exc)))
        st.rerun()
        return

    st.session_state.pop(_ITEMS_KEY, None)
    if choice == "keep_mine":
        flash_success(t(
            "mp_conflict_kept",
            lang=lang,
            files=", ".join(report.get("kept_files") or []) or "-",
        ))
    elif choice == "fork":
        flash_success(t("mp_suffix_notice", lang=lang, slug=report.get("fork_slug") or ""))
    else:
        flash_success(t(
            "mp_conflict_replaced",
            lang=lang,
            files=", ".join(report.get("replaced_files") or []) or "-",
        ))
        flash_success(t("mp_conflict_backup", lang=lang))
    st.rerun()


def _reset(view: dict, lang: str) -> None:
    """Сбросить локальные правки к версии каталога из кэша пакета."""
    entity = view.get("entity") or {}
    try:
        with st.spinner(t("mp_installing", lang=lang)):
            report = marketplace_conflicts.reset_to_catalog(
                entity, type_=_entity_type(view)
            )
    except marketplace_store.MarketplaceError as exc:
        if str(getattr(exc, "code", "")) == "package_not_cached":
            flash_error(t("mp_reset_no_package", lang=lang))
        else:
            flash_error(t("mp_install_failed", lang=lang, message=exc.message))
        st.rerun()
        return
    except Exception as exc:  # pragma: no cover - unexpected failure
        flash_error(t("mp_install_failed", lang=lang, message=str(exc)))
        st.rerun()
        return

    st.session_state.pop(_ITEMS_KEY, None)
    flash_success(t(
        "mp_reset_ok",
        lang=lang,
        files=", ".join(report.get("reset_files") or []) or "-",
    ))
    st.rerun()


def _adopt(view: dict, lang: str) -> None:
    """Зарегистрировать уже существующую на диске установку."""
    entity = view.get("entity") or {}
    settings = marketplace_config.marketplace_settings()
    try:
        with st.spinner(t("mp_installing", lang=lang)):
            marketplace_conflicts.adopt(
                entity,
                channel=str(settings.get("channel") or "main"),
                type_=_entity_type(view),
            )
    except marketplace_store.MarketplaceError as exc:
        flash_error(t("mp_install_failed", lang=lang, message=exc.message))
        st.rerun()
        return
    except Exception as exc:  # pragma: no cover - unexpected failure
        flash_error(t("mp_install_failed", lang=lang, message=str(exc)))
        st.rerun()
        return

    st.session_state.pop(_ITEMS_KEY, None)
    flash_success(t("mp_adopt_notice", lang=lang))
    st.rerun()


def _render_uninstall(view: dict, lang: str) -> None:
    """Инлайн-подтверждение удаления под карточкой (без модальных окон)."""
    entity_id = str(view.get("id") or "")
    pending = str(st.session_state.get("mp_confirm_remove") or "")
    if pending != entity_id:
        if st.button(t("mp_uninstall", lang=lang), key=f"mp_remove_{entity_id}"):
            st.session_state["mp_confirm_remove"] = entity_id
            st.rerun()
        return

    st.warning(t("mp_uninstall_confirm", lang=lang))
    col_yes, col_no = st.columns(2)
    with col_yes:
        if st.button(t("mp_uninstall_yes", lang=lang), key=f"mp_remove_yes_{entity_id}"):
            _uninstall(view, lang)
    with col_no:
        if st.button(t("mp_uninstall_cancel", lang=lang), key=f"mp_remove_no_{entity_id}"):
            st.session_state.pop("mp_confirm_remove", None)
            st.rerun()


def _uninstall(view: dict, lang: str) -> None:
    """Удалить сущность; карточка остаётся в каталоге, но как неустановленная."""
    entity_id = str(view.get("id") or "")
    st.session_state.pop("mp_confirm_remove", None)
    try:
        marketplace_conflicts.uninstall(_entity_type(view), entity_id)
    except marketplace_store.MarketplaceError as exc:
        flash_error(t("mp_uninstall_failed", lang=lang, message=exc.message))
        st.rerun()
        return
    except Exception as exc:  # pragma: no cover - unexpected failure
        flash_error(t("mp_uninstall_failed", lang=lang, message=str(exc)))
        st.rerun()
        return

    st.session_state.pop(_ITEMS_KEY, None)
    flash_success(t(
        "mp_uninstall_ok",
        lang=lang,
        name=view.get("name") or entity_id,
    ))
    st.rerun()


def _human_size(size_bytes: int) -> str:
    """Return a compact human-readable size label."""
    size = float(size_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
