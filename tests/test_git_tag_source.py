from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pipip.config import GitTagOptions, parse_config
from pipip.sources.base import SourceError
from pipip.sources.git_tag import GitTagSource


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 128])
async def test_git_tag_source_runs_ls_remote_and_filters_tags(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    remote = "https://git.example.test/project.git"
    source = GitTagSource(
        GitTagOptions(url=remote, web_url="https://git.example.test/project/releases/tag/{tag}"),
        parse_config({}),
    )
    monkeypatch.setattr("pipip.sources.git_tag.ensure_url_allowed", AsyncMock())
    process = SimpleNamespace(
        returncode=returncode,
        communicate=AsyncMock(return_value=(
            b"abc\trefs/tags/v1.2.0\ndef\trefs/tags/v2.0.0-rc.1\nignored\trefs/heads/main\n",
            b"repository unavailable" if returncode else b"",
        )),
    )
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr("pipip.sources.git_tag.asyncio.create_subprocess_exec", launch)

    if returncode:
        with pytest.raises(SourceError, match="repository unavailable"):
            await source.latest()
    else:
        release = await source.latest()
        assert release is not None
        assert release.version == "v1.2.0"
        assert release.digest == "abc"
        assert release.url == "https://git.example.test/project/releases/tag/v1.2.0"

    assert launch.call_args.args[-4:] == ("ls-remote", "--tags", "--refs", remote)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [TimeoutError, asyncio.CancelledError])
async def test_git_tag_stops_transport_on_interruption(
    monkeypatch: pytest.MonkeyPatch, interrupted: type[BaseException]
) -> None:
    source = GitTagSource(GitTagOptions(url="https://git.example.test/project"), parse_config({}))
    monkeypatch.setattr("pipip.sources.git_tag.ensure_url_allowed", AsyncMock())
    process = SimpleNamespace(
        pid=42, communicate=AsyncMock(side_effect=[interrupted(), (b"", b"")])
    )
    launch = AsyncMock(return_value=process)
    kill_group = Mock()
    monkeypatch.setattr("pipip.sources.git_tag.asyncio.create_subprocess_exec", launch)
    monkeypatch.setattr("pipip.sources.git_tag.os.killpg", kill_group)

    if interrupted is TimeoutError:
        with pytest.raises(SourceError, match="timed out"):
            await source.list_releases()
    else:
        with pytest.raises(asyncio.CancelledError):
            await source.list_releases()

    assert launch.call_args.kwargs["start_new_session"] is True
    kill_group.assert_called_once_with(process.pid, signal.SIGKILL)
    assert process.communicate.await_count == 2


@pytest.mark.parametrize(
    ("remote", "web_url", "expected"),
    [
        ("https://github.com/nginx/nginx.git", None, None),
        ("https://github.com/nginx/nginx.git", "https://github.com/nginx/nginx", None),
        (
            "ssh://git@github.com/nginx/nginx.git",
            "https://github.com/nginx/nginx/tree/{tag}",
            "https://github.com/nginx/nginx/tree/v1.2%2Fstable",
        ),
        (
            "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
            "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tag/?h={tag}",
            "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tag/?h=v1.2%2Fstable",
        ),
        ("https://git.example.test/project.git", None, None),
        ("ssh://git@git.example.test:2222/team/project.git", None, None),
        (
            "ssh://git@git.example.test:2222/team/project.git",
            "https://code.example.test/team/project/-/tags/{tag}",
            "https://code.example.test/team/project/-/tags/v1.2%2Fstable",
        ),
        (
            "ssh://git@git.example.test/team/project.git",
            "https://code.example.test/project?ref={tag}&view=tag",
            "https://code.example.test/project?ref=v1.2%2Fstable&view=tag",
        ),
        ("ssh://git@git.example.test/project.git", "ssh://git@git.example.test/project", None),
    ],
)
async def test_git_tag_links(
    monkeypatch: pytest.MonkeyPatch, remote: str, web_url: str | None, expected: str | None
) -> None:
    source = GitTagSource(GitTagOptions(url=remote, web_url=web_url), parse_config({}))
    monkeypatch.setattr("pipip.sources.git_tag.ensure_url_allowed", AsyncMock())
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"abc\trefs/tags/v1.2/stable\n", b"")),
    )
    monkeypatch.setattr(
        "pipip.sources.git_tag.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    )

    (release,) = await source.list_releases()
    assert release.url == expected
