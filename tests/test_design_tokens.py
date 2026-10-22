"""Core design exports stay aligned with the shipped presentation tokens."""
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[1]


def test_core_palette_is_identical_in_spec_exports_and_runtime():
    spec = (ROOT / "DESIGN.md").read_text().split("---", 2)[1]
    colors = dict(re.findall(r'^  ([\w-]+): "(#[0-9a-fA-F]{6})"$', spec, re.M))
    exported = {key: value for key, value in json.loads((ROOT / "tokens.json").read_text())["color"].items()
                if not key.startswith("$")}
    tailwind = json.loads((ROOT / "tailwind.theme.json").read_text())["theme"]["extend"]["colors"]
    assert set(colors) == set(exported) == set(tailwind)
    assert {key: value["$value"]["hex"].lower() for key, value in exported.items()} == colors
    assert tailwind == colors
    css = (ROOT / "app/web/static/app.css").read_text()
    roles = {"canvas": "--bg", "text": "--text", "muted": "--muted", "panel": "--panel",
             "inset": "--panel-2", "border": "--line", "facts": "--finn", "interpretation": "--nancy"}
    dark_names = {"canvas": "neutral", "facts": "primary", "interpretation": "secondary"}
    for theme in ("light", "dark"):
        rule = re.search(r':root\[data-theme="' + theme + r'"\]\s*\{([^}]+)\}', css)
        assert rule is not None
        tokens = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", rule[1]))
        for name, variable in roles.items():
            key = "light-" + name if theme == "light" else dark_names.get(name, name)
            assert colors[key] == tokens[variable], (theme, key, colors[key], tokens[variable])
