"""Portable exports describe the shipped foundation, never a second palette."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
FOUNDATION = ROOT / "app/web/static/foundation"


def resolve_token(source: dict[str, Any], path: str, seen: tuple[str, ...] = ()) -> str:
    """Resolve the snapshot's explicit DTCG groups and aliases into CSS values."""
    assert path not in seen, f"Cyclic token alias: {path}"
    token: Any = source
    for key in path.split("."):
        token = token[key]
    value: Any = token["$value"]
    if isinstance(value, str) and value.startswith("{"):
        return resolve_token(source, value[1:-1], (*seen, path))
    kind = token["$type"]
    if kind == "color":
        assert isinstance(value, dict)
        assert value["colorSpace"] == "srgb" and len(value["components"]) == 3
        return "#" + "".join(f"{round(float(channel) * 255):02x}" for channel in value["components"])
    if kind in {"dimension", "duration"}:
        assert isinstance(value, dict)
        return f'{value["value"]}{value["unit"]}'
    if kind == "cubicBezier":
        return "cubic-bezier(" + ",".join(map(str, value)) + ")"
    return str(value)


def flatten_tokens(group, prefix=""):
    for name, token in group.items():
        if name.startswith("$"):
            continue
        path = prefix + name
        if "$value" in token:
            yield path
        else:
            yield from flatten_tokens(token, path + ".")


def test_every_canonical_token_and_alias_matches_the_shipped_css():
    source = json.loads((FOUNDATION / "tokens.json").read_text())
    css = (FOUNDATION / "tokens.css").read_text()
    selectors = {"global": ":root", "light": ':root, [data-bs-theme="light"]',
                 "dark": '[data-bs-theme="dark"]'}
    for group, selector in selectors.items():
        rule = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", css)
        assert rule is not None
        actual = dict(re.findall(r"--([\w-]+):\s*([^;]+);", rule[1]))
        expected = {path.replace(".", "-"): resolve_token(source, f"{group}.{path}")
                    for path in flatten_tokens(source[group])}
        assert actual == expected, group


def test_core_palette_is_identical_in_spec_exports_and_runtime():
    spec = (ROOT / "DESIGN.md").read_text().split("---", 2)[1]
    colors = dict(re.findall(r'^  ([\w-]+): "(#[0-9a-fA-F]{6})"$', spec, re.M))
    exported = {key: value for key, value in json.loads((ROOT / "tokens.json").read_text())["color"].items()
                if not key.startswith("$")}
    tailwind = json.loads((ROOT / "tailwind.theme.json").read_text())["theme"]["extend"]["colors"]
    assert set(colors) == set(exported) == set(tailwind)
    assert {key: value["$value"]["hex"].lower() for key, value in exported.items()} == colors
    assert tailwind == colors
    source = json.loads((FOUNDATION / "tokens.json").read_text())
    roles = {"canvas": "neutral", "surface": "panel", "hover": "inset", "ink": "text",
             "muted": "muted", "line": "border", "control-line": "control-line",
             "finn": "primary", "nancy": "secondary", "danger": "danger",
             "on-finn": "on-finn", "focus": "focus"}
    light_names = {"neutral": "canvas", "primary": "facts", "secondary": "interpretation"}
    for theme in ("light", "dark"):
        for runtime, portable in roles.items():
            name = "light-" + light_names.get(portable, portable) if theme == "light" else portable
            assert colors[name] == resolve_token(source, f"{theme}.{runtime}"), (theme, runtime)


@pytest.mark.parametrize("name,runtime", [
    ("heading", "title"), ("section", "heading"), ("body-md", "body"), ("label", "small"),
])
def test_portable_type_exports_use_actual_foundation_sizes_and_font(name, runtime):
    source = json.loads((FOUNDATION / "tokens.json").read_text())
    exported = json.loads((ROOT / "tokens.json").read_text())["typography"][name]["$value"]
    tailwind = json.loads((ROOT / "tailwind.theme.json").read_text())["theme"]["extend"]
    size = exported["fontSize"]
    assert f'{size["value"]}{size["unit"]}' == resolve_token(source, f"global.{runtime}")
    assert tailwind["fontSize"][name][0] == resolve_token(source, f"global.{runtime}")
    assert exported["fontFamily"] == resolve_token(source, "global.font")
    assert tailwind["fontFamily"][name] == [resolve_token(source, "global.font")]
    spec = (ROOT / "DESIGN.md").read_text().split("---", 2)[1]
    match = re.search(r"^  " + name + r":\n((?:    .+\n)+)", spec, re.M)
    assert match is not None
    section = match[1]
    assert f"fontSize: {resolve_token(source, f'global.{runtime}')}" in section
    # CLI 0.4 exports type sizes/weights but omits lineHeight. Keep that contract
    # in the source spec and deployed adapter, not a hand-modified JSON shape.
    if name == "body-md":
        assert f"lineHeight: {resolve_token(source, 'global.line-height')}" in section
        assert "line-height:var(--line-height)" in (FOUNDATION / "components.css").read_text()


def test_portable_spacing_and_shape_exports_match_canonical_dimensions():
    source = json.loads((FOUNDATION / "tokens.json").read_text())
    exported = json.loads((ROOT / "tokens.json").read_text())
    tailwind = json.loads((ROOT / "tailwind.theme.json").read_text())["theme"]["extend"]
    for name, step in {"xs": "1", "sm": "2", "md": "3", "lg": "4", "xl": "6", "xxl": "8", "xxxl": "12"}.items():
        dimension = exported["spacing"][name]["$value"]
        assert f'{dimension["value"]}{dimension["unit"]}' == resolve_token(source, "global.space." + step)
        assert tailwind["spacing"][name] == resolve_token(source, "global.space." + step)
    for name in ("sm", "md"):
        dimension = exported["rounded"][name]["$value"]
        assert f'{dimension["value"]}{dimension["unit"]}' == resolve_token(source, "global.radius")
        assert tailwind["borderRadius"][name] == resolve_token(source, "global.radius")
    spec = (ROOT / "DESIGN.md").read_text().split("---", 2)[1]
    assert "height: " + resolve_token(source, "global.control") in spec


def test_bootstrap_and_canonical_snapshot_match_their_pinned_provenance():
    provenance = json.loads((FOUNDATION / "SOURCE.json").read_text())
    assert provenance["bootstrap"] == "5.3.8"
    assert "Bootstrap  v5.3.8" in (FOUNDATION / "vendor/bootstrap.min.css").read_text()[:300]
    for path, expected in provenance["sha256"].items():
        assert hashlib.sha256((FOUNDATION / path).read_bytes()).hexdigest() == expected, path
