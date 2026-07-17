"""Runtime v2 的依赖方向契约测试。"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


@dataclass(frozen=True)
class ImportViolation:
    """描述一条违反 Runtime v2 依赖边界的导入。"""

    source_path: Path
    imported_module: str


class RuntimeV2BoundaryDirectory(StrEnum):
    """受 Runtime v2 依赖方向约束的源码目录。"""

    DOMAIN = "domain"
    APPLICATION = "application"


class ForbiddenModulePrefix(StrEnum):
    """Runtime v2 不得直接依赖的具体基础设施模块前缀。"""

    JOURNAL = "dotclaw.journal"
    SESSION = "dotclaw.session"
    SLOT_CONTEXT = "dotclaw.agent.slotContext"
    MEMORY = "dotclaw.memory"
    MCP = "dotclaw.mcp"
    CHANNEL = "dotclaw.channel"


PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
RUNTIME_ROOT: Path = PROJECT_ROOT / "src" / "dotclaw" / "runtime"
V2_BOUNDARY_DIRECTORIES: tuple[Path, ...] = (
    *(RUNTIME_ROOT / directory.value for directory in RuntimeV2BoundaryDirectory),
)
FORBIDDEN_MODULE_PREFIXES: tuple[ForbiddenModulePrefix, ...] = tuple(ForbiddenModulePrefix)


def _module_is_forbidden(module_name: str) -> bool:
    """判断导入模块是否属于 Runtime v2 禁止依赖的具体实现。"""
    forbidden_prefix: ForbiddenModulePrefix
    for forbidden_prefix in FORBIDDEN_MODULE_PREFIXES:
        prefix_value: str = forbidden_prefix.value
        if module_name == prefix_value or module_name.startswith(f"{prefix_value}."):
            return True
    return False


def _collect_imported_modules(
    source_path: Path,
    package_name: str | None = None,
) -> tuple[str, ...]:
    """解析单个源码文件中声明的绝对导入模块。"""
    source_code: str = source_path.read_text(encoding="utf-8")
    syntax_tree: ast.Module = ast.parse(source_code, filename=str(source_path))
    imported_modules: list[str] = []
    syntax_nodes: list[ast.AST] = list(ast.walk(syntax_tree))

    syntax_node: ast.AST
    for syntax_node in syntax_nodes:
        if isinstance(syntax_node, ast.Import):
            import_alias: ast.alias
            for import_alias in syntax_node.names:
                imported_modules.append(import_alias.name)
        elif isinstance(syntax_node, ast.ImportFrom):
            imported_module: str | None = _resolve_import_from_module(
                source_path,
                syntax_node,
                package_name,
            )
            if imported_module is not None:
                imported_modules.append(imported_module)

    return tuple(imported_modules)


def _resolve_import_from_module(
    source_path: Path,
    import_node: ast.ImportFrom,
    package_name: str | None,
) -> str | None:
    """将绝对或相对的 from import 还原为完整模块名。"""
    if import_node.module is None:
        return None
    if import_node.level == 0:
        return import_node.module

    resolved_package_name: str | None = package_name or _source_package_name(source_path)
    if resolved_package_name is None:
        return import_node.module

    package_parts: list[str] = resolved_package_name.split(".")
    parent_depth: int = import_node.level - 1
    if parent_depth >= len(package_parts):
        return import_node.module

    parent_parts: list[str] = package_parts[: len(package_parts) - parent_depth]
    return ".".join((*parent_parts, import_node.module))


def _source_package_name(source_path: Path) -> str | None:
    """根据项目源码路径推导文件所属的 Python 包。"""
    source_root: Path = PROJECT_ROOT / "src"
    try:
        relative_source_path: Path = source_path.relative_to(source_root)
    except ValueError:
        return None

    module_parts: list[str] = list(relative_source_path.with_suffix("").parts)
    if module_parts[-1] != "__init__":
        module_parts.pop()
    return ".".join(module_parts)


def _find_forbidden_imports(source_paths: tuple[Path, ...]) -> tuple[ImportViolation, ...]:
    """找出指定 Runtime v2 文件中的所有禁止导入。"""
    violations: list[ImportViolation] = []
    source_path: Path
    for source_path in source_paths:
        imported_module: str
        for imported_module in _collect_imported_modules(source_path):
            if _module_is_forbidden(imported_module):
                violations.append(ImportViolation(source_path, imported_module))
    return tuple(violations)


def _runtime_v2_source_paths() -> tuple[Path, ...]:
    """收集受 Runtime v2 新边界约束的源码文件。"""
    source_paths: list[Path] = []
    boundary_directory: Path
    for boundary_directory in V2_BOUNDARY_DIRECTORIES:
        if boundary_directory.exists():
            source_paths.extend(boundary_directory.rglob("*.py"))
    return tuple(sorted(source_paths))


def test_architecture_guard_detects_forbidden_concrete_import(tmp_path: Path) -> None:
    """验证护栏可识别 Journal、Session 与 Slot 的直接依赖。"""
    sample_source: Path = tmp_path / "engine.py"
    sample_source.write_text(
        "from dotclaw.journal import Journal\n"
        "from dotclaw.session.session import Session\n"
        "from dotclaw.agent.slotContext import SlotContext\n",
        encoding="utf-8",
    )

    violations: tuple[ImportViolation, ...] = _find_forbidden_imports((sample_source,))
    imported_modules: set[str] = {violation.imported_module for violation in violations}

    assert imported_modules == {
        "dotclaw.journal",
        "dotclaw.session.session",
        "dotclaw.agent.slotContext",
    }


def test_architecture_guard_resolves_relative_concrete_import(tmp_path: Path) -> None:
    """验证护栏不会被 RuntimeEngine 的相对导入绕过。"""
    sample_source: Path = tmp_path / "engine.py"
    sample_source.write_text(
        "from ...journal import Journal\n",
        encoding="utf-8",
    )

    imported_modules: tuple[str, ...] = _collect_imported_modules(
        sample_source,
        "dotclaw.runtime.application",
    )

    assert imported_modules == ("dotclaw.journal",)
    assert _module_is_forbidden(imported_modules[0])


def test_runtime_v2_boundary_has_no_concrete_infrastructure_imports() -> None:
    """保证未来 RuntimeEngine 只能经 Port 使用具体基础设施。"""
    source_paths: tuple[Path, ...] = _runtime_v2_source_paths()
    violations: tuple[ImportViolation, ...] = _find_forbidden_imports(source_paths)
    violation_descriptions: list[str] = [
        f"{violation.source_path.relative_to(PROJECT_ROOT)} -> {violation.imported_module}"
        for violation in violations
    ]

    assert not violation_descriptions, "\n".join(violation_descriptions)


def test_domain_has_no_application_dependency_and_no_generic_models_module() -> None:
    """Domain 只能描述事实和规则，不能反向导入 Application DTO 或执行过程。"""
    domain_directory: Path = RUNTIME_ROOT / RuntimeV2BoundaryDirectory.DOMAIN.value
    domain_paths: tuple[Path, ...] = tuple(sorted(domain_directory.rglob("*.py")))
    application_imports: list[str] = []
    source_path: Path
    for source_path in domain_paths:
        imported_module: str
        for imported_module in _collect_imported_modules(source_path):
            if imported_module == "dotclaw.runtime.application" or imported_module.startswith(
                "dotclaw.runtime.application.",
            ):
                application_imports.append(
                    f"{source_path.relative_to(PROJECT_ROOT)} -> {imported_module}",
                )

    assert not application_imports, "\n".join(application_imports)
    assert not (domain_directory / "models.py").exists()
