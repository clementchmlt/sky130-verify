"""Local badge rendering: badge.json (shields.io endpoint schema),
badge.svg (flat style, no network call), and a Markdown snippet."""

from __future__ import annotations

from dataclasses import dataclass
import json

_LABEL = "sky130-verify"

# Approximate glyph widths (px, ~11px), good enough for short ASCII labels.
_AVG = 7.0
_WIDTHS = {c: 6.0 for c in "iIl.:,'"} | {c: 9.5 for c in "mMW"}


def _text_width(text: str) -> float:
    return sum(_WIDTHS.get(c, _AVG) for c in text)


@dataclass(frozen=True)
class BadgeColor:
    hex: str
    name: str


_GREEN = BadgeColor("#4c1", "brightgreen")
_RED = BadgeColor("#e05d44", "red")
_YELLOW = BadgeColor("#dfb317", "yellow")
_GREY = BadgeColor("#9f9f9f", "lightgrey")


def color_for_verdicts(drc_verdict: str, lvs_verdict: str) -> BadgeColor:
    verdicts = {drc_verdict, lvs_verdict}
    if verdicts == {"pass"}:
        return _GREEN
    if "fail" in verdicts:
        return _RED
    if "error" in verdicts:
        return _RED
    if verdicts & {"unknown", "not_run"}:
        return _YELLOW
    return _GREY


def message_for_verdicts(drc_verdict: str, lvs_verdict: str) -> str:
    return f"drc {drc_verdict} · lvs {lvs_verdict}"


def endpoint_json(cell_id: str, drc_verdict: str, lvs_verdict: str,
                   manifest_sha256: str | None = None) -> dict:
    color = color_for_verdicts(drc_verdict, lvs_verdict)
    data = {
        "schemaVersion": 1,
        "label": _LABEL,
        "message": message_for_verdicts(drc_verdict, lvs_verdict),
        "color": color.name,
    }
    if manifest_sha256 is not None:
        # Ties the badge to the exact manifest it derives from.
        data["manifest_sha256"] = manifest_sha256
    return data


def render_svg(label: str, message: str, color_hex: str) -> str:
    """Flat two-segment badge in the shields.io visual style."""
    pad = 10
    label_w = round(_text_width(label) + 2 * pad)
    message_w = round(_text_width(message) + 2 * pad)
    total_w = label_w + message_w
    height = 20

    def _escape_text(content: str) -> str:
        return content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _escape_attr(content: str) -> str:
        return _escape_text(content).replace('"', "&quot;")

    def _text(x: float, content: str) -> str:
        escaped = _escape_text(content)
        return (
            f'<text x="{x}" y="14" fill="#010101" fill-opacity=".3">{escaped}</text>'
            f'<text x="{x}" y="13">{escaped}</text>'
        )

    label_cx = label_w / 2
    message_cx = label_w + message_w / 2
    aria_label = _escape_attr(f"{label}: {message}")

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{height}" role="img" aria-label="{aria_label}">
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r">
    <rect width="{total_w}" height="{height}" rx="3" fill="#fff"/>
  </clipPath>
  <g clip-path="url(#r)">
    <rect width="{label_w}" height="{height}" fill="#555"/>
    <rect x="{label_w}" width="{message_w}" height="{height}" fill="{color_hex}"/>
    <rect width="{total_w}" height="{height}" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">
    {_text(label_cx, label)}
    {_text(message_cx, message)}
  </g>
</svg>
'''


def snippet_markdown(cell_id: str, badge_json_path: str, badge_svg_path: str) -> str:
    return (
        f"![sky130-verify: {cell_id}]({badge_svg_path})\n\n"
        f"Dynamic badge (shields.io endpoint): "
        f"`https://img.shields.io/endpoint?url=<raw-url-to>/{badge_json_path}`\n"
    )


def render_all(cell_id: str, drc_verdict: str, lvs_verdict: str,
                manifest_sha256: str | None = None) -> dict[str, str]:
    """Returns the three artifacts' content without writing to disk."""
    data = endpoint_json(cell_id, drc_verdict, lvs_verdict, manifest_sha256)
    color = color_for_verdicts(drc_verdict, lvs_verdict)
    svg = render_svg(data["label"], data["message"], color.hex)
    md = snippet_markdown(cell_id, "badge.json", "badge.svg")
    return {
        "badge.json": json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        "badge.svg": svg,
        "snippet.md": md,
    }
