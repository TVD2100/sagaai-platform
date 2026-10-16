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

__version__ = "2.1.4"

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
