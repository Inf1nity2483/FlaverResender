from __future__ import annotations

from typing import Any

LinkReplaceRules = tuple[tuple[tuple[str, ...], str], ...]


def replace_url_string(url: str, prefixes: tuple[str, ...], new_url: str) -> str:
    for prefix in sorted(prefixes, key=len, reverse=True):
        if url.startswith(prefix):
            return new_url + url[len(prefix) :]
    return url


def replace_in_html(html: str, rules: LinkReplaceRules) -> str:
    out = html
    for prefixes, new_url in rules:
        for prefix in sorted(prefixes, key=len, reverse=True):
            out = out.replace(prefix, new_url)
    return out


def telegram_inline_keyboard_to_max(
    reply_markup: Any, rules: LinkReplaceRules
) -> list[dict[str, Any]] | None:
    if reply_markup is None:
        return None
    rows = getattr(reply_markup, "inline_keyboard", None)
    if not rows:
        return None

    buttons_rows: list[list[dict[str, Any]]] = []
    for row in rows:
        btns: list[dict[str, Any]] = []
        for btn in row:
            url = getattr(btn, "url", None)
            text = getattr(btn, "text", None)
            if not url:
                continue
            out_url = url
            for prefixes, new_url in rules:
                out_url = replace_url_string(out_url, prefixes, new_url)
            btns.append({"type": "link", "text": (text or "...")[:200], "url": out_url})
        if btns:
            buttons_rows.append(btns)

    if not buttons_rows:
        return None
    return [{"type": "inline_keyboard", "payload": {"buttons": buttons_rows}}]
