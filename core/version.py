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

# 2.8.0 adds the optional url-fetch model to employee settings: the
# employee can be given a page-fetch model, and only providers that declare
# a fetch_models catalog are offered (DeepSeek ships one with
# deepseek-flash and deepseek-v4-pro over its Search/Fetch API at
# https://api.deepseek.com/v1/tools/fetch). The page is downloaded by
# core.url_fetch (POST {"url"}) and its Markdown body is sanitized and
# labelled [DATA_FROM_URL_FETCH] as untrusted data, capped at 30,000
# characters. While the model is assigned, the tool appears in the
# employee's tool catalog, its '## Available tools' prompt block and the
# system-functions list; clearing the model - or the provider dropping it
# from the catalog - removes the tool again.

# 2.9.0 renames that tool to url_fetch (page_fetch stays a legacy alias)
# and widens what it can fetch. Two new sources join the provider API: a
# PDF link is downloaded directly and its text extracted page by page
# (pdfplumber with a pypdf fallback), and a 'Use Selenium' checkbox lets
# the tool render any page in a headless browser and hand the visible text
# to the employee's main model for Markdown conversion. Direct downloads
# pass an SSRF guard that rejects loopback, private, link-local and
# service-discovery hosts. url_fetch is offered when a fetch model is
# assigned OR Selenium is enabled, and disappears once both are gone.

# 2.9.1 refreshes DevAgent's shipped defaults and tidies two settings
# behaviours. The employee now starts on DeepSeek/deepseek-flash for the
# main model with the xhigh reasoning effort, for image analysis and for
# web search (temperature 0.4, three tool calls, the high effort); image
# generation stays unassigned, so a fresh install needs no extra setup.
# The Models tab draws the Selenium checkbox first and hides the
# page-fetch model selectors while it is on, keeping the stored pair for
# when it is cleared; save confirmations are now painted below the page
# body instead of above the page title.

# 2.10.0 adds multi-agent orchestration: an employee can spawn another
# employee (spawn_agent) and call an assistant once (invoke_assistant)
# through the core/orchestration package - a versioned run contract,
# an orchestration service with a run tree, a slot pool, six delegation
# limits (depth, active children, steps, total runs, timeout, token
# budget), cascade cancellation and fan-out of independent spawns in the
# executing phase. Children work in the parent's folder with their own
# derived thread and task journal; the Delegation settings tab hands out
# the allowlists and limits.

# 2.11.0 makes documents readable without code: core/document_text
# extracts text from PDF, DOCX, PPTX, XLSX, XLS, RTF, ODT, HTML and plain
# text, the new read_document tool opens a file on disk by absolute path
# with a line window, and read_thread_file returns attachment text the same
# way; run_code is marked as a last resort. Delegation gets explicit
# contexts: spawn_agent carries the absolute paths of the parent workspace
# and its dialog files folder, invoke_assistant reads a document as one
# call, and the system prompt lists the available agents and assistants
# with their ids.

# 2.12.0 makes delegation asynchronous and resumable. A child can be
# launched with background=true: the dialog gets its run_id at once, keeps
# working, and learns about the finish from the tree mailbox delivered as a
# hidden system notice on the next step, never as a visible user line. Runs
# are stored durably (atomic records, heartbeat, heal of a dead claim by
# stale heartbeat / foreign boot id / missing pid) and observed through
# subagent_status (one run, a whole tree, or a bounded wait),
# read_subagent_state (the child's own journal by absolute path) and
# resume_subagent, which continues from the journal: finished steps and the
# files they produced are not redone, the run keeps its tree and counters,
# and a resume chain is capped. A child has no human to confirm anything, so
# a call that needs confirmation is refused with confirmation_required
# instead of hanging the run.

# 2.13.0 makes the prompt-injection protection mode follow a delegated child.
# A child used to start protected on its own, and a launch that explicitly
# asked for less could be lost on the way to the runner - the dialog's Safety
# setting applied to the parent and not to the child. The mode is now resolved
# once at spawn (an explicit request, else the mode of the run being resumed,
# else protected) and frozen in the run record, so a background child never
# reads the setting of a dialog that may already be gone. A one-step viewing
# permission approved for a single withheld payload is not a mode and is not
# inherited, and the model cannot change any of this from a tool argument.

# 2.14.0 splits memory into two stores. The personal layer
# (core/personal_memory.py) keeps notes about HOW to work - an owner card
# shared by every employee plus per-employee and per-project scopes, capped
# at 64 KiB, written atomically with revision headers and ten recoverable
# versions, screened for credentials, and archived instead of deleted when an
# employee leaves; it lives in DATA_DIR/personal and never leaves the
# installation. The working-data folder (MEMORY_DIR, resolve_data_dir) keeps
# the role's own registries, cards and journals inside the working project
# and travels with it. Three agent tools read and write the personal layer,
# the owner card and a size-only employee block are injected into the system
# prompt, the employee settings page gets a Memory tab, and the
# employee_creator instruction gains a data-structure profile section that
# picks FLOW / CASE / COURSE / ARTICLE / RESEARCH layouts by the shape of the
# unit of work.

# 2.14.1 makes the memory rules reach every employee from the first dialog.
# The `## Memory` block used to appear only once an employee already had
# notes, so a fresh install never taught the practice: the one place that
# could start it stayed silent. The block is now emitted always, and it
# states WHEN to write - after the owner corrects an approach, after a
# decision that will matter next time, after a discovered limit - instead of
# only how. The owner profile is shared, so it can be extended with append
# but not rewritten wholesale: a replace needs overwrite_owner_profile, set
# only on an explicit owner request. The catalog no longer claims `user` as
# the default read scope (it resolves to the employee's own scope), and a
# role whose working-data folder is gone is told to ask rather than silently
# start a new one.

# 2.15.0 makes subagent failures diagnosable. A raise inside the child loop
# is no longer reported as a provider failure: it becomes internal_error with
# retryable=False, while a provider failure keeps provider_error and stays
# retryable, so a caller can tell "retry once" from "this will fail the same
# way". The run record freezes the child thread id and the workspace before
# the child starts, and the finish appends a Runtime outcome paragraph to the
# child journal even when the child never wrote one. The delegation tools
# carry a note field that turns a status into the next decision - platform
# defect, retryable provider, exhausted step budget, expired wait meaning
# still running, empty journal meaning an unwritten record. A state
# assignment in the child_mode branch of the loop is fixed.

# 2.16.0 adds the blocking sleep tool. Instead of polling subagent_status in
# idle steps, a parent waits for an event or a timeout: sleep(seconds=...,
# until="any_child"|"timeout") blocks a step, and the caller picks the
# duration - a child is a full agent, not a command, and may work for tens of
# minutes. The ceiling MAX_SLEEP_SECONDS = 3600 (one hour) only guards against
# an accidental frozen step and marks a shortened wait as clamped. The wait is
# a poll with a growing interval - the
# orchestration core has no condition variable - and each tick checks the
# cancellation of the caller, the terminal runs of the tree and the mailbox
# read-only: sleep never drains a notification. The answer carries woken_by
# (event / timeout / cancelled) and finished, the run ids that became
# terminal during the wait, never their summaries or artifacts - those are
# read with subagent_status or read_subagent_state. A timeout cancels
# nothing, since ending a wait is not ending the work. The tool is enabled
# for children too, and the tree root (the mailbox and run-list key) is
# derived from the parent record because a child knows only its parent.
#
# 2.16.1 synchronizes the wait and the work budgets. The default time budget
# of one child run rises from 600 s to an hour, since sleep is built around a
# full agent rather than a command, and an upper bound
# MAX_SUBAGENT_TIMEOUT_SECONDS clamps a hand-written value - a child must not
# outlive the dialog that spawned it. MAX_SLEEP_SECONDS is derived from that
# same constant instead of being a second, drifting number, and a blocking
# spawn waits for a pool slot no longer than min(run budget,
# DEFAULT_POOL_WAIT_SECONDS), so a busy pool cannot stall one call for an
# hour. The child heartbeat is refreshed from step events at most once a
# minute: a long healthy run must not be healed into a dead one.

# 2.17.0 retunes the delegation defaults against real usage and stops task
# journals from piling up. New orchestrators start with five active
# children (was 3), 300 steps per child (was 20) and 64 runs per tree
# (was 32): the step cap now sits well above what a real task uses, so a
# long run ends on its time budget rather than on a counter that stops
# healthy work, and a fan-out of 4-7 agents no longer refuses its fourth
# child. Journal scaffolds are created before a task starts, so an install
# accumulated one file per dialog and one per delegated run, most of them
# empty forever; prune_task_state_journals now removes an empty journal
# older than a week and the journal of a finished delegated run whose
# parent journal is gone, while the live dialog, fresh files, journals
# with history and foreign files stay untouched. An empty parent does not
# cascade onto its children in the same pass, so a working child is never
# swept, and the sweep covers both the project folder and the empty-state
# dialog folders once per process.

# 2.17.1 settles two chat affordances that were easy to misuse. The
# economy-mode checkbox is gone: the compact context is the supported way
# to run a dialog, so ECONOMY_MODE_ALWAYS_ON is passed to the agent loop
# unconditionally while the loop itself still honours the flag. The
# historical chat_economy_mode config key stays readable but is no longer
# consulted, so a saved False cannot switch the mode off. The separate
# "Обзор…" button of the folder block is gone too: sitting next to the
# chat-input paperclip it read as "attach a file". The native dialog now
# opens from the last entry of the folder selector (FOLDER_PICKER_BROWSE);
# the entry is an action, not a value, so a fresh widget key after the
# dialog closes makes the selector fall back to the chosen folder whether
# the dialog returned a path or was cancelled.

# 2.18.0 adds the marketplace foundation (core.marketplace): a catalog
# client, an installer for skills with a journal, rollback and recovery of an
# interrupted run, a conflict matrix for a locally edited installation, an
# update path with per-entity refusals and a bounded run journal. The section
# lives behind one master switch (marketplace.enabled): it gates the sidebar
# entry, the one-time seeding of registry rows and the imported defaults, so
# a disabled marketplace leaves no trace in the UI and writes nothing. Every
# start-up path stays offline - the catalog is reached only from a button -
# and the RAG half of the section is not part of this slice.

# 2.19.0 extends the marketplace from skills to every shipped entity type and
# stops seeding the catalog entities from the distribution. An agent installs
# into an orchestrator folder (config merge, instructions, functions), a base
# through its manifest, source and index with an atomic folder replace, and an
# MCP server as a credential-less connection that waits for secrets. A bundle
# previews a recommended set with checkboxes, the update check reports all four
# types in their own sections and addresses an entity as type:id, and the
# installable-type list is read by the showcase instead of a trial call. The
# catalog lives in a separate GitHub repository published by
# scripts/marketplace_publish.py; entities listed as catalog-only are no longer
# imported from defaults while the marketplace is on, and a removal marker
# still wins over that rule.

# 2.19.1 fixes the false "subagents running" reports. A dialog root run stands
# for one step and has no worker that could ever write its final status, so
# release_tree closes it explicitly and a new step reopens the finished record
# instead of inheriting the previous step's status. A status reader judges
# liveness more strictly than the file repair: a stored running claim without a
# live in-memory counterpart is reported as failed/stale_run, the whole store is
# swept for records of previous processes at startup, and the heartbeat timer
# is started only for a background run, because a blocking child keeps its
# record in memory until it returns.

# 2.20.0 adds helpers to the marketplace: an ``agent`` entity of kind
# ``assistant`` installs as an assistant profile (a folder with manifest.json,
# prompt.md and text attachments, plus a row in the assistants table) next to
# the existing employee kind, which installs an orchestrator folder. The kind
# travels with the manifest, the catalog index, the showcase card, the
# registry row and the Updates row, so the showcase labels a card without
# downloading its package; a manifest without the field is read as an
# employee. The publisher refuses an unknown kind, and an installation
# refuses to switch the kind of an installed agent, because the kind decides
# both the folder and the table. Attachments are limited to text files the
# storage layer can hold, and an update keeps the attachments the user added
# while it leaves an edited prompt untouched.

# The marketplace gained helpers in 2.20.0 and, in 2.21.0, the navigation
# that makes them first-class. In 2.21.1 a card keeps its expanded state in
# the session, so its single install button covers both the entity and the
# ticked extras - and actually reaches the installer on the rerun that
# follows a click. In 2.22.0 the bundled Rag Base Creator is installed by
# default again: it is seeded into the folder named after its preset, so one
# skill exists on disk, the catalog adopts that copy instead of offering a
# second one, and a catalog installation of the same skill stops the seed.

# 2.23.0 puts the parent in charge of the children it started. A delegated
# run can be stopped with cancel_subagent: the request is a flag checked at
# step boundaries (a tool call already in flight finishes first), the run and
# its descendants end as cancelled, and only runs of the caller's own tree
# are addressable. read_subagent_thread shows what a child actually saw and
# said, not just what its journal claims. A child now reports its finish
# through a done loop status instead of a question, keeps no plan gate it
# cannot pass, and a run that ends without a summary is reported as
# unfinished work rather than as a result. The blocking wait gained
# until="all_children".

__version__ = "2.23.0"

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
