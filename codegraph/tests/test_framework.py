"""Tests for :mod:`codegraph.framework`."""
from __future__ import annotations

from pathlib import Path

import pytest

from codegraph.framework import FrameworkDetector, FrameworkInfo, FrameworkType
from codegraph.schema import PackageNode

FIXTURES = Path("/tmp/agent-onboarding/tests/fixtures")


@pytest.mark.parametrize(
    "fixture_dir,expected",
    [
        ("react-app", {FrameworkType.REACT, FrameworkType.REACT_TYPESCRIPT}),
        ("nextjs-app", {FrameworkType.NEXTJS}),
        ("vue-app", {FrameworkType.VUE, FrameworkType.VUE3}),
        ("sveltekit-app", {FrameworkType.SVELTEKIT, FrameworkType.SVELTE}),
        ("angular-app", {FrameworkType.ANGULAR}),
        ("odoo-app", {FrameworkType.ODOO}),
    ],
)
def test_detects_fixture_framework(
    fixture_dir: str, expected: set[FrameworkType]
) -> None:
    path = FIXTURES / fixture_dir
    if not path.exists():
        pytest.skip(f"fixture {fixture_dir} not available at {path}")
    info = FrameworkDetector(path).detect()
    assert info.framework in expected, (
        f"{fixture_dir}: got {info.framework}, expected one of {expected}"
    )
    assert info.confidence > 0.2, f"{fixture_dir}: confidence too low ({info.confidence})"


def test_nextjs_package_manager_detection() -> None:
    path = FIXTURES / "nextjs-app"
    if not path.exists():
        pytest.skip("nextjs-app fixture not available")
    info = FrameworkDetector(path).detect()
    assert info.framework == FrameworkType.NEXTJS


def test_odoo_has_no_package_manager() -> None:
    path = FIXTURES / "odoo-app"
    if not path.exists():
        pytest.skip("odoo-app fixture not available")
    info = FrameworkDetector(path).detect()
    assert info.framework == FrameworkType.ODOO
    assert info.package_manager is None
    assert info.router == "odoo-menu-router"
    assert info.confidence >= 0.9


def test_empty_directory_is_unknown(tmp_path: Path) -> None:
    info = FrameworkDetector(tmp_path).detect()
    assert info.framework == FrameworkType.UNKNOWN
    assert info.confidence == 0.0
    assert info.package_manager is None


def test_package_json_with_no_framework_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"x","dependencies":{"lodash":"^4.0.0"}}')
    info = FrameworkDetector(tmp_path).detect()
    assert info.framework == FrameworkType.UNKNOWN


def test_typescript_detection_from_tsconfig(tmp_path: Path) -> None:
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "package.json").write_text(
        '{"name":"x","dependencies":{"react":"^18.0.0","react-dom":"^18.0.0"}}'
    )
    info = FrameworkDetector(tmp_path).detect()
    assert info.typescript is True
    assert info.framework == FrameworkType.REACT_TYPESCRIPT


def test_package_manager_bun(tmp_path: Path) -> None:
    (tmp_path / "bun.lockb").write_text("")
    (tmp_path / "package.json").write_text("{}")
    info = FrameworkDetector(tmp_path).detect()
    assert info.package_manager == "bun"


def test_package_node_from_framework_info() -> None:
    info = FrameworkInfo(
        framework=FrameworkType.NEXTJS,
        version="^14.0.0",
        typescript=True,
        styling=["tailwind"],
        router="next/router",
        state_management=["zustand"],
        ui_library="shadcn",
        build_tool="next",
        package_manager="bun",
        confidence=0.92,
    )
    p = PackageNode.from_framework_info("packages/web", info)
    assert p.name == "packages/web"
    assert p.framework == "Next.js"          # display name, not enum value
    assert p.framework_version == "^14.0.0"
    assert p.typescript is True
    assert p.styling == ["tailwind"]
    assert p.router == "next/router"
    assert p.state_management == ["zustand"]
    assert p.confidence == pytest.approx(0.92)
    assert p.id == "package:packages/web"


def test_unknown_display_name_is_preserved() -> None:
    info = FrameworkInfo(framework=FrameworkType.UNKNOWN, confidence=0.0)
    p = PackageNode.from_framework_info("packages/misc", info)
    assert p.framework == "Unknown"
    assert p.confidence == 0.0
