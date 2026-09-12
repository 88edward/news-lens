"""step8 / 정적 사이트 빌드 테스트.

완료 조건:
  - --from-fixtures 로 샘플 데이터만 가지고 site/dist 가 생성된다
  - globe.json 과 country/{ISO2}.json 이 설계서의 계약과 맞는다
  - 사이트에 기사 원문이 실리지 않는다 (요약 + 출처 링크만)
"""
from __future__ import annotations

import json
import re

import pytest

import config
from pipeline import step8_build_site
from store.db import connect

DATE = "2026-09-12"


@pytest.fixture
def built(tmp_path, config_dir):
    out = tmp_path / "dist"
    rc = step8_build_site.main(
        ["--from-fixtures", "--date", DATE, "--out", str(out)]
    )
    assert rc == 0
    return out


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── 산출물 ──────────────────────────────────────────────────────────────


def test_fixtures만으로_dist가_생성된다(built):
    assert (built / "index.html").exists()
    assert (built / "data" / "globe.json").exists()
    assert (built / "static" / "globe.js").exists()
    assert (built / "static" / "globe.css").exists()
    assert (built / "static" / "earth-night.jpg").exists()


def test_globe_json이_계약과_맞는다(built):
    payload = read_json(built / "data" / "globe.json")
    assert payload["generated_at"] and payload["date"] == DATE
    assert payload["countries"]

    required = {
        "iso2", "name_ko", "lat", "lng", "weight",
        "article_count", "event_count", "avg_tone", "surge", "top_event",
    }
    for country in payload["countries"]:
        assert required <= set(country), set(country) ^ required
        assert 0.0 <= country["weight"] <= 1.0
        assert isinstance(country["avg_tone"], (int, float))
        assert -10 <= country["avg_tone"] <= 10
        assert config.by_iso(country["iso2"]) is not None
        if country["top_event"]:
            assert set(country["top_event"]) == {"id", "headline", "source_count"}


def test_globe_json은_weight_내림차순이다(built):
    countries = read_json(built / "data" / "globe.json")["countries"]
    weights = [c["weight"] for c in countries]
    assert weights == sorted(weights, reverse=True)


def test_국가별_json이_계약과_맞는다(built):
    globe = read_json(built / "data" / "globe.json")
    for country in globe["countries"][:5]:
        payload = read_json(built / "data" / "country" / f"{country['iso2']}.json")
        assert payload["iso2"] == country["iso2"]
        assert payload["date"] == DATE
        for event in payload["events"]:
            assert set(event) == {
                "id", "headline", "summary", "category",
                "tone", "source_count", "sources",
            }
            for source in event["sources"]:
                assert set(source) == {"domain", "title", "url"}
                assert source["url"].startswith("http")


def test_모든_국가_json이_글로브와_짝을_이룬다(built):
    globe = read_json(built / "data" / "globe.json")
    files = {p.stem for p in (built / "data" / "country").glob("*.json")}
    assert {c["iso2"] for c in globe["countries"]} == files


# ── 원문 미게시 ─────────────────────────────────────────────────────────


def test_사이트에_기사_원문이_실리지_않는다(built):
    """요약 + 출처 링크만. 원문 재게시는 저작권 문제로 직결된다."""
    for path in (built / "data" / "country").glob("*.json"):
        payload = read_json(path)
        for event in payload["events"]:
            for source in event["sources"]:
                # 출처에는 제목·도메인·링크만 있다. 본문 필드가 있으면 안 된다.
                assert "body" not in source
                assert "content" not in source
                assert "lead" not in source
                assert "html" not in source


def test_출처_링크에_보안_속성이_붙는다(built):
    html = (built / "country" / "US" / "index.html").read_text(encoding="utf-8")
    for match in re.finditer(r'<a [^>]*href="https?://(?!localhost)[^"]+"[^>]*>', html):
        tag = match.group(0)
        assert 'rel="noopener nofollow external"' in tag, tag


# ── 페이지 ──────────────────────────────────────────────────────────────


def test_index가_글로브와_폴백_목록을_둘_다_갖는다(built):
    html = (built / "index.html").read_text(encoding="utf-8")
    assert 'id="globe"' in html
    assert 'id="globe-fallback-note"' in html
    # 목록은 JS 없이도 읽혀야 한다 — 서버가 이미 렌더한 표가 있어야 한다
    assert "<table class=\"countries\"" in html
    assert html.count("<tr data-iso2=") >= 10
    assert "<noscript>" in html


def test_사건_상세와_브리핑_페이지가_생성된다(built):
    events = list((built / "event").glob("evt_*/index.html"))
    assert len(events) > 10
    html = events[0].read_text(encoding="utf-8")
    assert "출처" in html and "핵심 사실" in html

    briefing = built / "briefing" / DATE / "index.html"
    assert briefing.exists()
    assert "오늘의 3대 흐름" in briefing.read_text(encoding="utf-8")


def test_국가_폴백_페이지는_JS_없이_읽힌다(built):
    html = (built / "country" / "US" / "index.html").read_text(encoding="utf-8")
    assert "<script" not in html.split("</main>")[0], "본문이 JS에 의존하면 안 된다"
    assert "event-card" in html


def test_페이지에_뷰포트_메타가_있다(built):
    for path in (built / "index.html", built / "country" / "US" / "index.html"):
        html = path.read_text(encoding="utf-8")
        assert 'name="viewport"' in html
        assert 'lang="ko"' in html


def test_설정값이_템플릿에_전달된다(built, config_dir):
    html = (built / "index.html").read_text(encoding="utf-8")
    site = config.pipeline()["site"]["globe"]
    assert f"ringSurgeThreshold: {site['ring_surge_threshold']}" in html
    assert f"maxLabels: {site['max_labels']}" in html


# ── 프론트엔드 계약 ─────────────────────────────────────────────────────


def test_globe_js가_반드시_지킬_것을_구현한다(built):
    js = (built / "static" / "globe.js").read_text(encoding="utf-8")
    assert "webglSupported" in js and "degrade(" in js, "WebGL 폴백"
    assert "prefers-reduced-motion" in js, "모션 축소 존중"
    assert "Math.sqrt" in js, "마커 크기는 sqrt 정규화"
    assert "Escape" in js, "Esc 로 패널 닫기"
    assert "trapFocus" in js, "포커스 트랩"
    assert "autoRotate" in js and "5000" in js, "드래그 후 5초 뒤 회전 재개"


def test_css가_400px에서_가로스크롤을_막는다(built):
    css = (built / "static" / "globe.css").read_text(encoding="utf-8")
    assert "overflow-x: hidden" in css
    assert "max-width: 640px" in css, "모바일 분기"
    assert "[hidden] { display: none !important; }" in css, (
        "display:grid/flex 가 [hidden] 을 이기는 문제를 막아야 한다"
    )


def test_three를_따로_로드하지_않는다(built):
    """three r160+ 는 UMD 빌드가 없어 404 가 난다. globe.gl 번들에 포함돼 있다."""
    html = (built / "index.html").read_text(encoding="utf-8")
    assert "three.min.js" not in html
    assert "globe.gl" in html


# ── 그 외 ───────────────────────────────────────────────────────────────


def test_dry_run은_파일을_쓰지_않는다(tmp_path, config_dir):
    out = tmp_path / "nope"
    rc = step8_build_site.main(
        ["--from-fixtures", "--date", DATE, "--out", str(out), "--dry-run"]
    )
    assert rc == 0 and not out.exists()


def test_분석된_사건이_없으면_실패한다(tmp_path, config_dir):
    rc = step8_build_site.main(
        ["--date", DATE, "--db", str(tmp_path / "empty.db"), "--out", str(tmp_path / "d")]
    )
    assert rc == 1


def test_재빌드가_이전_산출물을_남기지_않는다(tmp_path, config_dir):
    out = tmp_path / "dist"
    step8_build_site.main(["--from-fixtures", "--date", DATE, "--out", str(out)])
    stale = out / "country" / "ZZ" / "index.html"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale", encoding="utf-8")

    step8_build_site.main(["--from-fixtures", "--date", DATE, "--out", str(out)])
    assert not stale.exists()


def test_report가_열어보는_법을_알려준다(built, tmp_path, config_dir, capsys):
    step8_build_site.main(
        ["--from-fixtures", "--date", DATE, "--out", str(tmp_path / "r"), "--report"]
    )
    out = capsys.readouterr().out
    assert "http.server" in out and "localhost" in out
