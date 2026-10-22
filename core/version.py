# core/version.py - single source of truth for the SagaAI platform version.
#
# The platform is published as a stable release: the 2.x line keeps its
# public APIs, UI and data formats backward compatible; breaking changes
# would arrive in a new major version. The version follows SemVer.
#
# 2.0.0 changes the distribution terms: since this release the platform is
# licensed under PolyForm Noncommercial 1.0.0 (commercial use requires a
# separate agreement). Versions 1.x and earlier remain under the MIT License.
#
# 2.1.0 adds the chat folder selector ("Выбор папки"): a static block with
# "Авто", up to 10 recent workspaces and a native OS folder dialog; the picked
# folder is applied automatically on the next send, and the DevAgent prompt
# (v3.21) offers the numbered recent-folders menu for a concrete project.
#
# 2.1.1 restores chat feed auto-scroll (core.feed_autoscroll): while the agent
# works the feed follows the newest output, a manual scroll-up pauses the
# follow and returning to the bottom resumes it.
#
# 2.1.2 removes the module's own bottom padding: Streamlit 1.66 already
# reserves room for the pinned bottom bar, so the manual padding-bottom gave a
# double inset and left a large empty gap between the last message and the
# input form.
#
# 2.1.3 moves file upload into the chat input itself: the bulky separate
# file_uploader dropzone is replaced by st.chat_input(accept_file="multiple")
# (paperclip with a tooltip); files travel with the text submission, and a
# files-only submission uses the i18n placeholder naming the attachments.

# 2.1.4 adds the cold-start dependency audit (core.dependency_check): on
# the first start the platform checks installed libraries and their versions
# and warns the user about missing or outdated packages (with a pip hint);
# requirements.txt now pins streamlit>=1.66.0 instead of an exact ==.
#
# 2.1.5 makes save confirmations visible: a message painted right before
# st.rerun() was discarded with the current pass, so settings pages never
# showed "Saved". Announcements now go through ui.components.flash (a queue in
# session_state) and are painted by render_flash() on the next pass.

#
# 2.1.6 keeps a settings save in place: the employee settings page tracks
# the open section, so after st.rerun() the user stays on the tab they were
# editing instead of landing on the first one, and the queued confirmation
# is visible next to that section (a repeated save is confirmed every time).
# A failed save now reports an error instead of silently doing nothing, and
# a failing folder mirror no longer discards a successful database write.

# 2.2.0 adds the connector-creation architecture through DevAgent: draft
# connections (credentials_status=pending) are created in the chat without
# any secret, filled on the Connectors page, then verified and enabled for
# orchestrators; every connection keeps an agent-maintained memory notes
# file. The connector_creator instruction, the ssh/github instruction
# updates and the credentials-pending marker in the prompt block complete
# the flow.

#
# 2.3.0 adds MCP support through connectors: a from-scratch JSON-RPC 2.0
# stdio client (core/mcp_client.py, stdlib only, no official SDK) plus the
# mcp connector service (command whitelist, env_json secret). Orchestrators
# get mcp_list_tools / mcp_call_tool and dynamic mcp__<conn>__<tool> tools;
# untrusted server data is sanitized, non-text results are rejected and a
# hung server is terminated without blocking the session. The mcp_connector
# DevAgent instruction documents the flow.

#
# 2.4.0 extends MCP through connectors with a preset system: bundled MCP
# server templates (defaults/connectors/mcp/, no secrets) that DevAgent
# instantiates as pending drafts - the user later fills only the credential
# fields on the Connectors page (friendly secret_env_fields mapped to
# env_pairs). MCP connections gain an http transport (https-only or
# http://localhost endpoints) with SSE responses, Mcp-Session-Id replay and
# an Authorization header template resolved from the decrypted connection
# secrets per request; secret updates drop the cached client so the new
# credentials reach the next call. Presets ship for Vkusvill (http),
# GitHub MCP (http) and the official Everything demo (stdio); the
# Connectors page offers a preset picker plus an advanced mode, and the
# mcp_connector instruction documents the preset flow and http transport.

# 2.5.0 reworks the Connectors page MCP UX: MCP servers get their own
# tab with icon preset tiles (a click instantly instantiates the preset
# as a pending draft) and a "+ custom server" tile with a stdio/http
# form. The preset dropdown is gone, the Everything demo preset is no
# longer shipped, and the MCP connections list lives inside the tab.

# 2.6.0 reworks the whole Connectors page around three tabs and one-click
# flows. Tabs keep a stable key and an explicit pin before every rerun, so
# the selected tab survives any click or save; a new "Connections" tab
# lists ALL saved connections (GitHub, SSH, MCP) as cards with the service
# icon, credential state, test / edit / delete actions. General services
# became icon tiles: a click selects the service AND opens the create form
# at once (no second click on the expander), and a successful create lands
# on the Connections tab. An MCP preset tile creates the connection and
# immediately opens its credential form on the Connections tab, so the
# "draft" is never invisible; the same applies to the custom stdio/http
# server form. MCP HTTP errors 401/403 now carry an actionable hint: the
# GitHub Copilot endpoint rejects classic ghp_* PATs and needs a
# fine-grained github_pat_* token.

# 2.7.0 reworks the Connectors page into a list <-> form model, the same
# pattern the assistants page uses: a page-level view key
# (connectors_view = list | create | edit) decides what renders, and a
# form REPLACES the list instead of opening inside an expander or on
# another tab. Clicking a service tile, an MCP preset tile or the custom
# server tile now only opens the create form - nothing is written. The
# connection is persisted solely by the Create button and is active from
# the start (the typed credentials are encrypted with it), so the page
# never leaves an invisible credential-less draft behind; Cancel and the
# back link return to the list without writing anything, and a failed
# save keeps the form open with the entered values. The edit form follows
# the same full-page pattern, and pending drafts created from the chat are
# completed through it with a "Fill credentials" button. Tabs became
# stateful: st.tabs(on_change="rerun", key=..., default=<label>) - without
# on_change="rerun" the widget keeps no state at all, so the stored tab was
# ignored and every rerun fell back to the first tab. After a save the list
# reopens on the Connections tab through the default label.
#
# instantiate_preset gained account/secrets/pending: the page passes
# pending=False and submits the credentials together, while the default
# pending=True keeps the agent's draft-first flow unchanged.

#
# 2.7.1 hides the GitHub MCP preset from the MCP servers tab: the remote
# server at https://api.githubcopilot.com/mcp/ accepts only fine-grained
# PATs carrying the account permission Copilot Requests and answers
# 403 access denied to anything else, so the tile sent users into a dead
# end. Presets gained an optional "hidden" flag: a hidden preset leaves
# the tile catalogue (list_presets) but stays resolvable by id, so a
# connection created from it earlier keeps its preset form and clearing
# the flag brings the tile back. GitHub work goes through the github_rest
# connection (ghr_* tools); any third-party MCP server is still added via
# the "+ Custom MCP server" tile.

__version__ = "2.7.1"

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
