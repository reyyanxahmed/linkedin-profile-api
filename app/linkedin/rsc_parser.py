"""Parser for LinkedIn's flagship-web RSC (React Server Components) wire format.

Single responsibility: decode the base64-encoded RSC stream, parse the line-based
wire protocol, and extract a flat list of text strings in document order. The
caller (the flagship-web strategy) then walks the text list to find profile data
by pattern-matching against known section structures.

RSC wire format:
  Each line is: <id_hex>:<type_char>,<json_payload>
  Type chars:
    I = component reference import (ignore for text extraction)
    T = text/blob data (ignore for text extraction)
    [ = array — the actual component tree data
    Other = various hints (ignore)

The component trees are arrays like:
  ["$","div",null,{"children":[ ["$","p",null,{"children":["Analyst"]}], ... ]}]
  ["$","$L4",null,{"componentKey":"...","children":[ ["$","section",null,{...}] ]}]

Text content lives in the `children` property of HTML-like elements, as string
values within arrays. We walk the tree depth-first and collect strings > 1 char
that are not CSS class names, style vars, component IDs, or tracking data.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

# Keys that hold CSS/tracking metadata, not human-readable content.
_SKIP_KEYS = frozenset({
    "className", "style", "viewTrackingSpecs", "trackingScope", "componentKey",
    "componentId", "sduiid", "parentSpanId", "observabilityIdentifier",
    "contentTrackingId", "bindableModifiers", "visibilityTriggers", "triggers",
    "action", "actions", "modelStates", "key", "$case", "$type", "expression",
    "cssVarName", "transition", "isReactive", "impressionThresholds",
    "viewName", "transporterKeys", "delegateComponentKey",
})
# Keys whose values are human-readable content.
_CONTENT_KEYS = frozenset({
    "children", "text", "label", "title", "alt", "name",
    "header", "subtitle", "value", "description", "href",
})
# Single-char/noise strings to skip.
_NOISE_STRINGS = frozenset({
    "div", "span", "p", "ul", "li", "section", "h2", "h1", "h3", "img", "a",
    "button", "svg", "path", "circle", "rect", "header", "footer", "main",
    "nav", "article", "aside", "figure", "figcaption", "table", "tr", "td",
    "th", "thead", "tbody", "tfoot", "label", "input", "form", "br", "hr",
    "strong", "em", "small", "large", "xlarge", "xxlarge", "medium", "tiny",
    "bold", "normal", "italic", "underline", "none", "block", "inline",
    "inlineBlock", "flex", "grid", "absolute", "relative", "fixed", "sticky",
    "start", "center", "end", "stretch", "wrap", "noWrap", "wrapReverse",
    "horizontal", "vertical", "all", "both", "x", "y", "top", "bottom",
    "left", "right", "middle", "fill", "contain", "cover", "fit", "auto",
    "inherit", "initial", "unset", "solid", "dashed", "dotted", "hidden",
    "visible", "scroll", "clip", "ellipsis", "truncate", "show", "hide",
    "true", "false", "null", "undefined", "default", "sans", "serif",
    "monospace", "open", "tight", "loose", "rounded", "square",
    "fillAvailable", "ltr", "rtl", "menuitem", "list", "listitem", "box",
    "fitContent", "primary", "secondary", "menu", "isolate", "light", "BR",
    "profile", "metadata", "title", "meta", "viewport", "width",
})

# Regex for strings that are clearly not human content.
_NOT_CONTENT_RE = re.compile(
    r"^(_{1,2}|--|\$|com\.|proto\.|urn:li:|var\(|#[0-9A-Fa-f]{6}|"
    r"^[a-f0-9-]{20,}$|^[0-9]+$|^[a-z][a-zA-Z0-9]*$)"
)


def decode_rsc(body: str) -> str:
    """Decode a base64-encoded RSC response body to text.

    If the body is not base64 (already text), return it as-is.
    """
    try:
        decoded = base64.b64decode(body)
        return decoded.decode("utf-8", errors="replace")
    except Exception:
        return body


def parse_rsc_lines(decoded: str) -> list[dict | list | str]:
    """Parse the RSC wire format into a list of JSON payloads.

    Each line is <id>:<type>,<json>. Only lines with type '[' (array/component tree)
    are interesting; we parse and collect those. Lines starting with T are text
    blobs, I are imports — both skipped for component-tree walking.
    """
    out: list[Any] = []
    for line in decoded.split("\n"):
        if ":" not in line:
            continue
        parts = line.split(":", 1)
        if len(parts) < 2:
            continue
        rest = parts[1]
        if not rest:
            continue
        # The format is: type_char,json_str
        # type_char is one character, followed by a comma, then the JSON
        if len(rest) >= 2 and rest[1] == ",":
            type_char = rest[0]
            json_str = rest[2:]
        else:
            # Some lines may start the JSON directly
            type_char = " "
            json_str = rest
        # Only parse component-tree arrays (type '[' or type '0' for root)
        if type_char not in ("[", "0", " "):
            continue
        try:
            data = json.loads(json_str)
            out.append(data)
        except json.JSONDecodeError:
            continue
    return out


def extract_text(decoded: str) -> list[str]:
    """Extract all human-readable text strings from an RSC stream, in order.

    This is the main entry point for the strategy. It decodes, parses, and walks
    every component tree, collecting strings that look like actual content (not
    CSS, tracking, or structural noise).
    """
    if not decoded:
        return []
    # If the input looks like base64, decode first.
    if not decoded.startswith("[") and not decoded.startswith("<"):
        decoded = decode_rsc(decoded)
    payloads = parse_rsc_lines(decoded)
    texts: list[str] = []
    seen: set[str] = set()
    for payload in payloads:
        _walk(payload, texts, seen)
    return texts


def _walk(obj: Any, out: list[str], seen: set[str]) -> None:
    """Depth-first walk of a component tree, collecting content strings."""
    if isinstance(obj, str):
        if _is_content(obj) and obj not in seen:
            out.append(obj)
            seen.add(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in _CONTENT_KEYS:
                _walk(v, out, seen)
            elif k not in _SKIP_KEYS:
                _walk(v, out, seen)
    elif isinstance(obj, list):
        for el in obj:
            _walk(el, out, seen)


def _is_content(s: str) -> bool:
    """Return True if the string looks like human-readable content, not metadata."""
    if not s or len(s) < 2:
        return False
    if s in _NOISE_STRINGS:
        return False
    if _NOT_CONTENT_RE.match(s):
        return False
    if s.startswith("$"):
        return False
    return True