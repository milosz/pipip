"""Atom feeds for releases and current comparison results."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from pipip.config import AppConfig
from pipip.core.comparisons import build_comparison_results
from pipip.core.utils import truncate
from pipip.db.models import Target, VersionRecord
from pipip.db.session import Database, previous_detected_version

_EPOCH = datetime(1970, 1, 1)
_RELEASE_NOTES_LIMIT = 1_200
_XML_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")

_COMPARISON_TITLES = {
    "match": "Versions match",
    "drift": "Versions differ",
    "missing": "Waiting for version data",
}
_TARGET_COMPARISON_LABELS = {
    "match": "matches the reference",
    "ahead": "newer than the reference",
    "behind": "older than the reference",
    "different": "differs from the reference",
    "missing": "comparison unavailable",
}


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _feed(url: str, title: str, updated: datetime) -> Element:
    feed = Element("feed", xmlns="http://www.w3.org/2005/Atom")
    SubElement(feed, "id").text = url
    SubElement(feed, "title").text = _XML_CONTROL_CHARS.sub("", title)
    SubElement(feed, "link", rel="self", href=url)
    SubElement(SubElement(feed, "author"), "name").text = "pi-pip!"
    SubElement(feed, "updated").text = _timestamp(updated)
    return feed


def _add_entry(
    feed: Element,
    *,
    entry_id: str,
    title: str,
    updated: str,
    content: str,
    href: str,
) -> None:
    entry = SubElement(feed, "entry")
    SubElement(entry, "id").text = entry_id
    SubElement(entry, "title").text = _XML_CONTROL_CHARS.sub("", title)
    SubElement(entry, "updated").text = updated
    SubElement(entry, "content", type="text").text = _XML_CONTROL_CHARS.sub("", content)
    SubElement(entry, "link", rel="alternate", href=href)


def _atom_response(feed: Element) -> Response:
    return Response(
        content=tostring(feed, encoding="utf-8", xml_declaration=True),
        media_type="application/atom+xml; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


class AtomOutput:
    def __init__(self, settings: AppConfig, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.config = settings.atom

    def router(self) -> APIRouter:
        router = APIRouter(
            tags=["atom"],
        )

        @router.get("/feed.atom", response_class=Response)
        async def atom_feed(request: Request, group: str | None = None) -> Response:
            query = (
                select(Target.name, VersionRecord, previous_detected_version())
                .join(VersionRecord, Target.id == VersionRecord.application_id)
                .order_by(VersionRecord.detected_at.desc(), VersionRecord.id.desc())
                .limit(self.config.limit)
            )
            if self.config.targets != "all":
                query = query.where(Target.name.in_(self.config.targets))
            if group is not None:
                stored_group = Target.target_config["group"].as_string()
                query = query.where(
                    func.coalesce(func.nullif(stored_group, ""), "Ungrouped") == group
                )
            async with self.database.session() as session:
                rows = (await session.execute(query)).all()

            root_url = request.url_for("atom_feed")
            feed_url = root_url.include_query_params(group=group) if group is not None else root_url
            title = f"{self.settings.name} releases" + (f" · {group}" if group is not None else "")
            feed = _feed(str(feed_url), title, rows[0][1].detected_at if rows else _EPOCH)

            for name, version, previous in rows:
                detected = _timestamp(version.detected_at)
                title = (
                    f"{name}: {previous} → {version.version}"
                    if previous else f"{name}: version {version.version} detected"
                )
                message = (
                    f"pi-pip! detected a change from {previous} to {version.version}."
                    if previous else f"pi-pip! detected version {version.version}."
                )
                if notes := truncate(version.release_notes, _RELEASE_NOTES_LIMIT):
                    message = f"{message}\n\n{notes}"
                _add_entry(
                    feed,
                    entry_id=f"{root_url}#version-{version.id}-{detected}",
                    title=title,
                    updated=detected,
                    content=message,
                    href=str(request.url_for("app_detail", name=name)),
                )

            return _atom_response(feed)

        @router.get("/comparisons.atom", response_class=Response)
        async def comparisons_feed(request: Request) -> Response:
            apps = await self.database.list_targets()
            comparisons = build_comparison_results(self.settings.comparisons, apps)
            changed_at = {
                app.name: app.last_changed_at for app in apps if app.last_changed_at is not None
            }
            entries = []
            for comparison in comparisons:
                names = [comparison.reference, *(target.name for target in comparison.targets)]
                updated = max(
                    (changed_at[name] for name in names if name in changed_at), default=_EPOCH
                )
                entries.append((comparison, updated))

            feed_url = str(request.url_for("comparisons_feed"))
            page_url = request.url_for("comparisons_page")
            feed = _feed(
                feed_url,
                f"{self.settings.name} version comparisons",
                max((updated for _, updated in entries), default=_EPOCH),
            )
            SubElement(feed, "link", rel="alternate", href=str(page_url))
            for comparison, updated in entries:
                state = _COMPARISON_TITLES[comparison.status]
                reference = comparison.reference_version or "unknown"
                lines = [
                    f"Matches: {comparison.matched} of {len(comparison.targets)} targets.",
                    f"Reference: {comparison.reference} ({reference}).",
                ]
                lines.extend(
                    f"{target.name} ({target.version or 'unknown'}): "
                    f"{_TARGET_COMPARISON_LABELS[target.status]}."
                    for target in comparison.targets
                )
                _add_entry(
                    feed,
                    entry_id=f"{feed_url}#{comparison.name}",
                    title=f"{comparison.name}: {state}",
                    updated=_timestamp(updated),
                    content="\n".join(lines),
                    href=f"{page_url}#comparison-{comparison.name}",
                )
            return _atom_response(feed)

        return router
