"""Minimal plain text <-> Atlassian Document Format (ADF) conversion.

We use Jira REST v3 (ADF) instead of v2 wiki markup so that record text round-trips
verbatim: wiki markup would reinterpret characters such as `|`, `[`, `{` and `*`
that appear in the source records.
"""
from __future__ import annotations

from typing import Any


def text_to_adf(text: str) -> dict[str, Any]:
    """One paragraph per line; empty lines become empty paragraphs."""
    content = []
    for line in text.split("\n"):
        para: dict[str, Any] = {"type": "paragraph", "content": []}
        if line:
            para["content"].append({"type": "text", "text": line})
        content.append(para)
    return {"type": "doc", "version": 1, "content": content}


def adf_to_text(node: Any) -> str:
    """Flatten ADF back to plain text (paragraph/heading/list item -> line)."""
    if node is None:
        return ""
    if isinstance(node, str):  # already plain text (e.g. v2 payloads in tests)
        return node
    lines: list[str] = []
    _walk(node, lines)
    return "\n".join(lines)


_BLOCKS = {"paragraph", "heading", "codeBlock", "blockquote", "listItem", "tableCell", "tableHeader"}


def _inline(node: dict[str, Any]) -> str:
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t in ("mention", "emoji", "inlineCard", "date", "status"):
        attrs = node.get("attrs", {})
        return str(attrs.get("text") or attrs.get("url") or attrs.get("shortName") or "")
    return "".join(_inline(c) for c in node.get("content", []) or [])


def _walk(node: dict[str, Any], lines: list[str]) -> None:
    t = node.get("type")
    if t in ("paragraph", "heading", "codeBlock"):
        lines.append(_inline(node))
        return
    for child in node.get("content", []) or []:
        if isinstance(child, dict):
            _walk(child, lines)
