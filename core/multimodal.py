"""
core.multimodal - image analysis and image generation for platform tools.

Bridges the optional models assigned in the DevAgent configuration:

* ``vision_service`` / ``vision_model`` - image analysis (vision);
* ``image_service`` / ``image_model`` - image generation.

The assigned pair is validated against the service catalogs
(``vision_models`` / ``image_models`` blocks in the service profiles) and
the provider calls are routed through :mod:`core.api_layer`.

Assignment and input problems raise :class:`MultimodalError` with a
machine-readable ``code`` and an actionable message; provider failures
keep the :class:`core.api_errors.APIError` contract.

No streamlit imports.
"""
import base64
import os

from core.api_errors import APIError, api_error_message
from core.api_layer import send_image_generation_request, send_vision_request
from core.config import load_devagent_config
from core.services import get_image_models, get_services, get_vision_models


# Multimodal kinds accepted by resolve_model.
VISION = "vision"
IMAGE = "image"

# Guardrails applied before an image is sent to a provider.
MAX_IMAGE_BYTES = 10 * 1024 * 1024   # per image, raw bytes
MAX_IMAGES_PER_REQUEST = 5

_VISION_LABEL = "image analysis"
_IMAGE_LABEL = "image generation"
_SETTINGS_SECTION = {
    VISION: "«Модель для распознавания изображений»",
    IMAGE: "«Модель для генерации изображений»",
}


class MultimodalError(Exception):
    """Assignment or input validation failure for a multimodal call.

    Attributes:
        code: machine-readable identifier (not_assigned, service_not_found,
            catalog_empty, model_not_declared, no_images, too_many_images,
            empty_prompt, image_not_found, image_empty, image_too_large,
            unsupported_format).
        service / model: the resolved pair when known (may be empty).
    """

    def __init__(self, code: str, message: str, *,
                 service: str = "", model: str = "") -> None:
        self.code = code
        self.service = service or ""
        self.model = model or ""
        super().__init__(message)


def _label(kind: str) -> str:
    """Return the human-readable label for *kind*."""
    return _VISION_LABEL if kind == VISION else _IMAGE_LABEL


def _not_assigned_message(kind: str) -> str:
    """Return the setup hint shown when no model is assigned for *kind*."""
    return (
        f"No {_label(kind)} model is assigned. "
        f"Go to Settings → DevAgent → {_SETTINGS_SECTION[kind]} "
        f"and choose a service and model that support {_label(kind)}."
    )


def resolve_model(kind: str, *, config: dict = None) -> tuple:
    """Return the validated (service, model) pair assigned for *kind*.

    *kind* is :data:`VISION` or :data:`IMAGE`; *config* overrides the
    stored DevAgent configuration (tests). Raises MultimodalError with
    codes: not_assigned, service_not_found, catalog_empty,
    model_not_declared.
    """
    if kind not in (VISION, IMAGE):
        raise ValueError(f"unknown multimodal kind: {kind!r}")
    cfg = config if config is not None else (load_devagent_config() or {})
    service = str(cfg.get("vision_service" if kind == VISION else "image_service") or "").strip()
    model = str(cfg.get("vision_model" if kind == VISION else "image_model") or "").strip()
    if not service or not model:
        raise MultimodalError("not_assigned", _not_assigned_message(kind),
                              service=service, model=model)

    svc = get_services().get(service)
    if not svc:
        raise MultimodalError(
            "service_not_found",
            f"Service '{service}' is not available - "
            f"check the service connection in Settings.",
            service=service, model=model)

    label = _label(kind)
    catalog = get_vision_models(svc) if kind == VISION else get_image_models(svc)
    if not catalog:
        raise MultimodalError(
            "catalog_empty",
            f"Service '{service}' does not offer {label} models. "
            f"Choose a different service in Settings → DevAgent.",
            service=service, model=model)

    ids = [str(entry.get("id") or "").strip() for entry in catalog]
    if model not in ids:
        raise MultimodalError(
            "model_not_declared",
            f"Model '{model}' is not supported by service '{service}' "
            f"for {label}. Pick a supported model in Settings → DevAgent.",
            service=service, model=model)
    return service, model


def detect_mime(data: bytes) -> str:
    """Return the MIME type of *data* (JPEG/PNG/WebP) or "" when unknown."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def load_image(path: str) -> dict:
    """Read one image file for a vision request.

    Returns {"mime", "data", "path", "size"} where *data* is base64.
    Raises MultimodalError with codes image_not_found, image_empty,
    image_too_large or unsupported_format.
    """
    p = str(path or "").strip()
    if not p or not os.path.isfile(p):
        raise MultimodalError("image_not_found", f"Image file not found: {p or path}")
    size = os.path.getsize(p)
    if size <= 0:
        raise MultimodalError("image_empty", f"Image file is empty: {p}")
    if size > MAX_IMAGE_BYTES:
        raise MultimodalError(
            "image_too_large",
            f"Image {os.path.basename(p)} is {size} bytes; "
            f"the limit is {MAX_IMAGE_BYTES} bytes.")
    with open(p, "rb") as f:
        raw = f.read()
    mime = detect_mime(raw)
    if not mime:
        raise MultimodalError(
            "unsupported_format",
            f"Unsupported image format: {os.path.basename(p)} "
            f"(supported: JPEG, PNG, WebP).")
    return {
        "mime": mime,
        "data": base64.b64encode(raw).decode("ascii"),
        "path": p,
        "size": size,
    }


def analyze_image(images: list, prompt: str, *,
                  max_tokens: int = None, temperature: float = None,
                  config: dict = None) -> dict:
    """Analyze images with the assigned vision model.

    *images* - file paths (JPEG/PNG/WebP); *prompt* - the analysis task.
    Returns {"ok": True, "service", "model", "text", "images"}.
    Raises MultimodalError for assignment/input problems and APIError
    subclasses for provider failures.
    """
    paths = [str(p).strip() for p in (images or []) if str(p or "").strip()]
    if not paths:
        raise MultimodalError("no_images",
                              "Image analysis needs at least one image file path.")
    if len(paths) > MAX_IMAGES_PER_REQUEST:
        raise MultimodalError(
            "too_many_images",
            f"Too many images for one request: {len(paths)} "
            f"(at most {MAX_IMAGES_PER_REQUEST}).")

    service, model = resolve_model(VISION, config=config)
    loaded = [load_image(p) for p in paths]
    text = send_vision_request(service, model, str(prompt or ""), loaded,
                               temperature=temperature, max_tokens=max_tokens)
    return {"ok": True, "service": service, "model": model,
            "text": text, "images": len(loaded)}


def generate_image(prompt: str, *, config: dict = None) -> dict:
    """Generate an image from a text prompt with the assigned image model.

    Returns {"ok": True, "service", "model", "mime", "data"} where *data*
    is the base64-encoded image. Raises MultimodalError for assignment or
    input problems and APIError subclasses for provider failures.
    """
    text = str(prompt or "").strip()
    if not text:
        raise MultimodalError("empty_prompt",
                              "Image generation needs a non-empty text prompt.")
    service, model = resolve_model(IMAGE, config=config)
    result = send_image_generation_request(service, model, text)
    return {"ok": True, "service": service, "model": model,
            "mime": str(result.get("mime") or "image/jpeg"),
            "data": str(result.get("data") or "")}


def format_api_error(exc: Exception) -> str:
    """Render a multimodal/API failure as one clear message (never raises)."""
    try:
        if isinstance(exc, MultimodalError):
            return str(exc)
        if isinstance(exc, APIError):
            message = api_error_message(exc)
            detail = getattr(exc, "detail", "") or ""
            if detail and detail not in message:
                message = f"{message} ({detail})"
            return message
        return str(exc) or exc.__class__.__name__
    except Exception:
        return str(exc) or "multimodal error"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
