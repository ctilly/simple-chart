import ast
from pathlib import Path


def test_app_does_not_import_yahoo_reference_implementations() -> None:
    root = Path(__file__).resolve().parent.parent
    forbidden = (
        "yfinance",
        "providers",
        "data.provider.symbol_details",
        "data.provider.symbol_search",
    )
    violations: list[str] = []
    for path in sorted((root / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [f"{node.module}.{alias.name}" for alias in node.names]
            else:
                continue
            for module in modules:
                if any(module == name or module.startswith(f"{name}.") for name in forbidden):
                    violations.append(f"{path.relative_to(root)}:{node.lineno} imports {module}")
    assert not violations, "\n".join(violations)
