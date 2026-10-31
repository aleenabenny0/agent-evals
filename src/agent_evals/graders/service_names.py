"""The cited services' names (judge v6, docs/plans/judge-v6.md, changes 1, 2 and 4).

The claim judge never sees a URL, so it cannot know which service the quotes it reads document.
Three of judge v6's code rules need that: a term that is the cited service's own name is
downgraded and scored rather than failed, a flag on that name inside words of no claim is cleared
at verdict time, and the names count leaves it out. The names come from configs/service-names.json,
which scripts/service_names.py builds from the docs snapshot's page titles, each name marked by its
source; the grader reads a citation's URL path to find the service, and nothing of the URL reaches
the judge. The file's hash is part of the judge's version.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_evals.harness.cache import canonical_json, sha256_text

SERVICE_NAMES_PATH = Path("configs/service-names.json")
# The file as the repository holds it, for a command run from another working directory (the
# probe report, for one, is written where the command runs): the path above is tried first.
_REPOSITORY_COPY = Path(__file__).resolve().parents[3] / SERVICE_NAMES_PATH


@dataclass(frozen=True)
class ServiceNames:
    """The listed names by URL path segment (one guide each) and, for the pages of the General
    Reference, by page path, plus the publisher's names, which every answer may use."""

    publisher: tuple[str, ...]
    segments: Mapping[str, tuple[str, ...]]
    pages: Mapping[str, tuple[str, ...]]
    sha256: str  # of the file's canonical JSON: part of the judge's version

    @classmethod
    def load(cls, path: Path | None = None) -> ServiceNames:
        """The file at `path`, or the repository's configs/service-names.json: from the working
        directory when it is there, else the copy beside this package's source."""
        if path is None:
            path = SERVICE_NAMES_PATH if SERVICE_NAMES_PATH.is_file() else _REPOSITORY_COPY
        return cls.from_data(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ServiceNames:
        def names(entries: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
            return tuple(dict.fromkeys(str(entry["name"]) for entry in entries))

        return cls(
            publisher=tuple(str(name) for name in raw["publisher"]),
            segments={str(seg): names(held) for seg, held in dict(raw["segments"]).items()},
            pages={str(path): names(held) for path, held in dict(raw.get("pages", {})).items()},
            sha256=sha256_text(canonical_json(dict(raw))),
        )

    def for_url(self, url: str) -> tuple[str, ...]:
        """The listed names of the service this page documents: by the page's path when the file
        lists it (the General Reference), else by the first segment of the path; none for an
        unknown page, whose flags then go to the skeptic as in judge v5."""
        path = urlparse(url).path
        if path in self.pages:
            return self.pages[path]
        segment = next((part for part in path.split("/") if part), "")
        found = self.segments.get(segment)
        if found is None:
            folded = segment.casefold()
            found = next((v for k, v in self.segments.items() if k.casefold() == folded), ())
        return found

    def cited(self, urls: Iterable[str]) -> list[str]:
        """The listed names of the services these citations document, then the publisher's,
        each once, in order."""
        found: dict[str, None] = {}
        for url in urls:
            for name in self.for_url(url):
                found.setdefault(name, None)
        for name in self.publisher:
            found.setdefault(name, None)
        return list(found)
