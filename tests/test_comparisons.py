from __future__ import annotations

import pytest
from pydantic import ValidationError

from pipip.config import ComparisonConfig, parse_config
from pipip.core.comparisons import build_comparison_results
from pipip.db.models import Target


@pytest.mark.parametrize(
    ("reference", "versions", "group_status", "target_statuses", "matched"),
    [
        ("1.1", ["1.1", "1.1"], "match", ["match", "match"], 2),
        ("1.1", ["1.1", "1.0"], "drift", ["match", "behind"], 1),
        ("1.1", ["1.2", None], "drift", ["ahead", "missing"], 0),
        ("1.1", ["1.1", None], "missing", ["match", "missing"], 1),
        (None, ["1.1", "1.0"], "missing", ["missing", "missing"], 0),
    ],
)
def test_reference_comparison(
    reference: str | None,
    versions: list[str | None],
    group_status: str,
    target_statuses: list[str],
    matched: int,
) -> None:
    config = ComparisonConfig(
        name="channels", reference="git", targets=["docker", "debian"], mode="semver"
    )
    apps = [
        Target(name=name, current_version=version)
        for name, version in zip(("git", "docker", "debian"), (reference, *versions), strict=True)
    ]
    (result,) = build_comparison_results([config], apps)
    assert result.status == group_status
    assert result.reference_version == reference
    assert result.matched == matched
    assert [target.status for target in result.targets] == target_statuses



def test_single_target_exact_comparison() -> None:
    config = ComparisonConfig(name="pair", reference="git", targets=["docker"], mode="exact")
    apps = [
        Target(name="git", current_version="v1.1.0"),
        Target(name="docker", current_version="1.1.0"),
    ]
    (result,) = build_comparison_results([config], apps)

    assert result.status == "drift"
    assert result.targets[0].status == "different"
    config.enabled = False
    assert build_comparison_results([config], apps) == []


@pytest.mark.parametrize(
    ("comparisons", "message"),
    [
        (
            [{"name": "pair", "reference": "git", "targets": ["other"]}],
            "references unknown target",
        ),
        (
            [{"name": "pair", "reference": "git", "targets": ["git"]}],
            "requires distinct reference and targets",
        ),
        (
            [{"name": "pair", "reference": "git", "targets": ["docker"]}] * 2,
            "duplicate comparison names",
        ),
        ([{"name": "pair", "left": "git", "right": "docker"}], "reference"),
    ],
)
def test_config_rejects_invalid_comparisons(
    comparisons: list[dict[str, object]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_config(
            {
                    "targets": [
                    {"name": name, "source": "docker", "options": {"image": name}}
                    for name in ("git", "docker")
                ],
                "comparisons": comparisons,
            }
        )
