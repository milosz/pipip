from __future__ import annotations

from datetime import UTC, datetime

from pipip.config import DockerOptions
from pipip.core.types import ReleaseInfo
from pipip.core.versioning import compare_versions, filter_releases, is_prerelease


def test_semver_comparison() -> None:
    assert compare_versions("1.2.3", "1.2.4") < 0
    assert compare_versions("v2.0.0", "1.99.99") > 0
    assert compare_versions("1.2.3", "1.2.3") == 0
    assert compare_versions("1.2.3", "1.2.3-rc1") > 0


def test_debian_version_comparison() -> None:
    assert compare_versions("1.22.1-9+deb12u9", "1.22.1-9+deb12u8") > 0
    assert compare_versions("1:1.22.1-1", "1.22.1-9") > 0


def test_prerelease_detection() -> None:
    assert is_prerelease("1.0.0-rc.1")
    assert is_prerelease("2.0.0-beta3")
    assert not is_prerelease("1.0.0-alpine")
    assert not is_prerelease("2026.01.01")


def test_filter_releases() -> None:
    options = DockerOptions(image="library/nginx", version_regex=r"^\d+\.\d+\.\d+$")
    releases = [
        ReleaseInfo("1.2.3"),
        ReleaseInfo("1.2.4"),
        ReleaseInfo("1.2.5-rc1"),
        ReleaseInfo("latest"),
        ReleaseInfo("1.2.4"),
    ]
    filtered = filter_releases(releases, options)
    assert [release.version for release in filtered] == ["1.2.4", "1.2.3"]


def test_filter_releases_includes_prerelease() -> None:
    options = DockerOptions(
        image="library/nginx",
        include_prerelease=True,
        version_regex=r"^\d+\.\d+\.\d+",
    )
    filtered = filter_releases(
        [ReleaseInfo("1.2.3"), ReleaseInfo("1.3.0-rc1")], options
    )
    assert [release.version for release in filtered] == ["1.3.0-rc1", "1.2.3"]


def test_release_datetime_is_optional() -> None:
    release = ReleaseInfo("1.0.0", published_at=datetime.now(UTC))
    assert release.published_at is not None
