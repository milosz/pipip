"""Version parsing, filtering and comparison helpers.

The implementation prefers PEP 440-ish semver comparison when a version can be
parsed by the :mod:`semver` package and falls back to a tolerant natural sort
for distribution-style versions (for example Debian's ``1:1.22.1-9~deb12u1``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from functools import cmp_to_key
from typing import Any

import semver

from pipip.config import SourceOptions
from pipip.core.types import ReleaseInfo

_VERSION_RE = re.compile(
    r"^[vV]?(\d+(?:[._-]\d+)*)(?:[-+._]?([0-9A-Za-z][0-9A-Za-z._-]*))?$"
)
_DIGITS_RE = re.compile(r"\d+(?:[._-]\d+)*")
_PRERELEASE_RE = re.compile(
    r"(?:^|[._-])(alpha|beta|rc|pre|preview|dev|snapshot|nightly|canary|experimental)(?:[._-]?\d*)?(?:$|[._-])",
    re.IGNORECASE,
)


def clean_version(version: str) -> str:
    return version.strip().lstrip("vV")


def is_prerelease(version: str) -> bool:
    """Return ``True`` for versions carrying a common prerelease marker."""

    return bool(_PRERELEASE_RE.search(version.strip()))


def _numeric_parts(version: str) -> tuple[int, tuple[int, ...], str] | None:
    candidate = version.strip()
    epoch = 0
    if ":" in candidate:
        epoch_text, remainder = candidate.split(":", 1)
        if epoch_text.isdigit():
            epoch = int(epoch_text)
            candidate = remainder

    match = _VERSION_RE.match(candidate)
    if match:
        numbers = tuple(int(part) for part in re.split(r"[._-]", match.group(1)))
        return epoch, numbers, (match.group(2) or "")
    match = _DIGITS_RE.search(candidate)
    if not match:
        return None
    numbers = tuple(int(part) for part in re.split(r"[._-]", match.group(0)))
    remainder = candidate[match.end() :].lstrip(".-_+~")
    return epoch, numbers, remainder


def _compare_prerelease(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return 1
    if not right:
        return -1

    def tokens(value: str) -> list[str]:
        return [token for token in re.split(r"[._-]", value) if token]

    left_tokens = tokens(left)
    right_tokens = tokens(right)
    for l_token, r_token in zip(left_tokens, right_tokens, strict=False):
        if l_token == r_token:
            continue
        l_numeric = l_token.isdigit()
        r_numeric = r_token.isdigit()
        if l_numeric and r_numeric:
            return (int(l_token) > int(r_token)) - (int(l_token) < int(r_token))
        if l_numeric != r_numeric:
            return -1 if l_numeric else 1
        return (l_token > r_token) - (l_token < r_token)
    return (len(left_tokens) > len(right_tokens)) - (len(left_tokens) < len(right_tokens))


def compare_versions(left: str, right: str) -> int:
    """Compare two versions, returning -1, 0 or 1.

    Semver parsing is attempted first. Versions that are not valid semver are
    compared using a tolerant natural ordering, which keeps Debian/Ubuntu
    versions usable while still respecting prerelease suffixes.
    """

    left_clean = clean_version(left)
    right_clean = clean_version(right)
    if left_clean == right_clean:
        return 0

    # Semver ignores build metadata (the part after ``+``), while Debian and
    # other distributions use it for ordering. Fall back to natural comparison
    # whenever build metadata is present.
    if "+" not in left_clean and "+" not in right_clean:
        try:
            left_semver = semver.Version.parse(left_clean)
            right_semver = semver.Version.parse(right_clean)
        except ValueError:
            pass
        else:
            return left_semver.compare(right_semver)

    left_parts = _numeric_parts(left)
    right_parts = _numeric_parts(right)
    if left_parts is None or right_parts is None:
        return (left_clean > right_clean) - (left_clean < right_clean)

    left_epoch, left_numbers, left_pre = left_parts
    right_epoch, right_numbers, right_pre = right_parts
    if left_epoch != right_epoch:
        return (left_epoch > right_epoch) - (left_epoch < right_epoch)
    width = max(len(left_numbers), len(right_numbers))
    left_padded = left_numbers + (0,) * (width - len(left_numbers))
    right_padded = right_numbers + (0,) * (width - len(right_numbers))
    if left_padded != right_padded:
        return (left_padded > right_padded) - (left_padded < right_padded)
    return _compare_prerelease(left_pre, right_pre)


def version_key(version: str) -> Any:
    """Return a key usable with :func:`functools.cmp_to_key` based sorting."""

    return cmp_to_key(compare_versions)(version)


def filter_releases(
    releases: Iterable[ReleaseInfo],
    options: SourceOptions,
    *,
    sort_key: Callable[[str], Any] = version_key,
) -> list[ReleaseInfo]:
    """Apply target filters and return releases ordered newest first."""

    regex = re.compile(options.version_regex) if options.version_regex else None
    filtered: list[ReleaseInfo] = []
    seen: set[str] = set()
    for release in releases:
        version = release.version.strip()
        if not version or version in seen:
            continue
        if regex is not None and regex.search(version) is None:
            continue
        if not options.include_prerelease and (
            release.prerelease or is_prerelease(version)
        ):
            continue
        seen.add(version)
        filtered.append(release)
    filtered.sort(key=lambda item: sort_key(item.version), reverse=True)
    return filtered[: options.max_candidates]


def select_latest(
    releases: Iterable[ReleaseInfo],
    options: SourceOptions,
    *,
    sort_key: Callable[[str], Any] = version_key,
) -> ReleaseInfo | None:
    filtered = filter_releases(releases, options, sort_key=sort_key)
    return filtered[0] if filtered else None
