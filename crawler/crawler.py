#!/usr/bin/env python3
"""Ministerien-Job-Monitor crawler.

Architecture: one Adapter per source *system* (Interamt, SuccessFactors, generic HTML).
Each ministry is only a registry entry pointing at a system + parameters.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from bs4 import BeautifulSoup

from models import Job, SourceConfig
from storage import JobStore, utcnow_iso

USER_AGENT = (
    "StellenMonitor/0.1 "
    "(+https://github.com/philipp-hartmann-hub/Stellenauschreibungen; "
    "personal research crawler; contact via GitHub issues)"
)
DEFAULT_TIMEOUT = 30.0
PAUSE_BETWEEN_SOURCES = 1.0

log = logging.getLogger("crawler")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class HttpClient:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"},
            follow_redirects=True,
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.client.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.client.post(url, **kwargs)


def make_uid(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return digest


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text or None


def parse_de_date(value: str | None) -> str | None:
    """Normalize common German / SF date strings to ISO date if possible."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value


# ---------------------------------------------------------------------------
# Adapter base
# ---------------------------------------------------------------------------


class Adapter(ABC):
    name: str

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    @abstractmethod
    def fetch(self, source: SourceConfig) -> list[Job]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Interamt
# ---------------------------------------------------------------------------


class InteramtAdapter(Adapter):
    """Interamt public JSON feed used by embed widgets.

    Preferred: params.partner_id (or integration_id) →
      GET https://interamt.de/koop/app/webservice_v2?partner={id}

    Official REST API at gate.interamt.de/interamtApi requires X-API-KEY
    (see api-doc). Optional via env INTERAMT_API_KEY + params.use_official_api —
    not used by default because keys are mandant-scoped.

    HTML fallback: params.trefferliste_url or partner-filtered trefferliste.
    """

    name = "interamt"
    WEBSERVICE = "https://interamt.de/koop/app/webservice_v2"
    DETAIL = "https://interamt.de/koop/app/stelle?id={id}"
    TREFFER = "https://interamt.de/koop/app/trefferliste?0&partner={partner}"

    def fetch(self, source: SourceConfig) -> list[Job]:
        partner = (
            source.params.get("partner_id")
            or source.params.get("integration_id")
            or source.params.get("partner")
        )
        if partner:
            try:
                return self._from_webservice(source, str(partner))
            except Exception as exc:
                log.warning(
                    "%s: webservice_v2 failed (%s) — trying HTML trefferliste",
                    source.id,
                    exc,
                )
                return self._from_trefferliste(source, str(partner))

        treffer_url = source.params.get("trefferliste_url")
        if treffer_url:
            return self._from_trefferliste(source, partner=None, url=str(treffer_url))

        raise ValueError(
            f"{source.id}: Interamt requires params.partner_id / integration_id "
            "or trefferliste_url (official API needs mandant API key — TODO)"
        )

    def _from_webservice(self, source: SourceConfig, partner: str) -> list[Job]:
        url = f"{self.WEBSERVICE}?partner={partner}"
        resp = self.http.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        items = data.get("Stellenangebote") or []
        jobs: list[Job] = []
        for item in items:
            job_id = str(item.get("Id") or "")
            title = clean_text(item.get("StellenBezeichnung"))
            if not job_id or not title:
                continue
            orts = item.get("StellenangebotOrt") or []
            location = None
            if orts:
                o = orts[0]
                location = clean_text(
                    " ".join(x for x in [o.get("PLZ"), o.get("Ort")] if x)
                )
            daten = item.get("Daten") or {}
            jobs.append(
                Job(
                    uid=make_uid("interamt", job_id),
                    title=title,
                    url=self.DETAIL.format(id=job_id),
                    location=location,
                    posted_at=parse_de_date(daten.get("Eingestellt")),
                    deadline=parse_de_date(daten.get("Bewerbungsfrist")),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw={"partner": partner, "item": item},
                )
            )
        return jobs

    def _from_trefferliste(
        self,
        source: SourceConfig,
        partner: str | None,
        url: str | None = None,
    ) -> list[Job]:
        page_url = url or self.TREFFER.format(partner=partner)
        resp = self.http.get(page_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.select_one("table.ia-e-table--searchresults")
        if not table:
            log.warning("%s: no Interamt result table at %s", source.id, page_url)
            return []

        jobs: list[Job] = []
        for row in table.select("tbody tr"):
            cells = row.select("td")
            if len(cells) < 3:
                continue
            id_text = cells[0].get_text(" ", strip=True)
            m = re.search(r"(\d{5,})", id_text)
            if not m:
                continue
            job_id = m.group(1)
            title = clean_text(cells[2].get_text(" ", strip=True))
            if not title:
                continue
            location = None
            if len(cells) > 5:
                location = clean_text(cells[5].get_text(" ", strip=True))
            posted = clean_text(cells[8].get_text(" ", strip=True)) if len(cells) > 8 else None
            deadline = clean_text(cells[9].get_text(" ", strip=True)) if len(cells) > 9 else None
            jobs.append(
                Job(
                    uid=make_uid("interamt", job_id),
                    title=title,
                    url=self.DETAIL.format(id=job_id),
                    location=location,
                    posted_at=parse_de_date(posted),
                    deadline=parse_de_date(deadline),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw={"partner": partner, "row_text": row.get_text(" ", strip=True)[:500]},
                )
            )
        # TODO: Wicket pagination — currently only first page when many results
        return jobs


# ---------------------------------------------------------------------------
# SuccessFactors RMK career sites
# ---------------------------------------------------------------------------


class SuccessFactorsAdapter(Adapter):
    """SAP SuccessFactors Recruiting Marketing job search API.

    Verified endpoint (BMUKN):
      POST {base_url}/services/recruiting/v1/jobs
      body: {"locale":"de_DE","pageNumber":0,"keywords":"","location":"","sortBy":"recent"}
    """

    name = "successfactors"

    def fetch(self, source: SourceConfig) -> list[Job]:
        base = (source.params.get("base_url") or "").rstrip("/")
        if not base:
            raise ValueError(f"{source.id}: successfactors requires params.base_url")
        locale = source.params.get("locale") or "de_DE"
        page_size_hint = int(source.params.get("page_size") or 50)

        jobs: list[Job] = []
        page = 0
        while True:
            body = {
                "locale": locale,
                "keywords": source.params.get("keywords", ""),
                "location": source.params.get("location", ""),
                "pageNumber": page,
                "sortBy": source.params.get("sort_by") or "recent",
            }
            resp = self.http.post(
                f"{base}/services/recruiting/v1/jobs",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            total = int(data.get("totalJobs") or 0)
            results = data.get("jobSearchResult") or []
            if not results:
                break
            for entry in results:
                item = entry.get("response") or entry
                title = clean_text(item.get("unifiedStandardTitle") or item.get("title"))
                job_id = str(item.get("id") or "")
                if not title or not job_id:
                    continue
                slug = item.get("unifiedUrlTitle") or item.get("urlTitle") or job_id
                url = f"{base}/job/{slug}/{job_id}"
                locs = item.get("jobLocationShort") or []
                if isinstance(locs, list):
                    location = clean_text(
                        "; ".join(BeautifulSoup(str(x), "lxml").get_text(" ", strip=True) for x in locs)
                    )
                else:
                    location = clean_text(str(locs))
                jobs.append(
                    Job(
                        uid=make_uid("sf", base, job_id),
                        title=title,
                        url=url,
                        location=location,
                        posted_at=parse_de_date(item.get("unifiedStandardStart")),
                        deadline=parse_de_date(item.get("unifiedStandardEnd")),
                        source_id=source.id,
                        source_name=source.name,
                        ebene=source.ebene,
                        land=source.land,
                        adapter=self.name,
                        raw=item,
                    )
                )
            page += 1
            if len(jobs) >= total or len(results) < 1:
                break
            if page > 50:
                log.warning("%s: SF pagination safety stop", source.id)
                break
            time.sleep(0.4)
            _ = page_size_hint  # reserved for future pageSize if API accepts it
        return jobs


# ---------------------------------------------------------------------------
# Generic HTML (config-driven)
# ---------------------------------------------------------------------------


class GenericHtmlAdapter(Adapter):
    """Config-driven list-page scraper.

    Required params:
      list_url: page to fetch
      item_selector: CSS selector for each job card/row
      title_selector: relative selector for title (optional if link text is title)
      link_selector: relative selector for anchor (default: a[href])

    Optional:
      location_selector, date_selector, deadline_selector
      base_url: override for resolving relative links
      title_from: "text" | "link" | "attr:..." 
    """

    name = "generic_html"

    def fetch(self, source: SourceConfig) -> list[Job]:
        list_url = source.params.get("list_url")
        item_sel = source.params.get("item_selector")
        if not list_url or not item_sel:
            raise ValueError(
                f"{source.id}: generic_html requires params.list_url and item_selector"
            )
        link_sel = source.params.get("link_selector") or "a[href]"
        title_sel = source.params.get("title_selector")
        location_sel = source.params.get("location_selector")
        date_sel = source.params.get("date_selector")
        deadline_sel = source.params.get("deadline_selector")
        base_url = source.params.get("base_url") or list_url

        resp = self.http.get(str(list_url))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for item in soup.select(str(item_sel)):
            # Item itself may be the anchor (e.g. table a[href*=...]).
            if item.name == "a" and item.get("href"):
                link_el = item
            else:
                link_el = item.select_one(link_sel) if link_sel else None
            if not link_el or not link_el.get("href"):
                continue
            href = urljoin(str(base_url), link_el["href"])
            if href in seen_urls:
                continue
            # skip non-job anchors (sort controls etc.)
            if source.params.get("link_must_contain"):
                if source.params["link_must_contain"] not in href:
                    continue
            seen_urls.add(href)

            title = None
            if title_sel:
                t_el = item.select_one(str(title_sel))
                title = clean_text(t_el.get_text(" ", strip=True) if t_el else None)
            if not title:
                title = clean_text(link_el.get_text(" ", strip=True))
            if not title or title.lower() in {"jetzt lesen", "mehr", "details"}:
                # last resort: slug from URL
                slug = urlparse(href).path.rstrip("/").split("/")[-2:-1] or urlparse(href).path.rstrip("/").split("/")[-1:]
                title = clean_text(slug[0].replace("-", " ")) if slug else None
            if not title:
                continue

            location = None
            if location_sel:
                el = item.select_one(str(location_sel))
                location = clean_text(el.get_text(" ", strip=True) if el else None)
            posted = None
            if date_sel:
                el = item.select_one(str(date_sel))
                posted = parse_de_date(clean_text(el.get_text(" ", strip=True) if el else None))
            deadline = None
            if deadline_sel:
                el = item.select_one(str(deadline_sel))
                deadline = parse_de_date(clean_text(el.get_text(" ", strip=True) if el else None))

            jobs.append(
                Job(
                    uid=make_uid("html", source.id, href),
                    title=title,
                    url=href,
                    location=location,
                    posted_at=posted,
                    deadline=deadline,
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw={"list_url": list_url},
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# Registry + orchestration
# ---------------------------------------------------------------------------


ADAPTERS: dict[str, type[Adapter]] = {
    "interamt": InteramtAdapter,
    "successfactors": SuccessFactorsAdapter,
    "generic_html": GenericHtmlAdapter,
}


def load_registry(path: Path) -> list[SourceConfig]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources: list[SourceConfig] = []
    for raw in data.get("sources") or []:
        sources.append(
            SourceConfig(
                id=str(raw["id"]),
                name=str(raw["name"]),
                ebene=str(raw.get("ebene") or "bund"),
                land=raw.get("land"),
                type=str(raw["type"]),
                params=dict(raw.get("params") or {}),
                enabled=bool(raw.get("enabled", True)),
                notes=raw.get("notes"),
            )
        )
    return sources


def dedupe(jobs: list[Job]) -> list[Job]:
    by_uid: dict[str, Job] = {}
    for job in jobs:
        by_uid[job.uid] = job
    return list(by_uid.values())


def run_crawl(
    registry_path: Path,
    db_path: Path,
    json_path: Path | None = None,
    source_filter: set[str] | None = None,
) -> int:
    sources = load_registry(registry_path)
    http = HttpClient()
    store = JobStore(db_path)
    run_id = store.start_run()

    adapters = {name: cls(http) for name, cls in ADAPTERS.items()}
    all_jobs: list[Job] = []
    ok = 0
    failed = 0
    notes: list[str] = []
    crawled_source_ids: list[str] = []

    try:
        for source in sources:
            if not source.enabled:
                continue
            if source_filter and source.id not in source_filter:
                continue
            adapter = adapters.get(source.type)
            if not adapter:
                msg = f"{source.id}: unknown adapter type '{source.type}' — skipped"
                log.error(msg)
                notes.append(msg)
                failed += 1
                continue
            try:
                log.info("Fetching %s via %s …", source.id, source.type)
                jobs = adapter.fetch(source)
                jobs = dedupe(jobs)
                log.info("  → %d jobs", len(jobs))
                all_jobs.extend(jobs)
                crawled_source_ids.append(source.id)
                ok += 1
            except Exception as exc:
                failed += 1
                msg = f"{source.id}: {exc}"
                log.exception("Source failed: %s", source.id)
                notes.append(msg)
            time.sleep(PAUSE_BETWEEN_SOURCES)

        seen_at = utcnow_iso()
        unique = dedupe(all_jobs)
        touched = store.upsert_jobs(unique, seen_at=seen_at)
        deactivated = store.mark_missing_inactive(
            crawled_source_ids, touched, seen_at=seen_at
        )
        log.info(
            "Upserted %d jobs; deactivated %d stale rows",
            len(touched),
            deactivated,
        )
        if json_path:
            n = store.export_json(json_path, active_only=True)
            log.info("Wrote %d active jobs to %s", n, json_path)

        store.finish_run(
            run_id,
            jobs_seen=len(touched),
            sources_ok=ok,
            sources_failed=failed,
            notes="; ".join(notes)[:2000],
        )
    finally:
        http.close()
        store.close()

    return 0 if failed == 0 or ok > 0 else 1


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    repo_root = here.parent
    parser = argparse.ArgumentParser(description="Crawl ministry job listings")
    parser.add_argument(
        "--registry",
        type=Path,
        default=here / "registry.yaml",
        help="Path to registry.yaml",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=repo_root / "data" / "jobs.sqlite",
        help="SQLite database path",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=repo_root / "data" / "jobs.json",
        help="Optional JSON export path (active jobs)",
    )
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help="Comma-separated source ids to crawl",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    return run_crawl(args.registry, args.db, args.json, source_filter=only)


if __name__ == "__main__":
    sys.exit(main())
