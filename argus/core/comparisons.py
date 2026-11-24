"""Version comparison helpers against reference targets."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from argus.config import ComparisonConfig
from argus.core.versioning import compare_versions
from argus.db.models import Target


def _target_status(reference_version: str, target_version: str, mode: str) -> str:
    if reference_version == target_version:
        return "match"
    order = compare_versions(target_version, reference_version)
    if order > 0:
        return "ahead"
    if order < 0:
        return "behind"
    return "match" if mode == "semver" else "different"


class ReferenceTargetResult(BaseModel):
    name: str
    version: str | None
    status: str


class ReferenceComparisonResult(BaseModel):
    name: str
    reference: str
    reference_version: str | None
    targets: list[ReferenceTargetResult]
    status: str
    matched: int
    mode: str
    description: str | None = None


def build_comparison_results(
    comparisons: Sequence[ComparisonConfig],
    apps: Sequence[Target],
) -> list[ReferenceComparisonResult]:
    versions = {app.name: app.current_version for app in apps}
    results: list[ReferenceComparisonResult] = []
    for config in comparisons:
        if not config.enabled:
            continue
        reference = versions.get(config.reference)
        targets: list[ReferenceTargetResult] = []
        for name in config.targets:
            version = versions.get(name)
            status = "missing"
            if reference is not None and version is not None:
                status = _target_status(reference, version, config.mode)
            targets.append(ReferenceTargetResult(name=name, version=version, status=status))

        matched = sum(target.status == "match" for target in targets)
        if matched == len(targets):
            group_status = "match"
        elif any(target.status in {"ahead", "behind", "different"} for target in targets):
            group_status = "drift"
        else:
            group_status = "missing"
        results.append(
            ReferenceComparisonResult(
                name=config.name,
                reference=config.reference,
                reference_version=reference,
                targets=targets,
                status=group_status,
                matched=matched,
                mode=config.mode,
                description=config.description,
            )
        )
    return results
