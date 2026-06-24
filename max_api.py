from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger("resender.v2.max")

MAX_BASE = "https://platform-api.max.ru"


def _find_token_in_payload(data: Any) -> str | None:
    if isinstance(data, dict):
        direct = data.get("token")
        if isinstance(direct, str) and direct:
            return direct

        payload = data.get("payload")
        if isinstance(payload, dict):
            nested = payload.get("token")
            if isinstance(nested, str) and nested:
                return nested

        photos = data.get("photos")
        if isinstance(photos, dict):
            for entry in photos.values():
                token = _find_token_in_payload(entry)
                if token:
                    return token
        elif isinstance(photos, list):
            for entry in photos:
                token = _find_token_in_payload(entry)
                if token:
                    return token

        for value in data.values():
            token = _find_token_in_payload(value)
            if token:
                return token

    if isinstance(data, list):
        for item in data:
            token = _find_token_in_payload(item)
            if token:
                return token

    return None


class MaxClient:
    """MAX client goes direct (no system proxy)."""

    def __init__(self, token: str, chat_id: int) -> None:
        self._token = token
        self._chat_id = chat_id
        self._api = httpx.AsyncClient(
            base_url=MAX_BASE,
            headers={"Authorization": token},
            timeout=httpx.Timeout(120.0, connect=30.0),
            trust_env=False,
        )
        self._upload = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=30.0),
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._upload.aclose()

    async def send_message(
        self,
        text: str | None,
        *,
        attachments: list[dict[str, Any]] | None = None,
        notify: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"notify": notify}
        if text:
            payload["text"] = text
            payload["format"] = "html"
        if attachments:
            payload["attachments"] = attachments

        last_exc: Exception | None = None
        for attempt in range(6):
            resp = await self._api.post(
                "/messages",
                params={"chat_id": self._chat_id},
                json=payload,
            )
            if resp.status_code == 200:
                return resp.json()
            body_txt = resp.text
            if resp.status_code >= 400 and "attachment.not.ready" in body_txt:
                delay = 0.6 * (2**attempt)
                logger.warning("MAX attachment not ready, retry in %.1fs", delay)
                await asyncio.sleep(delay)
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                logger.error("MAX send failed: %s %s", resp.status_code, body_txt[:400])
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("MAX send failed after retries")

    async def upload_bytes(self, data: bytes, *, filename: str, upload_type: str) -> dict[str, Any]:
        meta_resp = await self._api.post("/uploads", params={"type": upload_type})
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        upload_url = meta.get("url")
        if not upload_url:
            raise RuntimeError(f"MAX /uploads missing url: {meta}")

        files = {"data": (filename, data, "application/octet-stream")}
        cdn_resp = await self._upload.post(
            upload_url,
            headers={"Authorization": self._token},
            files=files,
        )
        cdn_resp.raise_for_status()
        try:
            return cdn_resp.json()
        except ValueError:
            body = cdn_resp.text.strip()
            normalized = body.replace(" ", "").lower()
            if "<retval>1</retval>" in normalized:
                token = _find_token_in_payload(meta)
                if not token:
                    query = parse_qs(urlparse(upload_url).query)
                    for key in ("token", "photoIds"):
                        values = query.get(key, [])
                        if values and isinstance(values[0], str) and values[0]:
                            token = values[0]
                            break

                logger.warning(
                    "MAX CDN returned XML success for %s upload; fallback token found=%s",
                    upload_type,
                    bool(token),
                )
                out: dict[str, Any] = {"meta": meta, "retval": "1"}
                if token:
                    out["token"] = token
                return out

            raise RuntimeError(f"MAX CDN returned non-JSON: {body[:300]}")


def extract_attachment_token(upload_json: dict[str, Any]) -> str:
    token = _find_token_in_payload(upload_json)
    if token:
        return token
    raise RuntimeError(f"Unexpected MAX upload response (no token): {upload_json!r}")
