import ast
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _plugin_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "plugin_version" for target in node.targets):
            value = ast.literal_eval(node.value)
            assert isinstance(value, str)
            return value
    raise AssertionError(f"未找到 plugin_version：{path}")


def _call_names(path: Path, method_name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == method_name:
            calls.append(node)
    return calls


def test_v3_registry_and_source_versions_are_aligned():
    package_v2 = json.loads((ROOT / "package.v2.json").read_text(encoding="utf-8"))
    package_v3 = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
    expected = {
        "TraktSync": ("1.0.0", ROOT / "plugins.v3" / "traktsync"),
        "mediamsgwithdeletemsg": (
            "2.0.0",
            ROOT / "plugins.v3" / "mediamsgwithdeletemsg",
        ),
    }

    assert set(package_v3) == set(expected)
    for plugin_id, (version, source_dir) in expected.items():
        metadata = package_v3[plugin_id]
        assert metadata["version"] == version
        assert metadata["system_version"] == ">=3.0.0"
        assert metadata["history"]
        assert _plugin_version(source_dir / "__init__.py") == version
        assert package_v2[plugin_id]["v3"] is False


def test_v3_plugins_do_not_call_legacy_generic_media_contracts():
    v3_sources = [
        ROOT / "plugins.v3" / "traktsync" / "__init__.py",
        ROOT / "plugins.v3" / "mediamsgwithdeletemsg" / "__init__.py",
    ]
    for source in v3_sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        assert not any(module.startswith("app.db.") for module in imports)
        assert "app.sdk.plugin" in imports
        assert "app.sdk.plugins" not in imports
        for call in _call_names(source, "recognize_media"):
            assert not any(keyword.arg == "tmdbid" for keyword in call.keywords)
        for call in _call_names(source, "add"):
            if isinstance(call.func.value, ast.Attribute) and call.func.value.attr == "subscribechain":
                assert not any(keyword.arg == "tmdbid" for keyword in call.keywords)
