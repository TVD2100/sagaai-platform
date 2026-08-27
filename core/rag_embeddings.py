"""
core.rag_embeddings - provider embedding client for RAG indexing/search.

Uses the standard Yandex Foundation Models Embedding API:

  POST https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding
  {
    "modelUri": "emb://<folder_id>/text-search-doc",
    "text": "..."
  }

The legacy alias ``embeddings`` is mapped to ``text-search-doc`` (document
model) for backward compatibility. Query-side embedding uses
``text-search-query`` via :func:`embed_query`. Text-search models return a
fixed 256-dimension vector and reject a ``dim`` parameter, so it is omitted
for them.

Credentials are sourced from the platform config (BYOK): the IAM token and
Folder ID configured for the YandexAI service. All embeddings are generated
remotely but stored locally (core.rag_index); nothing is uploaded to any
cloud index.

No streamlit imports.
"""
import requests

from core.api_errors import ApiKeyMissingError, ProviderHTTPError, NetworkError, RequestTimeoutError
from core.config import load_config
from core.services import get_services


# Embeddings endpoint (official Yandex AI Studio REST API).
EMBEDDINGS_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding"

MODEL_TIMEOUT = 60  # seconds; embeddings requests are small but can be slow on cold start

# Yandex text-search embedding model ids. Documents and queries use separate
# models; the legacy alias "embeddings" maps to the document model so existing
# bases keep working unchanged.
DOC_EMBEDDING_MODEL = "text-search-doc"
QUERY_EMBEDDING_MODEL = "text-search-query"
_TEXT_SEARCH_PREFIX = "text-search-"


def get_yandex_embedding_credentials() -> tuple:
    """Return (api_key, folder_id) for the YandexAI service from config.

    Raises ApiKeyMissingError when either credential is absent.
    """
    svcs = get_services()
    svc = svcs.get("YandexAI", {})
    cfg = load_config()
    api_key = cfg.get(svc.get("config_key", "YANDEX_API_KEY"), "")
    if isinstance(api_key, str):
        api_key = api_key.strip()
    if not api_key:
        raise ApiKeyMissingError("YandexAI", field="IAM token")
    folder_id = cfg.get(svc.get("config_key2", "YANDEX_FOLDER_ID"), "")
    if isinstance(folder_id, str):
        folder_id = folder_id.strip()
    if not folder_id:
        raise ApiKeyMissingError("YandexAI", field="Folder ID")
    return api_key, folder_id


def embed_text(text: str, model: str = DOC_EMBEDDING_MODEL, dimension: int = 256,
               api_key: str = None, folder_id: str = None) -> list:
    """Embed a single text with the Yandex embeddings API.

    Returns the embedding vector as a list of floats. Raises:
    ApiKeyMissingError, ProviderHTTPError, RequestTimeoutError, NetworkError.
    """
    if not text or not str(text).strip():
        return []
    if not api_key or not folder_id:
        api_key, folder_id = get_yandex_embedding_credentials()
    if not model:
        model = DOC_EMBEDDING_MODEL
    if model == "embeddings":
        model = DOC_EMBEDDING_MODEL
    model_uri = f"emb://{folder_id}/{model}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "x-folder-id": folder_id,
        "Content-Type": "application/json",
    }
    payload = {
        "modelUri": model_uri,
        "text": str(text),
    }
    # Text-search models return a fixed dimension and reject a "dim" field.
    if dimension and not model.startswith(_TEXT_SEARCH_PREFIX):
        payload["dim"] = int(dimension)
    try:
        r = requests.post(
            EMBEDDINGS_URL, headers=headers, json=payload,
            timeout=MODEL_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        raise RequestTimeoutError(service="YandexAI")
    except requests.exceptions.RequestException as e:
        raise NetworkError(str(e), service="YandexAI")
    if r.status_code != 200:
        try:
            err_json = r.json()
            body = (
                err_json.get("error", {}).get("message")
                or err_json.get("message")
                or r.text[:300]
            )
        except Exception:
            body = r.text[:300]
        raise ProviderHTTPError(r.status_code, body, service="YandexAI")
    data = r.json()
    embedding = data.get("embedding") or []
    return [float(v) for v in embedding]


def embed_query(text: str, api_key: str = None, folder_id: str = None) -> list:
    """Embed a search query with the query-side text-search model.

    Queries must use ``text-search-query`` because the document model is
    trained for full documents; mixing them degrades retrieval quality.
    Returns the embedding vector as a list of floats.
    """
    return embed_text(
        text, model=QUERY_EMBEDDING_MODEL, dimension=None,
        api_key=api_key, folder_id=folder_id,
    )


def embed_many(texts: list, model: str = DOC_EMBEDDING_MODEL, dimension: int = 256,
               api_key: str = None, folder_id: str = None,
               progress_callback=None) -> list:
    """Embed a list of texts sequentially (10 rps quota allows this locally).

    Returns a list of embedding vectors aligned with input indexes. Skips
    empty texts with an empty vector. Calls ``progress_callback(done, total)``
    after each successful embedding when provided.
    """
    if not api_key or not folder_id:
        api_key, folder_id = get_yandex_embedding_credentials()
    result: list = []
    total = len(texts)
    for i, text in enumerate(texts):
        if not text or not str(text).strip():
            result.append([])
            continue
        try:
            vec = embed_text(
                text, model=model, dimension=dimension,
                api_key=api_key, folder_id=folder_id,
            )
        except Exception:
            raise
        result.append(vec)
        if progress_callback is not None:
            try:
                progress_callback(i + 1, total)
            except Exception:
                pass
    return result
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
