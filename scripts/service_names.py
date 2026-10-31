"""Build configs/service-names.json, the cited services' names (judge v6, docs/plans/judge-v6.md,
change 2), from the docs snapshot's page titles.

    uv run python scripts/service_names.py [--check]

Every page title in data/snapshots/aws-docs/pages.jsonl ends in " - " and its guide's name, which
names the service the guide documents. The names are read from there and nowhere else, by the URL
path segment that holds a guide (103 of them), each name marked by its source:

- title: the guide's name as the title ends (the 13 pages under general/ name the service before
  "endpoints and quotas", and are listed by page path, since one segment holds them all); a
  guide that names several services ("AWS WAF, AWS Firewall Manager, AWS Shield Advanced, and ...")
  is split on its commas; a version suffix such as " v2" and a parenthetical that is not an
  abbreviation ("(for Apache Cassandra)") are dropped.
- abbreviation: one a guide's pages, title or text, define in parentheses right after a full name
  ("Amazon Relational Database Service (Amazon RDS)", "AWS Cloud Development Kit (AWS CDK)").
- short: the name without its leading "Amazon " or "AWS ".
- hand: short forms the pages never define (ELB, OpenSearch, EC2, S3), listed in HAND below. A
  hand name that the rest already gives is refused, so the list stays what the pages lack.

A General Reference page takes the names of the guide whose title is its service's full name.
Every segment of the snapshot must get at least one name, or the build refuses. The snapshot is
not in the repository; the file is, with the sha256 of the snapshot it was built from, and --check
rebuilds it and fails if the committed file differs. The judge never sees a URL: the grader reads a
citation's URL path to look the names up (graders/service_names.py), and the file's hash is part
of the judge's version.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SNAPSHOT = Path("data/snapshots/aws-docs/pages.jsonl")
OUT = Path("configs/service-names.json")
PUBLISHER = ("AWS", "Amazon")
GENERAL = "general"
GENERAL_SUFFIX = " endpoints and quotas"
PREFIXES = ("Amazon ", "AWS ")
# Short forms the pages never define, by segment: a service's own name as answers write it, and
# never a feature of the service. The short form without "Amazon " or "AWS " follows by itself.
HAND: dict[str, tuple[str, ...]] = {
    "AWSEC2": ("Amazon EC2",),
    "AWSSimpleQueueService": ("Amazon SQS",),
    "AmazonS3": ("Amazon S3",),
    "STS": ("AWS STS",),
    "amazonglacier": ("S3 Glacier",),
    "bedrock-agentcore": ("AgentCore",),
    "elasticloadbalancing": ("ELB",),
    "firehose": ("Firehose",),
    "fsx": ("Amazon FSx",),
    "opensearch-service": ("OpenSearch",),
    "sagemaker": ("Amazon SageMaker",),
    "sns": ("Amazon SNS",),
    "streams": ("Kinesis",),
    "tag-editor": ("Tag Editor",),
    "waf": ("Shield",),
}
_VERSION_SUFFIX = re.compile(r"\s+v\d+$")
_PARENS = re.compile(r"\s*\(([^()]*)\)")
_ABBREVIATION = re.compile(r"^(?:Amazon |AWS )?[A-Z][A-Za-z0-9]*$")


class Refused(Exception):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _split(guide: str) -> list[str]:
    """A guide's name split into the services it names: a comma list, its last item's leading
    "and" dropped. A name that itself holds "and" ("AWS Identity and Access Management") is one
    name, so "and" alone splits nothing."""
    parts = [p.strip() for p in guide.split(",") if p.strip()]
    return [re.sub(r"^and\s+", "", p) for p in parts]


def _clean(name: str) -> tuple[str, list[str]]:
    """A service name without a version suffix or a parenthetical; the parenthetical, when it is
    an abbreviation, comes back beside it."""
    abbreviations: list[str] = []
    for found in _PARENS.findall(name):
        if _ABBREVIATION.match(found):
            abbreviations.append(found)
    name = _PARENS.sub("", name)
    name = _VERSION_SUFFIX.sub("", name).strip()
    return name, abbreviations


def _short(name: str) -> str | None:
    for prefix in PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix) :]
    return None


class Names:
    """The names of one segment or page, each once, in the order found, with their sources."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}

    def add(self, name: str, source: str) -> None:
        name = name.strip()
        if name and name not in self.entries:
            self.entries[name] = source
            short = _short(name)
            if short and short not in self.entries:
                self.entries[short] = "short"

    def listed(self) -> list[dict[str, str]]:
        return [{"name": n, "source": s} for n, s in self.entries.items()]


def build(snapshot: Path = SNAPSHOT) -> dict[str, Any]:
    rows = [json.loads(line) for line in snapshot.read_text(encoding="utf-8").splitlines() if line]
    segments: dict[str, Names] = defaultdict(Names)
    pages: dict[str, Names] = {}
    heads: dict[str, list[str]] = defaultdict(list)  # each page's title head and its text
    for row in rows:
        path = urlparse(str(row["url"])).path
        segment = next((part for part in path.split("/") if part), "")
        title = str(row["title"]).replace("\N{NO-BREAK SPACE}", " ")
        head, _, guide = title.rpartition(" - ")
        if not guide:
            raise Refused(f"{row['url']}: its title has no guide name after ' - '")
        if segment == GENERAL:
            if not head.endswith(GENERAL_SUFFIX):
                raise Refused(f"{row['url']}: a General Reference page not named by its service")
            names = pages.setdefault(path, Names())
            full, abbreviations = _clean(head.removesuffix(GENERAL_SUFFIX))
            names.add(full, "title")
            for found in abbreviations:
                names.add(found, "abbreviation")
            segments[segment]  # the segment is covered through its pages
            continue
        names = segments[segment]
        for service in _split(guide):
            full, abbreviations = _clean(service)
            names.add(full, "title")
            for found in abbreviations:
                names.add(found, "abbreviation")
        heads[segment] += [head, str(row["text"])]
    # Abbreviations a guide's pages define right after one of the guide's full names.
    for segment, held in heads.items():
        names = segments[segment]
        for full in [n for n, s in list(names.entries.items()) if s == "title"]:
            pattern = re.compile(re.escape(full) + r"\s*\(((?:Amazon |AWS )?[A-Z][A-Za-z0-9]*)\)")
            for head in held:
                for found in pattern.findall(head):
                    names.add(found, "abbreviation")
    for segment, hand in HAND.items():
        if segment not in segments:
            raise Refused(f"HAND names {segment!r}, which is no segment of the snapshot")
        for name in hand:
            if name in segments[segment].entries:
                raise Refused(f"HAND repeats {name!r}, which {segment!r} already has")
            segments[segment].add(name, "hand")
    for held_names in pages.values():  # the guide whose title is its service's full name
        full = next(iter(held_names.entries))
        for held in segments.values():
            if held.entries.get(full) == "title":
                for name, source in held.entries.items():
                    held_names.entries.setdefault(name, source)
    uncovered = sorted(s for s, n in segments.items() if not n.entries and s != GENERAL)
    if uncovered:
        raise Refused(f"segments with no name: {uncovered}")
    return {
        "about": (
            "The cited services' names for judge v6 (docs/plans/judge-v6.md, change 2), by the "
            "URL path segment of the guide that documents them, and by page path for the General "
            "Reference; each name with its source (title, abbreviation, short, hand). Built by "
            "scripts/service_names.py from the docs snapshot's page titles; the judge never sees a "
            "URL, and the grader reads this file by a citation's URL path. Its hash is part of the "
            "judge's version."
        ),
        "snapshot": snapshot.as_posix(),
        "snapshot_sha256": sha256_file(snapshot),
        "pages_in_snapshot": len(rows),
        "publisher": list(PUBLISHER),
        "segments": {
            segment: segments[segment].listed()
            for segment in sorted(segments)
            if segment != GENERAL
        },
        "pages": {path: pages[path].listed() for path in sorted(pages)},
    }


def render(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    try:
        data = build()
    except (Refused, FileNotFoundError, KeyError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    text = render(data)
    if "--check" in argv:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT} differs from a fresh build of the snapshot", file=sys.stderr)
            return 1
        print(f"{OUT} is the snapshot's build")
        return 0
    OUT.write_text(text, encoding="utf-8")
    names = sum(len(held) for held in data["segments"].values())
    print(
        f"written: {OUT} with {len(data['segments'])} segments ({names} names) and "
        f"{len(data['pages'])} General Reference pages"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
