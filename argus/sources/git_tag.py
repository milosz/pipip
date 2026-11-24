"""Remote Git tag source using ``git ls-remote`` (no clone required)."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from urllib.parse import quote, urlsplit

from argus.config import AppConfig, GitTagOptions
from argus.core.net import UrlNotAllowed, ensure_url_allowed
from argus.core.plugins import register_source
from argus.core.types import ReleaseInfo
from argus.sources.base import SourceError, SourceProvider


def _tag_link_template(web_url: str | None) -> str | None:
    if not web_url:
        return None
    try:
        web = urlsplit(web_url)
    except ValueError:
        return None
    if (
        web.scheme not in {"http", "https"}
        or not web.hostname
        or web.username is not None
        or "{tag}" in web.netloc
    ):
        return None
    if any("{tag}" in part for part in (web.path, web.query, web.fragment)):
        return web_url
    return None


@register_source("git_tag")
class GitTagSource(SourceProvider):
    """List tags from a remote Git repository without cloning it."""

    name = "git_tag"
    options_model = GitTagOptions

    def __init__(self, options: GitTagOptions, settings: AppConfig) -> None:
        super().__init__(options, settings)
        self.options: GitTagOptions = options
        self.default_timeout = settings.sources.git_tag.timeout_seconds

    async def list_releases(self) -> list[ReleaseInfo]:
        try:
            await ensure_url_allowed(
                self.options.url,
                allow_private=self.settings.security.allow_private_networks,
                schemes=frozenset({"https", "ssh", "git"}),
            )
        except UrlNotAllowed as error:
            raise SourceError(str(error)) from error
        env = os.environ.copy()
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        env.setdefault(
            "GIT_SSH_COMMAND",
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes",
        )
        env.setdefault("GIT_ALLOW_PROTOCOL", "https:ssh:git")
        command = [
            "git",
            "-c",
            "credential.helper=",
            "ls-remote",
            "--tags",
            "--refs",
            self.options.url,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError as error:
            raise SourceError("git executable not found; install git") from error
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except (TimeoutError, asyncio.CancelledError) as error:
            # Git can spawn transport helpers that share its stdout and stderr.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.communicate()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise SourceError(
                f"git ls-remote timed out after {self.timeout:.0f}s"
            ) from error
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip() or "unknown git error"
            raise SourceError(f"git ls-remote failed: {message}")

        releases: dict[str, ReleaseInfo] = {}
        prefix = "refs/tags/"
        tag_link_template = _tag_link_template(self.options.web_url)
        for line in stdout.decode(errors="replace").splitlines():
            sha, _, ref = line.partition("\t")
            if not ref.startswith(prefix):
                continue
            tag = ref[len(prefix) :]
            if not tag or tag in releases:
                continue
            releases[tag] = ReleaseInfo(
                version=tag,
                url=(
                    tag_link_template.replace("{tag}", quote(tag, safe=""))
                    if tag_link_template else None
                ),
                digest=sha,
                metadata={"ref": ref, "commit_sha": sha},
            )
        return list(releases.values())
