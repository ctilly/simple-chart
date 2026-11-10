"""
AST regression guard: exception text never reaches UI or log output.

An exception value is a name bound by `except ... as e`, a parameter or
local annotated with an exception type, a name assigned from one, an
attribute or subscript of one, or the `__cause__` / `__context__` of any
value. Outside _ALLOWLIST the guard flags:

1. formatting an exception value: str/repr/ascii/format(e), f"{e}",
   "%s" % e, "...".format(e), e.__str__(); a comparison such as
   `"x" in str(e)` is not output and is exempt;
2. an exception value passed to UI text (setText, setToolTip,
   setWindowTitle, showMessage, show_warning, show_information,
   ApplicationMessageDialog, QMessageBox, print);
3. an exception value passed to a logging call as its message or arguments;
4. logger.exception(), exc_info= on a logging call, and the traceback
   module's exception formatters and printers.

Passing an exception to any other call (describe, user_message,
report_error) is allowed; a callee that takes an annotated exception
parameter is checked under the same rules. An except clause or annotation
naming only UserFacingError subclasses (found from the scanned tree's class
definitions) is exempt for its own text, which SimpleChart composed; the
exception it was raised from is not. Aliased imports and
loggers are resolved. This is a regression guard, not a proof.
"""

import ast
import re
import textwrap
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import NamedTuple

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_SCANNED = ("main.py", "app", "chart", "data", "simplechart", "tools", "indicators")

# (path, function) -> why that function may format an exception value.
_ALLOWLIST: dict[tuple[str, str], str] = {
    ("data/errors.py", "_header"): (
        "describe() itself: renders the described exception's own text only "
        "when its real type is a UserFacingError."
    ),
    ("app/errors.py", "user_message"): (
        "The shown message itself: the text of a UserFacingError only, by "
        "isinstance check."
    ),
}

FORMATTED = "exception text formatted"
UI_TEXT = "exception passed to UI text"
LOG_ARGUMENT = "exception passed to logging"
LOG_EXCEPTION = "logger.exception"
EXC_INFO = "exc_info on a logging call"
TRACEBACK = "traceback formatting"

_UI_METHODS = frozenset({
    "setText",
    "setToolTip",
    "setWindowTitle",
    "showMessage",
    "setStatusTip",
    "setPlainText",
    "setHtml",
    "setInformativeText",
    "setDetailedText",
})
_UI_FUNCTIONS = frozenset({"show_warning", "show_information", "ApplicationMessageDialog"})
_LOG_METHODS = frozenset({
    "debug", "info", "warning", "warn", "error", "critical", "fatal", "exception", "log",
})
_LOG_CONTROL_KEYWORDS = frozenset({"exc_info", "stack_info", "stacklevel"})
_TRACEBACK_OUTPUT = frozenset({
    "traceback.format_exc",
    "traceback.format_exception",
    "traceback.format_exception_only",
    "traceback.print_exc",
    "traceback.print_exception",
    "traceback.print_last",
})
_STRINGIFIERS = frozenset({"str", "repr", "ascii", "format"})
_FORMAT_METHODS = frozenset({"format", "format_map"})
_DUNDER_TEXT = frozenset({"__str__", "__repr__", "__format__"})
# A chained exception is library text even under a UserFacingError.
_CHAINED = frozenset({"__cause__", "__context__"})
_LOGGER_NAME = re.compile(r"_*log(ger)?", re.IGNORECASE)
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class Finding(NamedTuple):
    path: str
    line: int
    function: str
    rule: str


def _final_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _type_names(node: ast.expr | None) -> list[str]:
    # Class names in an except clause or annotation; type[...] is a class,
    # not an instance, and is skipped.
    if node is None:
        return []
    if isinstance(node, ast.Tuple):
        return [name for element in node.elts for name in _type_names(element)]
    if isinstance(node, ast.BinOp):
        return _type_names(node.left) + _type_names(node.right)
    if isinstance(node, ast.Subscript):
        if _final_name(node.value) == "type":
            return []
        return _type_names(node.slice)
    name = _final_name(node)
    return [] if name is None else [name]


def _is_exception_name(name: str) -> bool:
    return name in ("BaseException", "Exception") or name.endswith(("Error", "Exception"))


def _bound_names(target: ast.AST) -> list[str]:
    return [
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    ]


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is not None:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                module = node.module or ""
                aliases[alias.asname or alias.name] = f"{module}.{alias.name}".lstrip(".")
    return aliases


class _ModuleScan:
    def __init__(self, tree: ast.Module, path: str, user_facing: frozenset[str]) -> None:
        self._path = path
        self._user_facing = user_facing
        self._aliases = _import_aliases(tree)
        self._in_comparison = {
            id(inner)
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            for inner in ast.walk(node)
        }
        self._loggers: set[str] = set()
        self._collect_loggers(tree)
        self.findings: list[Finding] = []

    def scan(self, scope: ast.AST, qualname: str, inherited: frozenset[str]) -> None:
        own, nested = self._own_nodes(scope)
        tainted = set(inherited) | self._tainted_parameters(scope)
        changed = True
        while changed:
            changed = False
            for node in own:
                for name in self._newly_tainted(node, tainted):
                    if name not in tainted:
                        tainted.add(name)
                        changed = True
        for node in own:
            self._check(node, tainted, qualname)
        prefix = "" if qualname == "<module>" else f"{qualname}."
        for child in nested:
            self.scan(child, f"{prefix}{child.name}", frozenset(tainted))

    def _own_nodes(
        self,
        scope: ast.AST,
    ) -> tuple[list[ast.AST], list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]]:
        own: list[ast.AST] = []
        nested: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, _SCOPE_NODES):
                nested.append(node)
                continue
            own.append(node)
            stack.extend(ast.iter_child_nodes(node))
        return own, nested

    def _collect_loggers(self, tree: ast.Module) -> None:
        # Two passes so a logger aliased from another alias is found.
        for _ in range(2):
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and self._is_logger(node.value):
                    for target in node.targets:
                        name = _final_name(target)
                        if name is not None:
                            self._loggers.add(name)
                elif isinstance(node, ast.AnnAssign):
                    name = _final_name(node.target)
                    is_logger = "Logger" in _type_names(node.annotation) or (
                        node.value is not None and self._is_logger(node.value)
                    )
                    if name is not None and is_logger:
                        self._loggers.add(name)
                elif isinstance(node, ast.arg) and "Logger" in _type_names(node.annotation):
                    self._loggers.add(node.arg)

    def _dotted(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self._aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._dotted(node.value)
            return None if base is None else f"{base}.{node.attr}"
        return None

    def _is_logger(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call):
            called = self._dotted(node.func)
            return called is not None and called.split(".")[-1] == "getLogger"
        if self._dotted(node) == "logging":
            return True
        name = _final_name(node)
        return name is not None and (
            name in self._loggers or _LOGGER_NAME.fullmatch(name) is not None
        )

    def _is_user_facing_only(self, names: list[str]) -> bool:
        return bool(names) and all(name in self._user_facing for name in names)

    def _is_exception_annotation(self, annotation: ast.expr | None) -> bool:
        names = [name for name in _type_names(annotation) if _is_exception_name(name)]
        return bool(names) and not self._is_user_facing_only(names)

    def _tainted_parameters(self, scope: ast.AST) -> set[str]:
        if not isinstance(scope, _FUNCTION_NODES):
            return set()
        arguments = scope.args
        parameters = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *(argument for argument in (arguments.vararg, arguments.kwarg) if argument),
        ]
        return {
            parameter.arg
            for parameter in parameters
            if self._is_exception_annotation(parameter.annotation)
        }

    def _newly_tainted(self, node: ast.AST, tainted: set[str]) -> list[str]:
        if isinstance(node, ast.ExceptHandler):
            if node.name is not None and not self._is_user_facing_only(_type_names(node.type)):
                return [node.name]
        elif isinstance(node, ast.AnnAssign):
            if self._is_exception_annotation(node.annotation) or (
                node.value is not None and self._carries(node.value, tainted)
            ):
                return _bound_names(node.target)
        elif isinstance(node, ast.Assign):
            if self._carries(node.value, tainted):
                return [name for target in node.targets for name in _bound_names(target)]
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            if self._carries(node.value, tainted):
                return _bound_names(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            if self._carries(node.iter, tainted):
                return _bound_names(node.target)
        return []

    def _raw(self, node: ast.AST, tainted: set[str]) -> bool:
        # The exception value itself, an attribute or subscript of it, a
        # chained exception, or a container holding one. A call's result is
        # opaque.
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, ast.Name) and current.id in tainted:
                return True
            if isinstance(current, ast.Attribute) and current.attr in _CHAINED:
                return True
            if isinstance(current, (ast.Call, ast.Compare, ast.Lambda)):
                continue
            stack.extend(ast.iter_child_nodes(current))
        return False

    def _carries(self, node: ast.AST, tainted: set[str]) -> bool:
        return self._raw(node, tainted) or any(
            self._formats(inner, tainted) for inner in ast.walk(node)
        )

    def _formats(self, node: ast.AST, tainted: set[str]) -> bool:
        if isinstance(node, ast.FormattedValue):
            return self._raw(node.value, tainted)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return self._raw(node.right, tainted)
        if not isinstance(node, ast.Call):
            return False
        if self._dotted(node.func) in _STRINGIFIERS:
            return bool(node.args) and self._raw(node.args[0], tainted)
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in _FORMAT_METHODS:
                values = [*node.args, *(keyword.value for keyword in node.keywords)]
                return any(self._raw(value, tainted) for value in values)
            if node.func.attr in _DUNDER_TEXT:
                return self._raw(node.func.value, tainted)
        return False

    def _is_ui_sink(self, call: ast.Call) -> bool:
        if isinstance(call.func, ast.Attribute) and call.func.attr in _UI_METHODS:
            return True
        called = self._dotted(call.func)
        if called is None:
            return False
        parts = called.split(".")
        return called == "print" or parts[-1] in _UI_FUNCTIONS or "QMessageBox" in parts

    def _check(self, node: ast.AST, tainted: set[str], qualname: str) -> None:
        def report(rule: str) -> None:
            line = getattr(node, "lineno", 0)
            self.findings.append(Finding(self._path, line, qualname, rule))

        if id(node) not in self._in_comparison and self._formats(node, tainted):
            report(FORMATTED)
        if not isinstance(node, ast.Call):
            return
        called = self._dotted(node.func) or ""
        if called in _TRACEBACK_OUTPUT or called.startswith("traceback.TracebackException"):
            report(TRACEBACK)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS
            and self._is_logger(node.func.value)
        ):
            if node.func.attr == "exception":
                report(LOG_EXCEPTION)
            values: list[ast.expr] = list(node.args)
            for keyword in node.keywords:
                if keyword.arg == "exc_info":
                    value = keyword.value
                    if not (isinstance(value, ast.Constant) and value.value in (False, None)):
                        report(EXC_INFO)
                elif keyword.arg not in _LOG_CONTROL_KEYWORDS:
                    values.append(keyword.value)
            if any(self._raw(value, tainted) for value in values):
                report(LOG_ARGUMENT)
        elif self._is_ui_sink(node):
            values = [*node.args, *(keyword.value for keyword in node.keywords)]
            if any(self._raw(value, tainted) for value in values):
                report(UI_TEXT)


def user_facing_names(trees: Iterable[ast.Module]) -> frozenset[str]:
    classes = [node for tree in trees for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    names = {"UserFacingError"}
    changed = True
    while changed:
        changed = False
        for node in classes:
            if node.name not in names and any(_final_name(base) in names for base in node.bases):
                names.add(node.name)
                changed = True
    return frozenset(names)


def scan_module(tree: ast.Module, path: str, user_facing: frozenset[str]) -> list[Finding]:
    module_scan = _ModuleScan(tree, path, user_facing)
    module_scan.scan(tree, "<module>", frozenset())
    return sorted(module_scan.findings)


def scanned_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for entry in _SCANNED:
        path = root / entry
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
    return files


def scan_tree(root: Path) -> list[Finding]:
    trees = {
        path.relative_to(root).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in scanned_files(root)
    }
    user_facing = user_facing_names(trees.values())
    return [
        finding
        for path, tree in trees.items()
        for finding in scan_module(tree, path, user_facing)
    ]


@cache
def _project_findings() -> tuple[Finding, ...]:
    return tuple(scan_tree(_ROOT))


@cache
def _project_user_facing() -> frozenset[str]:
    return user_facing_names(
        ast.parse(path.read_text(encoding="utf-8")) for path in scanned_files(_ROOT)
    )


def _scan_snippet(source: str) -> list[Finding]:
    tree = ast.parse(textwrap.dedent(source))
    return scan_module(tree, "app/fixture.py", _project_user_facing())


def test_project_has_no_raw_exception_output() -> None:
    violations = [
        finding
        for finding in _project_findings()
        if (finding.path, finding.function) not in _ALLOWLIST
    ]
    assert violations == []


def test_every_allowlist_entry_is_still_needed() -> None:
    flagged = {(finding.path, finding.function) for finding in _project_findings()}
    assert set(_ALLOWLIST) <= flagged


def test_every_listed_package_is_scanned() -> None:
    scanned = {path.relative_to(_ROOT).parts[0] for path in scanned_files(_ROOT)}
    assert scanned == set(_SCANNED)


def test_user_facing_names_come_from_the_tree() -> None:
    names = _project_user_facing()
    assert {"BarCorrectionError", "HistoryUnavailableError", "ProviderConfigurationError"} <= names
    assert "RequestCancelledError" not in names


_POSITIVE_FIXTURES: dict[str, tuple[str, str]] = {
    "aliased logger": (
        """
        import logging
        audit = logging.getLogger(__name__)
        channel = audit

        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                channel.warning("Load failed: %s", exc)
        """,
        LOG_ARGUMENT,
    ),
    "aliased logging module": (
        """
        import logging as lg

        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                lg.error(exc)
        """,
        LOG_ARGUMENT,
    ),
    "attribute logger": (
        """
        class Loader:
            def load(self) -> None:
                try:
                    fetch()
                except OSError as exc:
                    self._logger.info("failed: %r", exc.args)
        """,
        LOG_ARGUMENT,
    ),
    "multi-line call": (
        """
        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                _LOGGER.error(
                    "Load failed: %s",
                    exc,
                )
        """,
        LOG_ARGUMENT,
    ),
    "logging msg keyword": (
        """
        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                _LOGGER.log(level=40, msg=exc)
        """,
        LOG_ARGUMENT,
    ),
    "f-string": (
        """
        def load() -> str:
            try:
                fetch()
            except Exception as exc:
                return f"Load failed: {exc}"
            return ""
        """,
        FORMATTED,
    ),
    "f-string repr": (
        """
        def load() -> str:
            try:
                fetch()
            except Exception as exc:
                return f"{exc!r}"
            return ""
        """,
        FORMATTED,
    ),
    "str call": (
        """
        def load() -> None:
            try:
                fetch()
            except ValueError as exc:
                self.message = str(exc)
        """,
        FORMATTED,
    ),
    "repr call": (
        """
        def load() -> None:
            try:
                fetch()
            except ValueError as exc:
                record(repr(exc))
        """,
        FORMATTED,
    ),
    "percent format": (
        """
        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                text = "Load failed: %s" % (exc,)
        """,
        FORMATTED,
    ),
    "str.format": (
        """
        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                text = "Load failed: {}".format(exc)
        """,
        FORMATTED,
    ),
    "wrapped in another exception": (
        """
        def load() -> None:
            try:
                fetch()
            except KeyError as exc:
                raise ProviderConfigurationError(str(exc)) from exc
        """,
        FORMATTED,
    ),
    "assignment then UI": (
        """
        def load(self) -> None:
            try:
                fetch()
            except Exception as exc:
                detail = exc
            self.status_bar.showMessage(detail)
        """,
        UI_TEXT,
    ),
    "assignment then logging": (
        """
        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                reason = exc.args
                for part in reason:
                    _LOGGER.error(part)
        """,
        LOG_ARGUMENT,
    ),
    "label text": (
        """
        def load(self) -> None:
            try:
                fetch()
            except Exception as exc:
                self._label.setText(f"Error: {exc}")
        """,
        UI_TEXT,
    ),
    "tooltip": (
        """
        def load(self) -> None:
            try:
                fetch()
            except Exception as exc:
                self._button.setToolTip(exc.args[0])
        """,
        UI_TEXT,
    ),
    "window title": (
        """
        def load(self) -> None:
            try:
                fetch()
            except Exception as exc:
                self.setWindowTitle(f"{exc!r}")
        """,
        UI_TEXT,
    ),
    "show_warning": (
        """
        from app.dialogs import show_warning as warn

        def load(self) -> None:
            try:
                fetch()
            except Exception as exc:
                warn(self, "Load Failed", f"{exc}")
        """,
        UI_TEXT,
    ),
    "ApplicationMessageDialog": (
        """
        def load(self) -> None:
            try:
                fetch()
            except Exception as exc:
                detail = exc.args[0]
                ApplicationMessageDialog(self, "Load Failed", detail)
        """,
        UI_TEXT,
    ),
    "QMessageBox": (
        """
        from PyQt6.QtWidgets import QMessageBox as Box

        def load(self) -> None:
            try:
                fetch()
            except Exception as exc:
                Box.critical(
                    self,
                    "Error",
                    f"{exc}",
                )
        """,
        UI_TEXT,
    ),
    "logger.exception": (
        """
        def load() -> None:
            try:
                fetch()
            except Exception:
                _LOGGER.exception("Load failed")
        """,
        LOG_EXCEPTION,
    ),
    "traceback.format_exc": (
        """
        import traceback as tb

        def load() -> None:
            try:
                fetch()
            except Exception:
                record(tb.format_exc())
        """,
        TRACEBACK,
    ),
    "imported print_exc": (
        """
        from traceback import print_exc

        def load() -> None:
            try:
                fetch()
            except Exception:
                print_exc()
        """,
        TRACEBACK,
    ),
    "exc_info=True": (
        """
        def load() -> None:
            try:
                fetch()
            except Exception:
                _LOGGER.error("Load failed", exc_info=True)
        """,
        EXC_INFO,
    ),
    "exc_info=exc": (
        """
        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                _LOGGER.warning("Load failed", exc_info=exc)
        """,
        EXC_INFO,
    ),
    "annotated exception parameter": (
        """
        def show_failure(self, error: Exception) -> None:
            self._label.setText(str(error))
        """,
        FORMATTED,
    ),
    "user-facing mixed with library error": (
        """
        def load(self) -> None:
            try:
                fetch()
            except (BarCorrectionError, ValueError) as exc:
                self._label.setText(str(exc))
        """,
        FORMATTED,
    ),
    "user-facing cause formatted": (
        """
        def load(self) -> None:
            try:
                fetch()
            except ProviderUnreachableError as exc:
                show_warning(self, "Unreachable", str(exc.__cause__))
        """,
        FORMATTED,
    ),
    "user-facing cause to logging": (
        """
        def load() -> None:
            try:
                fetch()
            except ProviderUnreachableError as exc:
                _LOGGER.error("Load failed: %s", exc.__cause__)
        """,
        LOG_ARGUMENT,
    ),
    "user-facing cause assigned then logged": (
        """
        def load() -> None:
            try:
                fetch()
            except HistoryUnavailableError as exc:
                cause = exc.__cause__
                _LOGGER.warning(cause)
        """,
        LOG_ARGUMENT,
    ),
    "user-facing context to UI": (
        """
        def load(self) -> None:
            try:
                fetch()
            except BarCorrectionError as exc:
                self._label.setText(exc.__context__)
        """,
        UI_TEXT,
    ),
    "user-facing context assigned then UI": (
        """
        def load(self) -> None:
            try:
                fetch()
            except BarCorrectionError as exc:
                inner = exc.__context__
                self._label.setToolTip(f"{inner}")
        """,
        UI_TEXT,
    ),
    "user-facing parameter cause": (
        """
        def show_failure(self, error: ProviderRateLimitedError) -> None:
            self._label.setText(repr(error.__cause__))
        """,
        FORMATTED,
    ),
    "print": (
        """
        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                print(exc)
        """,
        UI_TEXT,
    ),
}


@pytest.mark.parametrize(
    ("source", "rule"),
    list(_POSITIVE_FIXTURES.values()),
    ids=list(_POSITIVE_FIXTURES),
)
def test_mutation_fixture_is_flagged(source: str, rule: str) -> None:
    assert rule in {finding.rule for finding in _scan_snippet(source)}


_NEGATIVE_FIXTURES: dict[str, str] = {
    "describe": """
        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                _LOGGER.error("Load failed: %s", describe(exc))
        """,
    "report_error": """
        def load(self) -> None:
            try:
                fetch()
            except Exception as exc:
                show_warning(
                    self,
                    "Load Failed",
                    report_error(_LOGGER, exc, ErrorContext("Loading")),
                )
        """,
    "user_message": """
        def load(self) -> None:
            try:
                fetch()
            except Exception as exc:
                self._status.setText(user_message(exc, ErrorContext("Loading")))
        """,
    "user-facing only except": """
        def save(self) -> None:
            try:
                store()
            except BarCorrectionError as exc:
                self._label.setText(str(exc))
            except (HistoryUnavailableError, MarketDataEntitlementError) as exc:
                show_warning(self, "Unavailable", f"{exc}")
        """,
    "comparison": """
        def save(self) -> None:
            try:
                store()
            except ValueError as exc:
                if "must differ" in str(exc):
                    show_information(self, "No Correction", "Nothing changed.")
        """,
    "type name and status": """
        def load() -> None:
            try:
                fetch()
            except Exception as exc:
                _LOGGER.error("failed: %s HTTP %s", type(exc).__name__, http_status(exc))
        """,
    "translated with cause": """
        def load() -> None:
            try:
                fetch()
            except OSError as exc:
                raise ProviderUnreachableError("Alpaca") from exc
        """,
    "stack locations only": """
        import traceback

        def main() -> None:
            try:
                run()
            except Exception as error:
                frames = traceback.extract_tb(error.__traceback__)
                print(f"{len(frames)} frames")
        """,
    "exc_info disabled": """
        def load() -> None:
            try:
                fetch()
            except Exception:
                _LOGGER.error("Load failed", exc_info=False)
        """,
}


@pytest.mark.parametrize(
    "source",
    list(_NEGATIVE_FIXTURES.values()),
    ids=list(_NEGATIVE_FIXTURES),
)
def test_shielded_fixture_is_not_flagged(source: str) -> None:
    assert _scan_snippet(source) == []


def test_tools_and_indicators_are_scanned(tmp_path: Path) -> None:
    violation = textwrap.dedent(
        """
        def compute(self) -> None:
            try:
                run()
            except Exception as exc:
                self._logger.error("%s", exc)
        """
    )
    (tmp_path / "tools" / "ruler").mkdir(parents=True)
    (tmp_path / "tools" / "ruler" / "__init__.py").write_text(violation, encoding="utf-8")
    (tmp_path / "indicators").mkdir()
    (tmp_path / "indicators" / "band.py").write_text(violation, encoding="utf-8")

    flagged = {(finding.path, finding.rule) for finding in scan_tree(tmp_path)}

    assert flagged == {
        ("tools/ruler/__init__.py", LOG_ARGUMENT),
        ("indicators/band.py", LOG_ARGUMENT),
    }
