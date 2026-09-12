"""수집 소스 — GDELT DOC 2.0 artlist 와 RSS.

pipeline/ 이 데이터 소스를 아는 유일한 지점이다. 가중치 신호(weights/)와 달리
수집은 소스 형태가 직접 드러날 수밖에 없으므로, 대신 여기 한 파일에 가둔다.

절대 하지 않는 것: 이용약관이 자동 접근을 금지하는 사이트 스크래핑.
Investing.com 은 공개 API가 없고 약관상 금지다. 대상으로 삼지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import config

#: 링크를 다르게 보이게 만들 뿐 내용은 같은 파라미터들.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "cmpid", "ref", "ref_src",
    "sh", "smid", "partner", "__twitter_impression", "amp", "ito", "ns_campaign",
}


def canonical_url(url: str) -> str:
    """중복 판정의 기준. 여기가 느슨하면 같은 기사를 여러 번 수집한다."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    if not p.netloc:
        return (url or "").strip().lower()

    host = p.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") or host.startswith("amp."):
        host = host.split(".", 1)[1]

    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(p.query, keep_blank_values=False)
            if k.lower() not in TRACKING_PARAMS
        )
    )
    path = p.path.rstrip("/") or "/"
    if path.endswith("/amp"):
        path = path[:-4] or "/"
    return urlunparse(("https", host, path, "", query, ""))


def article_id(url_canonical: str) -> str:
    return hashlib.sha1(url_canonical.encode("utf-8")).hexdigest()[:20]


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _parse_seendate(value: str) -> str | None:
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ", "%Y%m%d%H%M%S"):
        try:
            return (
                datetime.strptime(str(value), fmt)
                .replace(tzinfo=timezone.utc)
                .isoformat(timespec="seconds")
            )
        except (ValueError, TypeError):
            continue
    return None


# ── GDELT ───────────────────────────────────────────────────────────────


class GdeltSource:
    """DOC 2.0 artlist. fixture 를 주면 네트워크를 타지 않는다."""

    def __init__(self, fixture: Path | None = None):
        gd = config.sources().get("gdelt") or {}
        self.endpoint = gd.get("doc_api", "https://api.gdeltproject.org/api/v2/doc/doc")
        self.maxrecords = int(gd.get("maxrecords", 250))
        self.timespan = gd.get("timespan", "6h")
        self.sort = gd.get("sort", "hybridrel")
        self.timeout = int(gd.get("timeout_seconds", 45))
        self.retries = int(gd.get("retries", 3))
        self.fixture = Path(fixture) if fixture else None
        self.calls: list[dict] = []   # --report 와 테스트가 들여다본다

    def params(self, fips: str, theme: str | None, limit: int) -> dict[str, str]:
        query = f"sourcecountry:{fips}"
        if theme:
            query += f" theme:{theme}"
        return {
            "query": query,
            "mode": "artlist",
            # 상한(250)을 명시하지 않으면 기본 75건으로 조용히 떨어진다
            "maxrecords": str(min(limit, self.maxrecords)),
            "timespan": self.timespan,
            "sort": self.sort,
            "format": "json",
        }

    def fetch(self, fips: str, theme: str | None, limit: int) -> list[dict]:
        params = self.params(fips, theme, limit)
        self.calls.append(params)

        if self.fixture is not None:
            raw = json.loads(self.fixture.read_text(encoding="utf-8"))
            # 실제 API 는 sourcecountry:XX 쿼리에 그 나라 기사만 돌려준다.
            # fixture 는 전 국가가 한 파일에 있으므로 여기서 흉내 낸다.
            raw = {
                "articles": [
                    a
                    for a in (raw.get("articles") or [])
                    if not a.get("sourcecountry") or a["sourcecountry"] == fips
                ]
            }
        else:
            raw = self._get(params)
            if raw is None:
                return []
        return self.parse(raw, fips)[:limit]

    def _get(self, params: dict) -> dict | None:
        import requests  # noqa: PLC0415

        for attempt in range(1, self.retries + 1):
            try:
                resp = requests.get(self.endpoint, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[collect] GDELT 실패 ({attempt}/{self.retries}) "
                    f"{params['query']}: {exc}",
                    file=sys.stderr,
                )
        return None

    @staticmethod
    def parse(raw: dict, fips: str) -> list[dict]:
        """artlist 응답 → 기사 dict.

        GDELT 는 본문을 주지 않는다. 제목과 메타데이터만 온다 —
        그래서 사이트에도 요약과 링크만 실을 수 있다.
        """
        out = []
        for item in raw.get("articles") or []:
            url = item.get("url") or ""
            if not url:
                continue
            canonical = canonical_url(url)
            out.append(
                {
                    "id": article_id(canonical),
                    "url": url,
                    "url_canonical": canonical,
                    "domain": item.get("domain") or domain_of(url),
                    "title": (item.get("title") or "").strip(),
                    "lead": "",
                    "language": (item.get("language") or "").strip(),
                    "published_at": _parse_seendate(item.get("seendate")),
                    "country_fips": fips,
                    "source_kind": "gdelt",
                }
            )
        return out


# ── RSS ─────────────────────────────────────────────────────────────────


class RssSource:
    """설정에 적힌 피드만 읽는다. 피드에 달린 FIPS 태그로 국가를 귀속시킨다."""

    def __init__(self, fixture_dir: Path | None = None):
        rss = config.sources().get("rss") or {}
        self.feeds = list(rss.get("feeds") or [])
        self.timeout = int(rss.get("timeout_seconds", 20))
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None

    def fetch_all(self, limit_per_feed: int = 50) -> list[dict]:
        out: list[dict] = []
        for feed in self.feeds:
            try:
                out.extend(self.fetch(feed, limit_per_feed))
            except Exception as exc:  # noqa: BLE001 — 피드 하나가 전체를 죽이면 안 된다
                print(f"[collect] RSS 실패 {feed.get('url')}: {exc}", file=sys.stderr)
        return out

    def fetch(self, feed: dict, limit: int = 50) -> list[dict]:
        fips = (feed.get("fips") or "").upper()
        if fips and fips not in config.known_fips():
            print(
                f"[collect] RSS 피드의 fips '{fips}' 가 countries.yaml 에 없다: "
                f"{feed.get('url')}",
                file=sys.stderr,
            )
            return []

        if self.fixture_dir is not None:
            path = self.fixture_dir / f"{feed.get('name', fips)}.json"
            entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        else:
            import feedparser  # noqa: PLC0415

            parsed = feedparser.parse(feed["url"])
            entries = [
                {
                    "link": e.get("link", ""),
                    "title": e.get("title", ""),
                    "summary": e.get("summary", ""),
                    "published": e.get("published", ""),
                }
                for e in parsed.entries
            ]

        out = []
        for entry in entries[:limit]:
            url = entry.get("link") or ""
            if not url:
                continue
            canonical = canonical_url(url)
            out.append(
                {
                    "id": article_id(canonical),
                    "url": url,
                    "url_canonical": canonical,
                    "domain": feed.get("domain") or domain_of(url),
                    "title": (entry.get("title") or "").strip(),
                    "lead": (entry.get("summary") or "").strip()[:1000],
                    "language": feed.get("language", "en"),
                    "published_at": entry.get("published") or None,
                    "country_fips": fips,
                    "source_kind": "rss",
                }
            )
        return out
