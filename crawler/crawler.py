#!/usr/bin/env python3
"""Ministerien-Job-Monitor crawler.

Architecture: one Adapter per source *system* (Interamt, SuccessFactors, generic HTML).
Each ministry is only a registry entry pointing at a system + parameters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from abc import ABC, abstractmethod
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

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

    def _title_excluded(self, title: str, source: SourceConfig) -> bool:
        needles = [
            clean_text(n) or "" for n in (source.params.get("exclude_title_contains") or [])
        ]
        needles = [n for n in needles if n]
        if not needles:
            return False
        hay = (clean_text(title) or "").lower()
        return any(n.lower() in hay for n in needles)

    def _behoerde_matches(self, behoerde: str, source: SourceConfig) -> bool:
        exact = clean_text(source.params.get("behoerde_equals"))
        if exact:
            return behoerde == exact
        equals = [
            clean_text(n) or "" for n in (source.params.get("behoerde_equals_any") or [])
        ]
        equals = [n for n in equals if n]
        if equals:
            return behoerde in equals
        needle = clean_text(source.params.get("behoerde_contains"))
        if needle:
            return needle.lower() in behoerde.lower()
        return True

    def _from_webservice(self, source: SourceConfig, partner: str) -> list[Job]:
        url = f"{self.WEBSERVICE}?partner={partner}"
        resp = self.http.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        items = data.get("Stellenangebote") or []
        jobs: list[Job] = []
        for item in items:
            behoerde = clean_text(item.get("Behoerde")) or ""
            if not self._behoerde_matches(behoerde, source):
                continue
            job_id = str(item.get("Id") or "")
            title = clean_text(item.get("StellenBezeichnung"))
            if not job_id or not title:
                continue
            if self._title_excluded(title, source):
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
                    raw={"partner": partner, "item": item, "behoerde": behoerde},
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
            if not title or title.lower() in {"jetzt lesen", "mehr", "details", "hier"}:
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
# MUZ Global Jobboard Client (GJB / HR-JSON search API)
# ---------------------------------------------------------------------------


class GjbAdapter(Adapter):
    """MUZ Global Jobboard Client: POST {api_base}search/jobs (legacy) or search/ (HR-XML)."""

    name = "gjb"
    _hrxml_items_cache: dict[str, list[dict]] | None = None

    def _resolve_job_url(self, desc: dict, portal_base: str) -> str:
        short = desc.get("PositionShortURI")
        if short:
            return urljoin(portal_base, str(short))
        job_id = desc.get("ID")
        if job_id:
            return urljoin(portal_base, f"index.php?ac=jobad&id={job_id}")
        uri = desc.get("PositionURI")
        if uri and str(uri).startswith("http"):
            return str(uri)
        return portal_base

    def _location_from_desc(self, desc: dict) -> str | None:
        locs = desc.get("PositionLocation") or []
        cities: list[str] = []
        for loc in locs if isinstance(locs, list) else []:
            if not isinstance(loc, dict):
                continue
            city = clean_text(loc.get("CityName"))
            if city and city not in cities:
                cities.append(city)
        return "; ".join(cities) if cities else None

    def _matches_org(self, desc: dict, source: SourceConfig) -> bool:
        org_needle = clean_text(source.params.get("organization_contains"))
        parent_needle = clean_text(source.params.get("parent_organization_contains"))
        parent_needles = [
            clean_text(n) or ""
            for n in (source.params.get("parent_organization_needles") or [])
        ]
        parent_needles = [n for n in parent_needles if n]
        parent_org_ids = [
            str(i) for i in (source.params.get("parent_organization_ids") or []) if i
        ]
        has_id_filter = bool(parent_org_ids)
        has_text_filter = bool(org_needle or parent_needle or parent_needles)
        if not has_id_filter and not has_text_filter:
            return True
        if parent_org_ids:
            parent_id = str(desc.get("ParentOrganization") or "")
            if parent_id not in parent_org_ids:
                return False
        org = (clean_text(desc.get("OrganizationName")) or "").lower()
        parent = (clean_text(desc.get("ParentOrganizationName")) or "").lower()
        hay = f"{org} {parent}"
        if org_needle:
            needle = org_needle.lower()
            if needle not in org and needle not in parent:
                return False
        if parent_needles:
            if not any(n.lower() in hay for n in parent_needles):
                return False
        elif parent_needle and parent_needle.lower() not in parent:
            return False
        return True

    def _request_headers(self, source: SourceConfig) -> dict[str, str]:
        portal_base = str(
            source.params.get("portal_base")
            or str(source.params.get("api_base") or "").replace("/api", "")
        ).rstrip("/")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        referer = clean_text(source.params.get("referer")) or (
            f"{portal_base}/" if portal_base else None
        )
        origin = clean_text(source.params.get("origin")) or (
            portal_base if portal_base else None
        )
        if referer:
            headers["Referer"] = referer
        if origin:
            headers["Origin"] = origin
        return headers

    def _items_to_jobs(
        self, items: list[dict], source: SourceConfig, portal_base: str
    ) -> list[Job]:
        jobs: list[Job] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            desc = item.get("MatchedObjectDescriptor") or item
            if not isinstance(desc, dict):
                continue
            if not self._matches_org(desc, source):
                continue
            title = clean_text(desc.get("PositionTitle"))
            job_id = str(desc.get("ID") or item.get("MatchedObjectId") or "")
            if not title or not job_id:
                continue
            channel = desc.get("PublicationChannel") or []
            posted_at = None
            if isinstance(channel, list) and channel:
                ch0 = channel[0]
                if isinstance(ch0, dict) and ch0.get("StartDate"):
                    posted_at = parse_de_date(str(ch0["StartDate"]))
            if not posted_at:
                posted_at = parse_de_date(desc.get("PublicationStartDate"))
            jobs.append(
                Job(
                    uid=make_uid("gjb", portal_base, job_id),
                    title=title,
                    url=self._resolve_job_url(desc, portal_base),
                    location=self._location_from_desc(desc),
                    posted_at=posted_at,
                    deadline=parse_de_date(desc.get("PublicationEndDate")),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=desc,
                )
            )
        return jobs

    def _load_hrxml_items(
        self,
        source: SourceConfig,
        api_base: str,
        search_path: str,
        language: str,
        search_criterion: Any,
        page_size: int,
    ) -> list[dict]:
        cache_key = f"{api_base}|{search_path}|{language}|{json.dumps(search_criterion, sort_keys=True)}"
        if (
            GjbAdapter._hrxml_items_cache is not None
            and cache_key in GjbAdapter._hrxml_items_cache
        ):
            return GjbAdapter._hrxml_items_cache[cache_key]

        search_url = f"{api_base}/{search_path}"
        headers = self._request_headers(source)
        items: list[dict] = []
        first_item = 1
        total: int | None = None
        while first_item <= 5000:
            payload: dict[str, Any] = {
                "LanguageCode": language,
                "SearchParameters": {
                    "FirstItem": first_item,
                    "CountItem": page_size,
                    "Sort": [
                        {"Criterion": "PublicationStartDate", "Direction": "DESC"}
                    ],
                },
            }
            if search_criterion is not None:
                payload["SearchCriteria"] = search_criterion
            resp = self.http.post(search_url, json=payload, headers=headers)
            resp.raise_for_status()
            sr = resp.json().get("SearchResult") or {}
            if total is None:
                total = int(sr.get("SearchResultCountAll") or sr.get("SearchResultCount") or 0)
            batch = sr.get("SearchResultItems") or []
            if not batch:
                break
            items.extend(batch)
            if total <= first_item + page_size - 1 or len(batch) < page_size:
                break
            first_item += page_size
            time.sleep(0.3)

        if GjbAdapter._hrxml_items_cache is None:
            GjbAdapter._hrxml_items_cache = {}
        GjbAdapter._hrxml_items_cache[cache_key] = items
        return items

    def fetch(self, source: SourceConfig) -> list[Job]:
        api_base = str(source.params.get("api_base") or "").rstrip("/")
        if not api_base:
            raise ValueError(f"{source.id}: gjb requires params.api_base")
        portal_base = str(source.params.get("portal_base") or api_base.replace("/api", "")).rstrip("/") + "/"
        search_format = str(source.params.get("search_format") or "legacy").lower()
        default_path = "search/" if search_format == "hrxml" else "search/jobs"
        search_path = str(source.params.get("search_path") or default_path).lstrip("/")
        page_size = int(source.params.get("page_size") or 50)
        language = str(source.params.get("language_code") or "DE")
        search_criterion = source.params.get("search_criterion")

        if search_format == "hrxml":
            items = self._load_hrxml_items(
                source, api_base, search_path, language, search_criterion, page_size
            )
            return self._items_to_jobs(items, source, portal_base)

        search_url = f"{api_base}/{search_path}"
        headers = self._request_headers(source)
        jobs: list[Job] = []
        page = 1
        total = None
        while page <= 30:
            payload: dict[str, Any] = {
                "LanguageCode": language,
                "SearchResultPage": page,
                "SearchResultPageSize": page_size,
            }
            if search_criterion is not None:
                payload["SearchCriteria"] = search_criterion
            resp = self.http.post(search_url, json=payload, headers=headers)
            if resp.status_code >= 400 and page == 1:
                resp = self.http.get(search_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            sr = data.get("SearchResult") or {}
            if total is None:
                total = int(sr.get("SearchResultCountAll") or sr.get("SearchResultCount") or 0)
            items = sr.get("SearchResultItems") or []
            if not items:
                break
            jobs.extend(self._items_to_jobs(items, source, portal_base))
            if total <= page * page_size or len(items) < page_size:
                break
            page += 1
            time.sleep(0.3)
        return jobs


# ---------------------------------------------------------------------------
# karriere.bayern.de / sei-dabay.de (POST job-postings.json)
# ---------------------------------------------------------------------------


class KarriereByAdapter(Adapter):
    """Landesportal Bayern: POST job-postings.json, Pagination ?&D={page}."""

    name = "karriere_by"
    API = "https://sei-dabay.de/job-postings.json"
    DETAIL = "https://sei-dabay.de/stellenangebot/{id}"
    REFERER = "https://sei-dabay.de/offene-stellen"
    _postings_cache: dict[str, dict] | None = None

    def _api_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Referer": self.REFERER,
            "X-Requested-With": "XMLHttpRequest",
        }

    def _load_postings(self) -> dict[str, dict]:
        if KarriereByAdapter._postings_cache is not None:
            return KarriereByAdapter._postings_cache
        seen: dict[str, dict] = {}
        page = 0
        empty_streak = 0
        while page < 50:
            resp = self.http.post(
                f"{self.API}?&D={page}",
                json={},
                headers=self._api_headers(),
            )
            resp.raise_for_status()
            items = resp.json().get("jobPostings") or []
            added = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("uid") or item.get("api_id") or "")
                if key and key not in seen:
                    seen[key] = item
                    added += 1
            if added == 0:
                empty_streak += 1
            else:
                empty_streak = 0
            if empty_streak >= 3 and page > 5:
                break
            page += 1
            time.sleep(0.3)
        KarriereByAdapter._postings_cache = seen
        return seen

    def _matches_authority(self, authority: str, source: SourceConfig) -> bool:
        auth = (authority or "").lower()
        exact = clean_text(source.params.get("authority"))
        contains = clean_text(source.params.get("authority_contains"))
        authorities = source.params.get("authorities") or []
        if exact:
            return auth == exact.lower()
        needles: list[str] = []
        if contains:
            needles.append(contains)
        needles.extend(clean_text(a) or "" for a in authorities)
        needles = [n for n in needles if n]
        if not needles:
            raise ValueError(
                f"{source.id}: karriere_by requires params.authority, "
                "authority_contains or authorities"
            )
        return any(n.lower() in auth for n in needles)

    def fetch(self, source: SourceConfig) -> list[Job]:
        jobs: list[Job] = []
        for item in self._load_postings().values():
            authority = clean_text(item.get("tendering_authority_name")) or ""
            if not self._matches_authority(authority, source):
                continue
            title = clean_text(item.get("title"))
            job_id = str(item.get("api_id") or item.get("uid") or "")
            if not title or not job_id:
                continue
            url = item.get("application_url") or self.DETAIL.format(id=job_id)
            if not str(url).startswith("http"):
                url = self.DETAIL.format(id=job_id)
            deadline = None
            valid = item.get("valid_through")
            if isinstance(valid, (int, float)) and valid > 0:
                deadline = datetime.utcfromtimestamp(int(valid)).date().isoformat()
            elif valid:
                deadline = parse_de_date(str(valid))
            posted_at = None
            posted = item.get("date_posted")
            if isinstance(posted, (int, float)) and posted > 0:
                posted_at = datetime.utcfromtimestamp(int(posted)).date().isoformat()
            elif posted:
                posted_at = parse_de_date(str(posted))
            jobs.append(
                Job(
                    uid=make_uid("by", job_id),
                    title=title,
                    url=str(url),
                    location=clean_text(item.get("location_city")),
                    deadline=deadline,
                    posted_at=posted_at,
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# karriere.bremen.de (sixcms Job-Konfigurator, session filter)
# ---------------------------------------------------------------------------


class KarriereHbAdapter(Adapter):
    """Land Bremen: karriere.bremen.de, POST dienststellen[] then paginate skip/max."""

    name = "karriere_hb"

    def _parse_frist(self, text: str | None) -> str | None:
        if not text:
            return None
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
        return parse_de_date(m.group(1)) if m else None

    def _parse_teaser_items(self, html: str, portal: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        items: list[dict] = []
        for block in soup.select("div.teaser_item"):
            link = block.select_one('a[href*="detail.php"]')
            if not link or not link.get("href"):
                continue
            href = urljoin(portal + "/", link["href"])
            title_el = block.select_one(".titel")
            title = clean_text(title_el.get_text(" ", strip=True) if title_el else None)
            if not title:
                title = clean_text(link.get("aria-label"))
            if not title:
                continue
            q = urlparse(href).query
            idj = (parse_qs(q).get("idj") or [None])[0]
            job_id = idj or href
            dienst_el = block.select_one(".dienststelle")
            datum_el = block.select_one(".datum")
            bwfrist_el = block.select_one(".bwfrist")
            regio_el = block.select_one(".regio")
            items.append(
                {
                    "id": str(job_id),
                    "title": title,
                    "url": href,
                    "employer": clean_text(
                        dienst_el.get_text(" ", strip=True) if dienst_el else None
                    ),
                    "location": clean_text(
                        regio_el.get_text(" ", strip=True) if regio_el else None
                    ),
                    "posted_at": self._parse_frist(
                        datum_el.get_text(" ", strip=True) if datum_el else None
                    ),
                    "deadline": self._parse_frist(
                        bwfrist_el.get_text(" ", strip=True) if bwfrist_el else None
                    ),
                }
            )
        return items

    def _total_count(self, html: str) -> int | None:
        m = re.search(r"Anzahl der Einträge[^\(]*\((\d+)\)", html)
        return int(m.group(1)) if m else None

    def fetch(self, source: SourceConfig) -> list[Job]:
        dienststelle_id = source.params.get("dienststelle_id")
        if dienststelle_id is None:
            raise ValueError(f"{source.id}: karriere_hb requires params.dienststelle_id")
        portal = str(source.params.get("portal_base") or "https://www.karriere.bremen.de").rstrip("/")
        list_slug = str(source.params.get("list_slug") or "stellenangebote-34126")
        list_url = f"{portal}/{list_slug}"
        page_size = int(source.params.get("page_size") or 100)

        bootstrap = self.http.get(f"{list_url}?skip=0&max=10")
        bootstrap.raise_for_status()
        form = BeautifulSoup(bootstrap.text, "lxml").find("form", id="jkForm")
        if not form:
            raise ValueError(f"{source.id}: karriere_hb: job filter form not found")
        form_id_el = form.select_one('input[name="id"]')
        csrf_el = form.select_one('input[name="csrf_job"]')
        if not form_id_el or not csrf_el:
            raise ValueError(f"{source.id}: karriere_hb: csrf/id missing in filter form")

        post = self.http.post(
            list_url,
            data={
                "id": form_id_el.get("value", ""),
                "csrf_job": csrf_el.get("value", ""),
                "dienststellen[]": str(dienststelle_id),
                "submit": "Suchen",
            },
        )
        post.raise_for_status()

        jobs: list[Job] = []
        seen: set[str] = set()
        skip = 0
        total = self._total_count(post.text)
        while skip <= 2000:
            resp = self.http.get(list_url, params={"skip": skip, "max": page_size})
            resp.raise_for_status()
            if total is None:
                total = self._total_count(resp.text)
            batch = self._parse_teaser_items(resp.text, portal)
            if not batch:
                break
            for item in batch:
                if item["url"] in seen:
                    continue
                seen.add(item["url"])
                jobs.append(
                    Job(
                        uid=make_uid("hb", item["id"]),
                        title=item["title"],
                        url=item["url"],
                        location=item["location"] or item["employer"],
                        posted_at=item["posted_at"],
                        deadline=item["deadline"],
                        source_id=source.id,
                        source_name=source.name,
                        ebene=source.ebene,
                        land=source.land,
                        adapter=self.name,
                        raw=item,
                    )
                )
            if total is not None and len(seen) >= total:
                break
            if len(batch) < page_size:
                break
            skip += page_size
            time.sleep(0.3)
        return jobs


# ---------------------------------------------------------------------------
# karriere.baden-wuerttemberg.de JSON API
# ---------------------------------------------------------------------------


class KarriereBwAdapter(Adapter):
    """Landesportal BW: GET /api/job-search (shape verified in prior probe)."""

    name = "karriere_bw"
    SEARCH = "https://karriere.baden-wuerttemberg.de/api/job-search"

    def fetch(self, source: SourceConfig) -> list[Job]:
        resort = source.params.get("resort_id")
        if resort is None:
            raise ValueError(f"{source.id}: karriere_bw requires params.resort_id")
        jobs: list[Job] = []
        page = 1
        while page <= 40:
            resp = self.http.get(
                self.SEARCH,
                params={"page": page, "per_page": 50, "filter.resort": str(resort)},
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 405:
                resp = self.http.post(
                    self.SEARCH,
                    json={"page": page, "per_page": 50, "filter": {"resort": str(resort)}},
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
            resp.raise_for_status()
            data = resp.json()
            listings = data.get("listings") or []
            for item in listings:
                title = clean_text(item.get("title"))
                if not title or title.upper().startswith("VORLAGE"):
                    continue
                job_id = str(item.get("id") or "")
                url = item.get("url") or ""
                if not job_id or not url:
                    continue
                jobs.append(
                    Job(
                        uid=make_uid("bw", job_id),
                        title=title,
                        url=url,
                        location=clean_text(item.get("location")),
                        deadline=parse_de_date(item.get("application_deadline")),
                        source_id=source.id,
                        source_name=source.name,
                        ebene=source.ebene,
                        land=source.land,
                        adapter=self.name,
                        raw=item,
                    )
                )
            pagination = data.get("pagination") or {}
            total_pages = int(pagination.get("total_pages") or page)
            if page >= total_pages or not listings:
                break
            page += 1
            time.sleep(0.3)
        return jobs


# ---------------------------------------------------------------------------
# karriereportal-stellen.berlin.de (Rexx / Finest Jobs)
# ---------------------------------------------------------------------------


class KarriereBeAdapter(Adapter):
    """Berliner Landesportal: HTML table#joboffers, filter[client_id], start-Pagination."""

    name = "karriere_be"
    BASE = "https://www.karriereportal-stellen.berlin.de/stellenangebote.html"

    def fetch(self, source: SourceConfig) -> list[Job]:
        client_ids = source.params.get("client_ids") or []
        if not client_ids:
            raise ValueError(f"{source.id}: karriere_be requires params.client_ids")
        page_size = int(source.params.get("page_size") or 20)
        jobs: list[Job] = []
        seen: set[str] = set()
        start = 0
        while start <= 2000:
            query = [f"filter[client_id][{cid}]={cid}" for cid in client_ids]
            if start:
                query.append(f"start={start}")
            url = f"{self.BASE}?{'&'.join(query)}"
            resp = self.http.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            rows = soup.select(
                "table#joboffers tr.alternative_0, table#joboffers tr.alternative_1"
            )
            if not rows:
                break
            for row in rows:
                link_el = row.select_one("td.real_table_col1 a")
                if not link_el or not link_el.get("href"):
                    continue
                href = urljoin(self.BASE, link_el["href"])
                if href in seen:
                    continue
                seen.add(href)
                m = re.search(r"-j(\d+)\.html", href, re.I)
                job_id = m.group(1) if m else href
                title = clean_text(link_el.get_text(" ", strip=True))
                if not title:
                    continue
                emp_el = row.select_one("td.real_table_col2")
                employer = clean_text(emp_el.get_text(" ", strip=True) if emp_el else None)
                dl_el = row.select_one("td.real_table_col5")
                deadline = parse_de_date(
                    clean_text(dl_el.get_text(" ", strip=True) if dl_el else None)
                )
                loc_el = row.select_one("td.real_table_col3")
                location = clean_text(loc_el.get_text(" ", strip=True) if loc_el else None)
                jobs.append(
                    Job(
                        uid=make_uid("be", job_id),
                        title=title,
                        url=href,
                        location=location or employer,
                        deadline=deadline,
                        source_id=source.id,
                        source_name=source.name,
                        ebene=source.ebene,
                        land=source.land,
                        adapter=self.name,
                        raw={"client_ids": client_ids, "employer": employer},
                    )
                )
            if len(rows) < page_size:
                break
            start += page_size
            time.sleep(0.3)
        return jobs


# ---------------------------------------------------------------------------
# karriere-in-brandenburg.de (Craft CMS, GET /api/offers)
# ---------------------------------------------------------------------------


class KarriereBbAdapter(Adapter):
    """Landesportal BB: GET /api/offers liefert JSON-Array (Aug 2026)."""

    name = "karriere_bb"
    OFFERS = "https://karriere-in-brandenburg.de/api/offers"
    DETAIL = "https://karriere-in-brandenburg.de/stellenangebote/{id}"
    _offers_cache: list[dict] | None = None

    def _load_offers(self) -> list[dict]:
        if KarriereBbAdapter._offers_cache is not None:
            return KarriereBbAdapter._offers_cache
        resp = self.http.get(self.OFFERS, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(f"karriere_bb: unexpected offers payload: {type(data)}")
        KarriereBbAdapter._offers_cache = data
        return data

    def fetch(self, source: SourceConfig) -> list[Job]:
        employer = clean_text(source.params.get("employer"))
        employer_contains = clean_text(source.params.get("employer_contains"))
        if not employer and not employer_contains:
            raise ValueError(
                f"{source.id}: karriere_bb requires params.employer or employer_contains"
            )
        offer_type = source.params.get("offer_type", 92)
        jobs: list[Job] = []
        for item in self._load_offers():
            if not isinstance(item, dict):
                continue
            if offer_type is not None and item.get("type") != offer_type:
                continue
            emp = clean_text(item.get("employer")) or ""
            if employer and emp != employer:
                continue
            if employer_contains and employer_contains.lower() not in emp.lower():
                continue
            title = clean_text(item.get("title"))
            job_id = str(item.get("id") or "")
            if not title or not job_id:
                continue
            vacancy = clean_text(item.get("vacancy"))
            deadline = None
            if vacancy and vacancy != "-":
                deadline = parse_de_date(vacancy) or vacancy
            online = item.get("onlineDate")
            posted_at = None
            if isinstance(online, (int, float)) and online > 0:
                posted_at = datetime.utcfromtimestamp(int(online)).date().isoformat()
            elif online:
                posted_at = parse_de_date(str(online))
            jobs.append(
                Job(
                    uid=make_uid("bb", job_id),
                    title=title,
                    url=self.DETAIL.format(id=job_id),
                    location=clean_text(item.get("city")),
                    deadline=deadline,
                    posted_at=posted_at,
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# karriere-in-mv.de (server-rendered Stellenliste)
# ---------------------------------------------------------------------------


class KarriereMvAdapter(Adapter):
    """MV: karriere-in-mv.de, HTML-Liste uebersicht/stelle, Filter employer_name exakt."""

    name = "karriere_mv"
    DEFAULT_LIST = "https://karriere-in-mv.de/uebersicht/stelle"
    DEFAULT_BASE = "https://karriere-in-mv.de/"
    _listings_cache: list[dict] | None = None

    def _load_listings(self, source: SourceConfig) -> list[dict]:
        if KarriereMvAdapter._listings_cache is not None:
            return KarriereMvAdapter._listings_cache
        list_url = str(source.params.get("list_url") or self.DEFAULT_LIST)
        base_url = str(source.params.get("base_url") or self.DEFAULT_BASE)
        resp = self.http.get(list_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        listings: list[dict] = []
        seen_urls: set[str] = set()
        for li in soup.select("li"):
            title_el = li.select_one("a.uk-accordion-title")
            link_el = li.select_one('a[href*="stelle/"]')
            block = li.select_one(".uk-width-2-3")
            employer_el = block.select_one("b") if block else None
            if not title_el or not link_el or not employer_el:
                continue
            href = urljoin(base_url, link_el["href"])
            if href in seen_urls:
                continue
            seen_urls.add(href)
            employer = clean_text(employer_el.get_text(" ", strip=True)) or ""
            location = None
            if block:
                loc_match = re.search(
                    r"in\s+(\d{5}\s+.+)$",
                    clean_text(block.get_text(" ", strip=True)) or "",
                )
                if loc_match:
                    location = clean_text(loc_match.group(1))
            slug = urlparse(href).path.rstrip("/").split("/")[-1]
            job_id_match = re.match(r"(\d+)", slug or "")
            job_id = job_id_match.group(1) if job_id_match else slug
            listings.append(
                {
                    "title": clean_text(title_el.get_text(" ", strip=True)),
                    "employer": employer,
                    "location": location,
                    "url": href,
                    "job_id": job_id,
                }
            )
        KarriereMvAdapter._listings_cache = listings
        return listings

    def _matches_employer(self, employer: str, source: SourceConfig) -> bool:
        names = [clean_text(n) or "" for n in (source.params.get("employer_names") or [])]
        exact = clean_text(source.params.get("employer_name"))
        if exact:
            names.append(exact)
        names = [n for n in names if n]
        if not names:
            raise ValueError(
                f"{source.id}: karriere_mv requires params.employer_name or employer_names"
            )
        emp = clean_text(employer) or ""
        return emp in names

    def fetch(self, source: SourceConfig) -> list[Job]:
        jobs: list[Job] = []
        for item in self._load_listings(source):
            employer = item.get("employer") or ""
            if not self._matches_employer(employer, source):
                continue
            title = clean_text(item.get("title"))
            job_id = str(item.get("job_id") or "")
            url = str(item.get("url") or "")
            if not title or not job_id or not url:
                continue
            jobs.append(
                Job(
                    uid=make_uid("mv", job_id),
                    title=title,
                    url=url,
                    location=clean_text(item.get("location")),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# karriere.niedersachsen.de (webservice.jobboerse + Neos GraphQL)
# ---------------------------------------------------------------------------


class KarriereNiAdapter(Adapter):
    """Niedersachsen: webservice.jobboerse.niedersachsen.de/data/SearchStellen."""

    name = "karriere_ni"
    SEARCH_API = "https://webservice.jobboerse.niedersachsen.de/data/SearchStellen"
    DEFAULT_PORTAL = "https://karriere.niedersachsen.de"
    DEFAULT_GRAPHQL_NODE = "6d0f9b17-ec6e-4d56-9d97-a05f2baad299"
    _postings_cache: list[dict] | None = None
    _slug_cache: dict[str, str] | None = None

    def _load_postings(self, source: SourceConfig) -> list[dict]:
        if KarriereNiAdapter._postings_cache is not None:
            return KarriereNiAdapter._postings_cache
        api_url = str(source.params.get("search_api") or self.SEARCH_API)
        timeout = float(source.params.get("request_timeout") or 120)
        portal_base = str(source.params.get("portal_base") or self.DEFAULT_PORTAL).rstrip("/")
        resp = self.http.get(
            api_url,
            headers={
                "Origin": portal_base,
                "Referer": f"{portal_base}/Stellenangebote.html",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError("karriere_ni: unexpected SearchStellen payload")
        stellen_typ = str(source.params.get("stellen_typ_id") or "1")
        items = [item for item in data if str(item.get("StellenTypId") or "") == stellen_typ]
        KarriereNiAdapter._postings_cache = items
        return items

    def _load_slugs(self, source: SourceConfig) -> dict[str, str]:
        if KarriereNiAdapter._slug_cache is not None:
            return KarriereNiAdapter._slug_cache
        portal_base = str(source.params.get("portal_base") or self.DEFAULT_PORTAL).rstrip("/")
        node_id = str(source.params.get("graphql_node_id") or self.DEFAULT_GRAPHQL_NODE)
        query = (
            "{ node(identifier: \""
            + node_id
            + "\") { childNodes(filter: \"Marktplatz.NDSKP:Document.JobOffer\") { properties } } }"
        )
        resp = self.http.post(
            f"{portal_base}/graphql",
            json={"query": query},
            headers={"Content-Type": "application/json"},
            timeout=float(source.params.get("request_timeout") or 120),
        )
        resp.raise_for_status()
        nodes = resp.json().get("data", {}).get("node", {}).get("childNodes") or []
        slugs: dict[str, str] = {}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            props = node.get("properties") or {}
            offer_id = props.get("offerId")
            segment = clean_text(props.get("uriPathSegment"))
            if offer_id and segment:
                slugs[str(offer_id)] = segment
        KarriereNiAdapter._slug_cache = slugs
        return slugs

    def _matches_employer(self, employer: str, source: SourceConfig) -> bool:
        names = [clean_text(n) or "" for n in (source.params.get("employer_names") or [])]
        exact = clean_text(source.params.get("employer_name"))
        if exact:
            names.append(exact)
        names = [n.strip() for n in names if n]
        if not names:
            raise ValueError(
                f"{source.id}: karriere_ni requires params.employer_name or employer_names"
            )
        emp = (clean_text(employer) or "").strip()
        return emp in names

    def _resolve_job_url(self, job: dict, slugs: dict[str, str], portal_base: str) -> str:
        job_id = str(job.get("StelleId") or "")
        segment = slugs.get(job_id)
        if segment:
            return f"{portal_base}/stellenausschreibungen/{segment}.html"
        return f"{portal_base}/Stellenangebote.html"

    def fetch(self, source: SourceConfig) -> list[Job]:
        portal_base = str(source.params.get("portal_base") or self.DEFAULT_PORTAL).rstrip("/")
        slugs = self._load_slugs(source)
        jobs: list[Job] = []
        for item in self._load_postings(source):
            if not isinstance(item, dict):
                continue
            employer = clean_text(item.get("DienststelleBezeichnung")) or ""
            if not self._matches_employer(employer, source):
                continue
            title = clean_text(item.get("Kurzbezeichnung"))
            job_id = str(item.get("StelleId") or "")
            if not title or not job_id:
                continue
            location_parts = [
                clean_text(item.get("DienststellePlz")),
                clean_text(item.get("DienststelleOrt")),
            ]
            location = " ".join(p for p in location_parts if p) or None
            jobs.append(
                Job(
                    uid=make_uid("ni", job_id),
                    title=title,
                    url=self._resolve_job_url(item, slugs, portal_base),
                    location=location,
                    deadline=parse_de_date(clean_text(item.get("BewerbungBis"))),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# karriere.nrw (REST API, kein serverseitiger Dienststellen-Filter)
# ---------------------------------------------------------------------------


class KarriereNrwAdapter(Adapter):
    """NRW: api.karriere.nrw/v1/jobs/stellenausschreibungen/.

    Filter: exakter Match auf ausschreibende_behoerde und dienststelle.benennung_dienststelle
    (behoerde allein umfasst nachgeordnete Dienststellen mit gleicher Dienststellen-UUID).
    """

    name = "karriere_nrw"
    API = "https://api.karriere.nrw/v1/jobs/stellenausschreibungen/"
    DEFAULT_PORTAL = "https://karriere.nrw"
    DEFAULT_CONCURRENCY = 12
    _postings_cache: list[dict] | None = None

    def _fetch_page(
        self, page: int, api_url: str, timeout: float, portal_base: str
    ) -> list[dict]:
        resp = self.http.get(
            api_url,
            params={"page": page},
            headers={
                "Origin": portal_base,
                "Referer": f"{portal_base}/",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results")
        if not isinstance(results, list):
            raise ValueError(f"karriere_nrw: unexpected page {page} payload")
        return results

    def _load_postings(self, source: SourceConfig) -> list[dict]:
        if KarriereNrwAdapter._postings_cache is not None:
            return KarriereNrwAdapter._postings_cache
        api_url = str(source.params.get("api_url") or self.API)
        portal_base = str(source.params.get("portal_base") or self.DEFAULT_PORTAL).rstrip("/")
        timeout = float(source.params.get("request_timeout") or 60)
        concurrency = int(source.params.get("concurrency") or self.DEFAULT_CONCURRENCY)
        resp = self.http.get(
            api_url,
            headers={
                "Origin": portal_base,
                "Referer": f"{portal_base}/",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        meta = resp.json()
        total = int(meta.get("count") or 0)
        first_page = meta.get("results") or []
        page_size = len(first_page) if first_page else 10
        pages = max(1, (total + page_size - 1) // page_size)
        items: list[dict] = list(first_page)
        if pages > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(self._fetch_page, page, api_url, timeout, portal_base): page
                    for page in range(2, pages + 1)
                }
                for future in as_completed(futures):
                    items.extend(future.result())
        if total and len(items) < total:
            log.warning(
                "karriere_nrw: expected %d postings, got %d (%d pages)",
                total,
                len(items),
                pages,
            )
        KarriereNrwAdapter._postings_cache = items
        log.info("karriere_nrw: loaded %d postings (%d pages)", len(items), pages)
        return items

    def _employer_names(self, source: SourceConfig) -> list[str]:
        names = [clean_text(n) or "" for n in (source.params.get("employer_names") or [])]
        exact = clean_text(source.params.get("employer_name"))
        if exact:
            names.append(exact)
        names = [n.strip() for n in names if n]
        if not names:
            raise ValueError(
                f"{source.id}: karriere_nrw requires params.employer_name or employer_names"
            )
        return names

    def _matches_ministry_job(self, item: dict, source: SourceConfig) -> bool:
        names = self._employer_names(source)
        contracting = (clean_text(item.get("ausschreibende_behoerde")) or "").strip()
        if contracting not in names:
            return False
        dienststelle = item.get("dienststelle") or {}
        if not isinstance(dienststelle, dict):
            return False
        dept_name = (clean_text(dienststelle.get("benennung_dienststelle")) or "").strip()
        if dept_name not in names:
            return False
        expected_uuid = clean_text(source.params.get("dienststelle_uuid"))
        if expected_uuid:
            actual_uuid = str(dienststelle.get("uuid") or "")
            if actual_uuid != expected_uuid:
                return False
        return True

    def _title_excluded(self, title: str, source: SourceConfig) -> bool:
        needles = [clean_text(n) or "" for n in (source.params.get("exclude_title_contains") or [])]
        needles = [n for n in needles if n]
        if not needles:
            return False
        hay = (clean_text(title) or "").lower()
        return any(n.lower() in hay for n in needles)

    def fetch(self, source: SourceConfig) -> list[Job]:
        portal_base = str(source.params.get("portal_base") or self.DEFAULT_PORTAL).rstrip("/")
        jobs: list[Job] = []
        for item in self._load_postings(source):
            if not isinstance(item, dict):
                continue
            if not self._matches_ministry_job(item, source):
                continue
            title = clean_text(item.get("titel_der_stelle") or item.get("bezeichnung"))
            if not title or self._title_excluded(title, source):
                continue
            job_id = str(item.get("uuid") or "")
            if not job_id:
                continue
            location = clean_text(item.get("ort") or item.get("address_display"))
            jobs.append(
                Job(
                    uid=make_uid("nrw", job_id),
                    title=title,
                    url=f"{portal_base}/stellenausschreibung/{job_id}",
                    location=location,
                    deadline=parse_de_date(clean_text(item.get("bewerbungsfrist"))),
                    posted_at=parse_de_date(clean_text(item.get("erscheinungsdatum"))),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# stellensuche.hessen.de (SAP OData zer_cand_unreg_srv)
# ---------------------------------------------------------------------------


class KarriereHeAdapter(Adapter):
    """Hessen: SAP HCM OData PostingAbstractSet auf stellensuche.hessen.de."""

    name = "karriere_he"
    DEFAULT_API = (
        "https://stellensuche.hessen.de/sap/opu/odata/sap/zer_cand_unreg_srv"
    )
    DEFAULT_PORTAL = (
        "https://stellensuche.hessen.de/sap/bc/ui5_ui5/sap/zer5_ccu"
    )
    _postings_cache: list[dict] | None = None

    def _parse_sap_date(self, value: str | None) -> str | None:
        if not value:
            return None
        match = re.search(r"/Date\((\d+)\)/", str(value))
        if match:
            return datetime.utcfromtimestamp(int(match.group(1)) / 1000).date().isoformat()
        return parse_de_date(str(value))

    def _load_postings(self, source: SourceConfig) -> list[dict]:
        if KarriereHeAdapter._postings_cache is not None:
            return KarriereHeAdapter._postings_cache
        api_base = str(source.params.get("api_base") or self.DEFAULT_API).rstrip("/")
        page_size = int(source.params.get("page_size") or 500)
        timeout = float(source.params.get("request_timeout") or 120)
        resp = self.http.get(
            f"{api_base}/PostingAbstractSet",
            params={"$format": "json", "$top": page_size},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json().get("d", {}).get("results") or []
        if not isinstance(items, list):
            raise ValueError("karriere_he: unexpected PostingAbstractSet payload")
        KarriereHeAdapter._postings_cache = items
        return items

    def _matches_department(self, dept_name: str, source: SourceConfig) -> bool:
        names = [clean_text(n) or "" for n in (source.params.get("department_names") or [])]
        exact = clean_text(source.params.get("department_name"))
        if exact:
            names.append(exact)
        names = [n for n in names if n]
        if not names:
            raise ValueError(
                f"{source.id}: karriere_he requires params.department_name or department_names"
            )
        dept = clean_text(dept_name) or ""
        return dept in names

    def fetch(self, source: SourceConfig) -> list[Job]:
        portal_base = str(
            source.params.get("portal_base") or self.DEFAULT_PORTAL
        ).rstrip("/")
        jobs: list[Job] = []
        for item in self._load_postings(source):
            if not isinstance(item, dict):
                continue
            dept = item.get("Department") or {}
            if not isinstance(dept, dict):
                continue
            dept_name = clean_text(dept.get("Name")) or ""
            if not self._matches_department(dept_name, source):
                continue
            title = clean_text(item.get("Header"))
            guid = str(item.get("Guid") or "")
            if not title or not guid:
                continue
            jobs.append(
                Job(
                    uid=make_uid("he", guid),
                    title=title,
                    url=f"{portal_base}/index.html#/Stellendetail/{guid}",
                    location=clean_text(dept.get("City")),
                    posted_at=self._parse_sap_date(item.get("DatePostingBegin")),
                    deadline=self._parse_sap_date(
                        item.get("DateApplicationEnd") or item.get("DatePostingEnd")
                    ),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# karriere.sachsen.de REST (stellenanzeige + institutionId)
# ---------------------------------------------------------------------------


class KarriereSachsenAdapter(Adapter):
    """Sachsen: GET karriere-verwaltung/rest/stellenanzeige?institutionId=…

    institutionId filtert serverseitig auf genau diese Institution (nicht den
    gesamten Geschäftsbereich) — damit nur Stellen im Ministerium selbst.
    """

    name = "karriere_sachsen"
    DEFAULT_API = "https://www.karriere.sachsen.de/karriere-verwaltung/rest/stellenanzeige"
    DEFAULT_PORTAL = "https://www.karriere.sachsen.de"
    DEFAULT_PAGE_SIZE = 100

    def _parse_iso_date(self, value: str | None) -> str | None:
        if not value:
            return None
        text = str(value).strip()
        # API liefert z. B. 2026-08-20T00:00:00.000Z+01:00
        match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
        return match.group(1) if match else parse_de_date(text)

    def _location(self, item: dict) -> str | None:
        places = item.get("placeOfEmployments") or []
        if isinstance(places, list):
            cities = [clean_text(p.get("city")) for p in places if isinstance(p, dict)]
            cities = [c for c in cities if c]
            if cities:
                return ", ".join(dict.fromkeys(cities))
        return None

    def fetch(self, source: SourceConfig) -> list[Job]:
        inst = source.params.get("institution_id")
        if inst is None or str(inst).strip() == "":
            raise ValueError(f"{source.id}: karriere_sachsen requires params.institution_id")
        inst_id = int(inst)
        api_url = str(source.params.get("api_url") or self.DEFAULT_API)
        portal = str(source.params.get("portal_base") or self.DEFAULT_PORTAL).rstrip("/")
        page_size = int(source.params.get("page_size") or self.DEFAULT_PAGE_SIZE)
        timeout = float(source.params.get("request_timeout") or 60)

        items: list[dict] = []
        offset = 0
        while True:
            resp = self.http.get(
                api_url,
                params={
                    "institutionId": inst_id,
                    "offset": offset,
                    "limit": page_size,
                    "sortBy": "ReleaseDate",
                    "sortOrder": "DESC",
                },
                headers={"Accept": "application/json", "Origin": portal, "Referer": f"{portal}/"},
                timeout=timeout,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list):
                raise ValueError(f"karriere_sachsen: unexpected payload for {source.id}")
            if not batch:
                break
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < page_size:
                break
            offset += page_size

        jobs: list[Job] = []
        for item in items:
            # Serverseitiger Filter reicht i. d. R.; zusätzlich exakte institution-ID.
            if item.get("institution") is not None and int(item["institution"]) != inst_id:
                continue
            title = clean_text(item.get("title"))
            job_id = str(item.get("id") or "")
            if not title or not job_id:
                continue
            jobs.append(
                Job(
                    uid=make_uid("sn", job_id),
                    title=title,
                    url=f"{portal}/karriere/stellenanzeige.jsp?id={job_id}",
                    location=self._location(item),
                    deadline=self._parse_iso_date(clean_text(item.get("bewerbungEnde"))),
                    posted_at=self._parse_iso_date(clean_text(item.get("bewerbungStart"))),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# karriere.sachsen-anhalt.de (TYPO3 wv_lpsa, HTML-Liste)
# ---------------------------------------------------------------------------


class KarriereStAdapter(Adapter):
    """Sachsen-Anhalt: karriere.sachsen-anhalt.de/stellenangebote.

    Eine HTML-Liste (p.vacancy); Filter über exakten Arbeitgeber-Namen
    (erste Zeile der Karte) — nur Stellen im Ministerium selbst.
    """

    name = "karriere_st"
    DEFAULT_LIST = "https://karriere.sachsen-anhalt.de/stellenangebote"
    DEFAULT_BASE = "https://karriere.sachsen-anhalt.de"
    _listings_cache: list[dict] | None = None

    def _load_listings(self, source: SourceConfig) -> list[dict]:
        if KarriereStAdapter._listings_cache is not None:
            return KarriereStAdapter._listings_cache
        list_url = str(source.params.get("list_url") or self.DEFAULT_LIST)
        base_url = str(source.params.get("base_url") or self.DEFAULT_BASE).rstrip("/") + "/"
        resp = self.http.get(list_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        listings: list[dict] = []
        seen: set[str] = set()
        for vac in soup.select("p.vacancy"):
            link = vac.select_one('a[href*="tx_wvlpsa"]')
            if not link:
                continue
            href = urljoin(base_url, str(link.get("href") or ""))
            if not href or href in seen:
                continue
            seen.add(href)
            lines = [
                ln.strip()
                for ln in vac.get_text("\n", strip=True).split("\n")
                if ln.strip()
            ]
            employer = clean_text(lines[0]) if lines else None
            title = clean_text(link.get_text(" ", strip=True))
            deadline = None
            for ln in lines[1:]:
                if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", ln):
                    deadline = parse_de_date(ln)
                    break
            # ID aus Path-Segment default-<hash> oder Query
            path = urlparse(href).path.rstrip("/")
            segment = path.split("/")[-1]
            job_id = segment
            m = re.search(r"default-([a-f0-9]+)", segment)
            if m:
                job_id = m.group(1)
            if not title or not employer or not job_id:
                continue
            listings.append(
                {
                    "title": title,
                    "employer": employer,
                    "url": href,
                    "job_id": job_id,
                    "deadline": deadline,
                }
            )
        KarriereStAdapter._listings_cache = listings
        log.info("karriere_st: loaded %d listings", len(listings))
        return listings

    def _matches_employer(self, employer: str, source: SourceConfig) -> bool:
        names = [clean_text(n) or "" for n in (source.params.get("employer_names") or [])]
        exact = clean_text(source.params.get("employer_name"))
        if exact:
            names.append(exact)
        names = [n for n in names if n]
        if not names:
            raise ValueError(
                f"{source.id}: karriere_st requires params.employer_name or employer_names"
            )
        return (clean_text(employer) or "") in names

    def _title_excluded(self, title: str, source: SourceConfig) -> bool:
        needles = [
            clean_text(n) or "" for n in (source.params.get("exclude_title_contains") or [])
        ]
        needles = [n for n in needles if n]
        if not needles:
            return False
        hay = (clean_text(title) or "").lower()
        return any(n.lower() in hay for n in needles)

    def fetch(self, source: SourceConfig) -> list[Job]:
        jobs: list[Job] = []
        for item in self._load_listings(source):
            if not self._matches_employer(str(item.get("employer") or ""), source):
                continue
            title = clean_text(item.get("title"))
            job_id = str(item.get("job_id") or "")
            url = str(item.get("url") or "")
            if not title or not job_id or not url:
                continue
            if self._title_excluded(title, source):
                continue
            jobs.append(
                Job(
                    uid=make_uid("st", job_id),
                    title=title,
                    url=url,
                    deadline=item.get("deadline"),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# karriere.sachsen-anhalt.de already above; Schleswig-Holstein central search
# ---------------------------------------------------------------------------


class KarriereShAdapter(Adapter):
    """Schleswig-Holstein: zentrale Stellensuche auf schleswig-holstein.de.

    Filter: exakter Match auf ausschreibende Behörde (p.c-teaser-job__employer).
    Nachgeordnete Behörden haben eigene Behördennamen in der Facette.
    """

    name = "karriere_sh"
    DEFAULT_PORTAL = "https://www.schleswig-holstein.de"
    DEFAULT_LIST = (
        "https://www.schleswig-holstein.de/DE/landesportal/karriere/stellenangebote"
    )
    DEFAULT_FORM = (
        "https://www.schleswig-holstein.de/SiteGlobals/Forms/Stellensuche/"
        "Stellensuche_Formular"
    )
    _listings_cache: list[dict] | None = None

    def _parse_jobs(self, html: str, base: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        jobs: list[dict] = []
        for item in soup.select(".l-search-result__item"):
            teaser = item.select_one(".c-teaser-job")
            if not teaser:
                continue
            title_el = teaser.select_one(
                '[data-testid="teaser-job-stellenbezeichnung"], .c-teaser-job__headline'
            )
            emp_el = teaser.select_one(
                '[data-testid="teaser-job-behoerde"], .c-teaser-job__employer'
            )
            link = teaser.select_one('a[href*="interamt_"]')
            if not title_el or not link:
                continue
            href = urljoin(base, str(link.get("href") or ""))
            m = re.search(r"interamt_(\d+)", href)
            if not m:
                continue
            location = None
            deadline = None
            for lab in teaser.select(".c-teaser-job__label"):
                text = lab.get_text(" ", strip=True)
                if text.startswith("Arbeitsort"):
                    location = clean_text(re.sub(r"^Arbeitsort:\s*", "", text))
                elif text.startswith("Bewerbungsfrist"):
                    raw = re.sub(r"^Bewerbungsfrist:\s*", "", text).strip()
                    deadline = parse_de_date(raw) if re.search(r"\d", raw) else None
            jobs.append(
                {
                    "job_id": m.group(1),
                    "title": clean_text(title_el.get_text(" ", strip=True)),
                    "employer": clean_text(emp_el.get_text(" ", strip=True)) if emp_el else None,
                    "url": href,
                    "location": location,
                    "deadline": deadline,
                }
            )
        return jobs

    def _load_listings(self, source: SourceConfig) -> list[dict]:
        if KarriereShAdapter._listings_cache is not None:
            return KarriereShAdapter._listings_cache
        portal = str(source.params.get("portal_base") or self.DEFAULT_PORTAL).rstrip("/")
        list_url = str(source.params.get("list_url") or self.DEFAULT_LIST)
        form_url = str(source.params.get("form_url") or self.DEFAULT_FORM)
        timeout = float(source.params.get("request_timeout") or 60)

        bootstrap = self.http.get(list_url, timeout=timeout)
        bootstrap.raise_for_status()
        soup = BeautifulSoup(bootstrap.text, "lxml")
        form = None
        for candidate in soup.select("form"):
            action = candidate.get("action") or ""
            if "Stellensuche_Formular" in action:
                form = candidate
                break
        params: dict[str, str] = {}
        action = form_url
        if form:
            action = urljoin(portal + "/", form.get("action") or form_url)
            for inp in form.select("input[name]"):
                params[str(inp["name"])] = str(inp.get("value") or "")
        params["resultsPerPage"] = str(source.params.get("page_size") or 100)

        listings: list[dict] = []
        seen: set[str] = set()
        page_url = action
        for _ in range(20):
            resp = self.http.get(page_url, params=params if page_url == action else None, timeout=timeout)
            resp.raise_for_status()
            batch = self._parse_jobs(resp.text, portal)
            for job in batch:
                jid = job.get("job_id")
                if not jid or jid in seen:
                    continue
                seen.add(str(jid))
                listings.append(job)
            soup = BeautifulSoup(resp.text, "lxml")
            next_page = _ + 2
            next_href = None
            for a in soup.select('a[href*="gtp="]'):
                label = a.get_text(strip=True)
                if label.isdigit() and int(label) == next_page:
                    next_href = a.get("href")
                    break
            if not next_href or not batch:
                break
            page_url = urljoin(portal + "/", next_href)
            params = {}

        KarriereShAdapter._listings_cache = listings
        log.info("karriere_sh: loaded %d listings", len(listings))
        return listings

    def _employer_names(self, source: SourceConfig) -> list[str]:
        names = [clean_text(n) or "" for n in (source.params.get("employer_names") or [])]
        exact = clean_text(source.params.get("employer_name"))
        if exact:
            names.append(exact)
        names = [n for n in names if n]
        if not names:
            raise ValueError(
                f"{source.id}: karriere_sh requires params.employer_name or employer_names"
            )
        return names

    def _title_excluded(self, title: str, source: SourceConfig) -> bool:
        needles = [
            clean_text(n) or "" for n in (source.params.get("exclude_title_contains") or [])
        ]
        needles = [n for n in needles if n]
        if not needles:
            return False
        hay = (clean_text(title) or "").lower()
        return any(n.lower() in hay for n in needles)

    def fetch(self, source: SourceConfig) -> list[Job]:
        names = set(self._employer_names(source))
        jobs: list[Job] = []
        for item in self._load_listings(source):
            employer = clean_text(item.get("employer")) or ""
            if employer not in names:
                continue
            title = clean_text(item.get("title"))
            job_id = str(item.get("job_id") or "")
            url = str(item.get("url") or "")
            if not title or not job_id or not url:
                continue
            if self._title_excluded(title, source):
                continue
            jobs.append(
                Job(
                    uid=make_uid("sh", job_id),
                    title=title,
                    url=url,
                    location=clean_text(item.get("location")),
                    deadline=item.get("deadline"),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# karriere.thueringen.de (Craft CMS, POST sft/job/search-job)
# ---------------------------------------------------------------------------


class KarriereThAdapter(Adapter):
    """Thüringen: zentrale Stellenbörse karriere.thueringen.de.

    Lädt alle Listings per AJAX (CSRF + criteria), filtert clientseitig auf
    exakten company-Namen. Behördenfilter der API ist serverseitig fehlerhaft
    (42S22) — daher Vollscan + Cache.
    """

    name = "karriere_th"
    DEFAULT_PORTAL = "https://karriere.thueringen.de"
    DEFAULT_SEARCH = "https://karriere.thueringen.de/stellensuche"
    _listings_cache: list[dict] | None = None

    def _employer_names(self, source: SourceConfig) -> list[str]:
        names = [clean_text(n) or "" for n in (source.params.get("employer_names") or [])]
        exact = clean_text(source.params.get("employer_name"))
        if exact:
            names.append(exact)
        names = [n for n in names if n]
        if not names:
            raise ValueError(
                f"{source.id}: karriere_th requires params.employer_name or employer_names"
            )
        return names

    def _title_excluded(self, title: str, source: SourceConfig) -> bool:
        needles = [
            clean_text(n) or "" for n in (source.params.get("exclude_title_contains") or [])
        ]
        needles = [n for n in needles if n]
        if not needles:
            return False
        hay = (clean_text(title) or "").lower()
        return any(n.lower() in hay for n in needles)

    def _load_listings(self, source: SourceConfig) -> list[dict]:
        if KarriereThAdapter._listings_cache is not None:
            return KarriereThAdapter._listings_cache
        portal = str(source.params.get("portal_base") or self.DEFAULT_PORTAL).rstrip("/")
        search_url = str(source.params.get("search_url") or self.DEFAULT_SEARCH)
        timeout = float(source.params.get("request_timeout") or 60)

        bootstrap = self.http.get(search_url, timeout=timeout)
        bootstrap.raise_for_status()
        soup = BeautifulSoup(bootstrap.text, "lxml")
        token_el = soup.select_one('input[name="CRAFT_CSRF_TOKEN"]')
        if not token_el or not token_el.get("value"):
            raise RuntimeError(f"{source.id}: CRAFT_CSRF_TOKEN missing on {search_url}")
        csrf = str(token_el["value"])

        listings: list[dict] = []
        seen: set[str] = set()
        pages = 1
        page = 1
        while page <= pages and page <= 80:
            resp = self.http.post(
                portal + "/",
                data={
                    "CRAFT_CSRF_TOKEN": csrf,
                    "action": "sft/job/search-job",
                    "criteria[view]": "list",
                    "criteria[page]": str(page),
                    "criteria[orderBy]": "publishDate",
                },
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": portal,
                    "Referer": search_url,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            if page == 1:
                pages = int(payload.get("pages") or 1)
            for el in payload.get("elements") or []:
                if not isinstance(el, dict):
                    continue
                job_id = str(el.get("id") or "")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                listings.append(
                    {
                        "job_id": job_id,
                        "title": clean_text(el.get("title")),
                        "employer": clean_text(el.get("company")),
                        "url": str(el.get("url") or ""),
                        "location": clean_text(el.get("location")),
                        "deadline": parse_de_date(clean_text(el.get("endTenderDate"))),
                        "posted_at": parse_de_date(clean_text(el.get("date"))),
                        "internal_id": clean_text(el.get("internalId")),
                    }
                )
            page += 1

        KarriereThAdapter._listings_cache = listings
        log.info("karriere_th: loaded %d listings", len(listings))
        return listings

    def fetch(self, source: SourceConfig) -> list[Job]:
        names = set(self._employer_names(source))
        jobs: list[Job] = []
        for item in self._load_listings(source):
            employer = clean_text(item.get("employer")) or ""
            if employer not in names:
                continue
            title = clean_text(item.get("title"))
            job_id = str(item.get("job_id") or "")
            url = str(item.get("url") or "")
            if not title or not job_id or not url:
                continue
            if self._title_excluded(title, source):
                continue
            jobs.append(
                Job(
                    uid=make_uid("th", job_id),
                    title=title,
                    url=url,
                    location=clean_text(item.get("location")),
                    deadline=item.get("deadline"),
                    posted_at=item.get("posted_at"),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
                )
            )
        return jobs


# ---------------------------------------------------------------------------
# karriere.rlp.de (TYPO3 + Solr, tx_rlpjobportal)
# ---------------------------------------------------------------------------


class KarriereRpAdapter(Adapter):
    """Rheinland-Pfalz: zentrale Stellenbörse karriere.rlp.de/im-beruf.

    Listen haben keinen Behördenfilter; Arbeitgeber kommt aus Detailseiten
    (Einsatzdienststelle, Fallback Bewerbungsadresse). Nachgeordnete
    Dienststellen bleiben über Einsatzdienststelle erkennbar und matchen
    nicht auf Ministeriums-Namen.
    """

    name = "karriere_rp"
    DEFAULT_LIST = "https://karriere.rlp.de/im-beruf"
    DEFAULT_PORTAL = "https://karriere.rlp.de"
    DEFAULT_CONCURRENCY = 8
    _MINISTRY_RE = re.compile(
        r"Ministerium|Staatskanzlei|Finanzministerium", re.IGNORECASE
    )
    _SUBORDINATE_RE = re.compile(
        r"Polizei|JVA|Justizvollzug|Hochschule|Landesamt|Landesbetrieb|"
        r"Finanzamt|Forstamt|Gericht|Direktion|Bibliothek|Landesbibliotheks|"
        r"\bLBB\b|\bLBM\b|Dienstleistungszentrum|Spielbank|Gewahrsam|"
        r"Kriminal|Amtsgericht|Landgericht|Oberlandes|Wasserschutz|"
        r"Flughafen|Aufsichts-|ADD\b",
        re.IGNORECASE,
    )
    _listings_cache: list[dict] | None = None

    def _employer_names(self, source: SourceConfig) -> list[str]:
        names = [clean_text(n) or "" for n in (source.params.get("employer_names") or [])]
        exact = clean_text(source.params.get("employer_name"))
        if exact:
            names.append(exact)
        names = [n for n in names if n]
        if not names:
            raise ValueError(
                f"{source.id}: karriere_rp requires params.employer_name or employer_names"
            )
        return names

    def _title_excluded(self, title: str, source: SourceConfig) -> bool:
        needles = [
            clean_text(n) or "" for n in (source.params.get("exclude_title_contains") or [])
        ]
        needles = [n for n in needles if n]
        if not needles:
            return False
        hay = (clean_text(title) or "").lower()
        return any(n.lower() in hay for n in needles)

    def _parse_dl(self, html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "lxml")
        fields: dict[str, str] = {}
        for dt in soup.select("dt"):
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue
            key = clean_text(dt.get_text(" ", strip=True))
            if not key:
                continue
            value = " | ".join(p.strip() for p in dd.stripped_strings if p.strip())
            fields[key] = value
        return fields

    def _resolve_employer(self, einsatz: str | None, address: str | None) -> str | None:
        einsatz_c = clean_text(einsatz) or ""
        if einsatz_c and self._MINISTRY_RE.search(einsatz_c):
            return einsatz_c
        if einsatz_c and self._SUBORDINATE_RE.search(einsatz_c):
            return einsatz_c
        for part in (address or "").split("|"):
            line = clean_text(part) or ""
            if line and self._MINISTRY_RE.search(line):
                return line
        return einsatz_c or None

    def _list_index(self, list_url: str, portal: str, timeout: float) -> list[dict]:
        items: list[dict] = []
        seen: set[str] = set()
        page = 1
        max_page = 1
        while page <= max_page and page <= 50:
            resp = self.http.get(list_url, params={"tx_solr[page]": page}, timeout=timeout)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.select("a[href*='tx_solr']"):
                href = a.get("href") or ""
                m = re.search(r"page(?:%5D|\])=(\d+)", href)
                if m:
                    max_page = max(max_page, int(m.group(1)))
            for entry in soup.select(".results-entry"):
                link = entry.select_one("a[href*='stellenboerse']")
                if not link or not link.get("href"):
                    continue
                href = urljoin(portal + "/", str(link["href"]))
                query = parse_qs(urlparse(href).query)
                job_id = None
                for key, values in query.items():
                    if "stellenboerse]" in key and "action" not in key and "controller" not in key:
                        job_id = values[0] if values else None
                        break
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                title_el = entry.select_one(".results-topic")
                date_el = entry.select_one("time.results-date")
                posted = date_el.get("datetime") if date_el else None
                items.append(
                    {
                        "job_id": job_id,
                        "title": clean_text(title_el.get_text(" ", strip=True)) if title_el else None,
                        "url": href,
                        "posted_at": parse_de_date(clean_text(posted)),
                    }
                )
            page += 1
        return items

    def _enrich_detail(self, item: dict, timeout: float) -> dict:
        resp = self.http.get(str(item["url"]), timeout=timeout)
        resp.raise_for_status()
        fields = self._parse_dl(resp.text)
        einsatz = fields.get("Einsatzdienststelle")
        address = fields.get("Bewerbungsadresse")
        enriched = dict(item)
        enriched.update(
            {
                "einsatzdienststelle": clean_text(einsatz),
                "bewerbungsadresse": clean_text(address),
                "employer": self._resolve_employer(einsatz, address),
                "location": clean_text(fields.get("Arbeitsort")),
                "deadline": parse_de_date(clean_text(fields.get("Ende der Bewerbungsfrist"))),
                "employer_url": clean_text(fields.get("Internetadresse des Arbeitgebers")),
            }
        )
        return enriched

    def _load_listings(self, source: SourceConfig) -> list[dict]:
        if KarriereRpAdapter._listings_cache is not None:
            return KarriereRpAdapter._listings_cache
        portal = str(source.params.get("portal_base") or self.DEFAULT_PORTAL).rstrip("/")
        list_url = str(source.params.get("list_url") or self.DEFAULT_LIST)
        timeout = float(source.params.get("request_timeout") or 60)
        concurrency = int(source.params.get("concurrency") or self.DEFAULT_CONCURRENCY)

        index = self._list_index(list_url, portal, timeout)
        listings: list[dict] = []
        if concurrency <= 1 or len(index) <= 1:
            for item in index:
                listings.append(self._enrich_detail(item, timeout))
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(self._enrich_detail, item, timeout) for item in index]
                for future in as_completed(futures):
                    listings.append(future.result())

        KarriereRpAdapter._listings_cache = listings
        log.info("karriere_rp: loaded %d listings", len(listings))
        return listings

    def fetch(self, source: SourceConfig) -> list[Job]:
        names = set(self._employer_names(source))
        jobs: list[Job] = []
        for item in self._load_listings(source):
            employer = clean_text(item.get("employer")) or ""
            if employer not in names:
                continue
            title = clean_text(item.get("title"))
            job_id = str(item.get("job_id") or "")
            url = str(item.get("url") or "")
            if not title or not job_id or not url:
                continue
            if self._title_excluded(title, source):
                continue
            jobs.append(
                Job(
                    uid=make_uid("rp", job_id),
                    title=title,
                    url=url,
                    location=clean_text(item.get("location")),
                    deadline=item.get("deadline"),
                    posted_at=item.get("posted_at"),
                    source_id=source.id,
                    source_name=source.name,
                    ebene=source.ebene,
                    land=source.land,
                    adapter=self.name,
                    raw=item,
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
    "gjb": GjbAdapter,
    "karriere_bw": KarriereBwAdapter,
    "karriere_be": KarriereBeAdapter,
    "karriere_bb": KarriereBbAdapter,
    "karriere_by": KarriereByAdapter,
    "karriere_hb": KarriereHbAdapter,
    "karriere_he": KarriereHeAdapter,
    "karriere_mv": KarriereMvAdapter,
    "karriere_ni": KarriereNiAdapter,
    "karriere_nrw": KarriereNrwAdapter,
    "karriere_sachsen": KarriereSachsenAdapter,
    "karriere_st": KarriereStAdapter,
    "karriere_sh": KarriereShAdapter,
    "karriere_th": KarriereThAdapter,
    "karriere_rp": KarriereRpAdapter,
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


def _parse_deadline_date(value: str | None) -> date | None:
    """Parse Job.deadline to a calendar date; None if missing or unparseable."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    # Prefer ISO YYYY-MM-DD (and optional time suffix).
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def drop_expired_jobs(
    jobs: list[Job], *, today: date | None = None
) -> tuple[list[Job], int]:
    """Drop postings whose deadline is strictly before today.

    Keeps jobs with missing or unparseable deadlines (e.g. „bis Besetzung“).
    deadline == today stays open.
    """
    cutoff = today or date.today()
    kept: list[Job] = []
    dropped = 0
    for job in jobs:
        deadline = _parse_deadline_date(job.deadline)
        if deadline is not None and deadline < cutoff:
            dropped += 1
            continue
        kept.append(job)
    return kept, dropped


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
    KarriereBbAdapter._offers_cache = None
    KarriereByAdapter._postings_cache = None
    GjbAdapter._hrxml_items_cache = None
    KarriereHeAdapter._postings_cache = None
    KarriereMvAdapter._listings_cache = None
    KarriereNiAdapter._postings_cache = None
    KarriereNiAdapter._slug_cache = None
    KarriereNrwAdapter._postings_cache = None
    KarriereStAdapter._listings_cache = None
    KarriereShAdapter._listings_cache = None
    KarriereThAdapter._listings_cache = None
    KarriereRpAdapter._listings_cache = None

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
        unique, expired = drop_expired_jobs(unique)
        if expired:
            log.info("Dropped %d expired jobs (deadline before today)", expired)
            notes.append(f"expired_dropped={expired}")
        touched = store.upsert_jobs(unique, seen_at=seen_at)
        deactivated = store.mark_missing_inactive(
            crawled_source_ids, touched, seen_at=seen_at
        )
        log.info(
            "Upserted %d jobs; deactivated %d stale rows; expired dropped %d",
            len(touched),
            deactivated,
            expired,
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
