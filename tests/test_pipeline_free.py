"""step1~4 (LLM 없는 구간) 통합 테스트.

완료 조건: fixture 200건으로 step1~4 를 돌려 사건 클러스터가 생기는 것을 검증.
"""
from __future__ import annotations

import json

import pytest

import config
from llm import registry
from pipeline import (
    step1_collect,
    step2_filter,
    step3_embed,
    step4_cluster,
)
from pipeline.sources import canonical_url, GdeltSource
from store.db import connect

ARTLIST = "tests/fixtures/gdelt_artlist.json"
TIMELINE = "tests/fixtures/gdelt_timelinesourcecountry.json"
DATE = "2026-09-12"


@pytest.fixture
def seeded_db(tmp_path, config_dir):
    """step0 을 돌려 가중치가 들어 있는 DB."""
    from pipeline import step0_weights

    path = tmp_path / "pipe.db"
    step0_weights.main(["--fixture", TIMELINE, "--db", str(path), "--date", DATE])
    return path


def run_step(module, db_path, *extra):
    return module.main(["--db", str(db_path), "--date", DATE, *extra])


# ── URL 정규화 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("https://www.reuters.com/x/y", "http://reuters.com/x/y/"),
        ("https://reuters.com/a?utm_source=tw&id=3", "https://reuters.com/a?id=3"),
        ("https://m.bbc.co.uk/news/1", "https://bbc.co.uk/news/1"),
        ("https://ft.com/a#section", "https://ft.com/a"),
        ("https://cnn.com/story/amp", "https://cnn.com/story"),
    ],
)
def test_같은_기사를_같은_url로_본다(a, b):
    assert canonical_url(a) == canonical_url(b)


def test_다른_기사는_다르게_본다():
    assert canonical_url("https://a.com/1") != canonical_url("https://a.com/2")


# ── step1 ───────────────────────────────────────────────────────────────


def test_step1은_step0_없이는_친절하게_실패한다(tmp_path, config_dir):
    assert step1_collect.main(["--db", str(tmp_path / "empty.db"), "--plan-only"]) == 1


def test_step1_계획이_설정대로_배분된다(seeded_db, config_dir):
    rc = run_step(step1_collect, seeded_db, "--plan-only")
    assert rc == 0

    budget = config.sources()["budget"]
    plan = connect(seeded_db).plan_for(DATE)
    assert len(plan) == budget["top_n_countries"]
    assert sum(r["quota"] for r in plan) == budget["articles_per_day"]
    assert max(r["quota"] for r in plan) <= budget["cap_per_country"]
    assert min(r["quota"] for r in plan) >= budget["floor_per_country"]


def test_step1_dry_run은_기사를_저장하지_않는다(seeded_db, config_dir):
    rc = run_step(step1_collect, seeded_db, "--dry-run", "--gdelt-fixture", ARTLIST)
    assert rc == 0
    assert connect(seeded_db).query("SELECT * FROM articles") == []


def test_step1이_기사를_수집한다(seeded_db, config_dir):
    rc = run_step(step1_collect, seeded_db, "--gdelt-fixture", ARTLIST)
    assert rc == 0

    db = connect(seeded_db)
    rows = db.query("SELECT * FROM articles")
    assert len(rows) > 50
    assert all(r["country_fips"] in config.known_fips() for r in rows)
    assert all(r["blob_key"] for r in rows), "원문 위치가 기록돼야 한다"
    assert all(r["source_kind"] == "gdelt" for r in rows)


def test_step1은_같은_url을_두_번_받지_않는다(seeded_db, config_dir):
    run_step(step1_collect, seeded_db, "--gdelt-fixture", ARTLIST)
    first = len(connect(seeded_db).query("SELECT * FROM articles"))

    run_step(step1_collect, seeded_db, "--gdelt-fixture", ARTLIST)
    second = len(connect(seeded_db).query("SELECT * FROM articles"))
    assert second == first, "두 번째 실행이 중복을 다시 넣었다"


def test_step1은_maxrecords를_명시한다(config_dir):
    """기본값 75로 돌면 수집량이 3분의 1로 조용히 준다."""
    source = GdeltSource()
    params = source.params("US", None, 500)
    assert params["maxrecords"] == "250"
    assert params["query"] == "sourcecountry:US"
    assert params["mode"] == "artlist"


def test_할당이_상한을_넘으면_테마로_쿼리를_쪼갠다(seeded_db, config_dir):
    config_dir.patch(
        "sources",
        lambda d: (
            d["gdelt"].update({"maxrecords": 50}),
            d["budget"].update({"cap_per_country": 200}),
        ),
    )
    source = GdeltSource(__import__("pathlib").Path(ARTLIST))
    from pipeline.budget import split_queries

    themes = config.sources()["gdelt"]["themes_for_split"]
    plan = split_queries(200, source.maxrecords, themes)
    assert len(plan) == 4


def test_step1_report는_계획_대비_실제를_낸다(seeded_db, config_dir, capsys):
    run_step(step1_collect, seeded_db, "--gdelt-fixture", ARTLIST, "--report")
    out = capsys.readouterr().out
    assert "계획" in out and "실제" in out and "달성" in out


# ── step2 ───────────────────────────────────────────────────────────────


@pytest.fixture
def collected(seeded_db, config_dir):
    run_step(step1_collect, seeded_db, "--gdelt-fixture", ARTLIST)
    return seeded_db


def test_step2가_룰로_걸러낸다(collected):
    before = len(connect(collected).articles_by_status("collected"))
    assert run_step(step2_filter, collected) == 0

    db = connect(collected)
    passed = db.articles_by_status("filtered")
    rejected = db.articles_by_status("rejected")
    assert passed and rejected
    assert len(passed) + len(rejected) == before


def test_step2는_걸러낸_이유를_남긴다(collected):
    run_step(step2_filter, collected)
    summary = {r["reason"]: r["n"] for r in connect(collected).filter_summary(DATE)}
    assert summary, "filter_log 가 비어 있으면 임계값을 손댈 근거가 없다"
    assert "language" in summary or "short_title" in summary or "domain" in summary


def test_step2가_언어_짧은제목_블록리스트를_각각_잡는다(collected):
    run_step(step2_filter, collected)
    db = connect(collected)
    reasons = {r["reason"] for r in db.query("SELECT DISTINCT reason FROM filter_log")}
    assert "language" in reasons, "일본어 기사가 언어 필터에 걸려야 한다"
    assert "short_title" in reasons, "제목 'Short' 가 걸려야 한다"
    assert "domain" in reasons, "example-spam.com 이 블록리스트에 걸려야 한다"


def test_step2_dry_run은_상태를_바꾸지_않는다(collected):
    before = len(connect(collected).articles_by_status("collected"))
    run_step(step2_filter, collected, "--dry-run")
    assert len(connect(collected).articles_by_status("collected")) == before


def test_step2_임계값은_설정에서_온다(collected, config_dir):
    config_dir.patch("pipeline", lambda d: d["filter"].update({"min_title_chars": 500}))
    run_step(step2_filter, collected)
    assert connect(collected).articles_by_status("filtered") == []


# ── step3 ───────────────────────────────────────────────────────────────


@pytest.fixture
def filtered(collected):
    run_step(step2_filter, collected)
    return collected


def test_step3_dry_run은_API_없이_돈다(filtered):
    """API 키가 하나도 없는 상태다 (conftest 가 지웠다)."""
    assert run_step(step3_embed, filtered, "--dry-run") == 0
    assert connect(filtered).query("SELECT * FROM embeddings") == []


def test_step3이_임베딩을_저장한다(filtered, config_dir, fake_client):
    config_dir.patch(
        "models", lambda d: d["roles"]["embedding"].update({"provider": "fake"})
    )
    assert run_step(step3_embed, filtered) == 0

    db = connect(filtered)
    rows = db.query("SELECT * FROM embeddings")
    assert rows
    assert all(r["dimensions"] == 512 for r in rows)
    assert all(len(r["vector"]) == 512 for r in rows), "int8 이면 차원당 1바이트다"


def test_임베딩에_모델이_기록된다(filtered, config_dir, fake_client):
    """모델을 바꾸면 기존 벡터와 거리 계산이 호환되지 않는다."""
    config_dir.patch(
        "models", lambda d: d["roles"]["embedding"].update({"provider": "fake"})
    )
    run_step(step3_embed, filtered)
    db = connect(filtered)
    model = config.role("embedding")["model"]
    assert {r["model"] for r in db.query("SELECT DISTINCT model FROM embeddings")} == {model}
    assert db.embedded_ids(model)
    assert db.embedded_ids("some-other-model") == set(), "다른 모델 벡터는 재사용하지 않는다"


def test_step3은_이미_임베딩된_기사를_건너뛴다(filtered, config_dir, fake_client):
    config_dir.patch(
        "models", lambda d: d["roles"]["embedding"].update({"provider": "fake"})
    )
    run_step(step3_embed, filtered)
    n = len(connect(filtered).query("SELECT * FROM embeddings"))
    run_step(step3_embed, filtered)
    assert len(connect(filtered).query("SELECT * FROM embeddings")) == n


def test_양자화_왕복이_방향을_보존한다():
    from pipeline.step3_embed import dequantize_int8, quantize_int8

    vec = [0.9, -0.4, 0.1, 0.0, -0.85]
    back = dequantize_int8(quantize_int8(vec))
    dot = sum(a * b for a, b in zip(vec, back))
    norm = (sum(v * v for v in vec) ** 0.5) * (sum(v * v for v in back) ** 0.5)
    assert dot / norm > 0.999, "코사인 유사도가 보존돼야 한다"


# ── step4 ───────────────────────────────────────────────────────────────


@pytest.fixture
def embedded(filtered, config_dir, fake_client):
    config_dir.patch(
        "models", lambda d: d["roles"]["embedding"].update({"provider": "fake"})
    )
    run_step(step3_embed, filtered)
    return filtered


def test_step4가_사건을_만든다(embedded):
    assert run_step(step4_cluster, embedded) == 0

    db = connect(embedded)
    events = db.events_for(DATE)
    assert events, "사건이 하나도 안 생겼다"
    assert all(e["article_count"] >= 2 for e in events)
    assert all(e["primary_country"] in config.known_fips() for e in events)
    assert all(e["status"] == "pending" for e in events)


def test_사건_수가_기사_수보다_훨씬_적다(embedded):
    """이게 성립하지 않으면 비용 구조가 무너진다 — 기사당 호출과 같아진다."""
    run_step(step4_cluster, embedded)
    db = connect(embedded)
    events = db.events_for(DATE)
    clustered = db.articles_by_status("clustered")
    assert len(events) < len(clustered) / 1.5


def test_대표_기사는_같은_매체에서_둘_뽑히지_않는다(embedded):
    run_step(step4_cluster, embedded)
    db = connect(embedded)
    for event in db.events_for(DATE):
        reps = json.loads(event["representatives"])
        assert len(reps) <= 3
        domains = [
            db.one("SELECT domain FROM articles WHERE id = ?", (rid,))["domain"]
            for rid in reps
        ]
        if event["source_count"] >= len(reps):
            assert len(set(domains)) == len(domains), f"{event['id']} 대표 매체 중복"


def test_기사에_event_id가_붙는다(embedded):
    run_step(step4_cluster, embedded)
    db = connect(embedded)
    clustered = db.articles_by_status("clustered")
    assert clustered
    assert all(a["event_id"] for a in clustered)
    for event in db.events_for(DATE):
        assert len(db.articles_of_event(event["id"])) == event["article_count"]


def test_재실행이_기존_사건을_덮어쓰지_않는다(embedded):
    run_step(step4_cluster, embedded)
    first = {e["id"] for e in connect(embedded).events_for(DATE)}
    assert first

    run_step(step4_cluster, embedded)          # 남은 기사로 한 번 더
    after = {e["id"] for e in connect(embedded).events_for(DATE)}
    assert first <= after, "기존 사건 id 가 사라졌다"


def test_recluster는_처음부터_다시_묶는다(embedded):
    run_step(step4_cluster, embedded)
    run_step(step4_cluster, embedded, "--recluster")
    db = connect(embedded)
    ids = [e["id"] for e in db.events_for(DATE)]
    assert len(ids) == len(set(ids))
    assert ids and min(ids).endswith("_0001")


def test_step4_dry_run은_사건을_저장하지_않는다(embedded):
    run_step(step4_cluster, embedded, "--dry-run")
    assert connect(embedded).events_for(DATE) == []


def test_step4_report가_사건별_제목을_출력한다(embedded, capsys):
    run_step(step4_cluster, embedded, "--report")
    out = capsys.readouterr().out
    assert "evt_20260912_" in out
    assert "★" in out, "대표 기사 표시가 있어야 눈으로 품질을 본다"


def test_두_클러스터링_방식_모두_동작한다(embedded):
    for method in ("threshold", "hdbscan"):
        run_step(step4_cluster, embedded, "--method", method, "--recluster")
        assert connect(embedded).events_for(DATE)


def test_primary_country_동률이면_매체가_많은_쪽():
    from pipeline.step4_cluster import primary_country

    rows = [
        {"country_fips": "US", "domain": "a.com"},
        {"country_fips": "US", "domain": "a.com"},
        {"country_fips": "CH", "domain": "b.com"},
        {"country_fips": "CH", "domain": "c.com"},
    ]
    assert primary_country(rows) == "CH"


def test_임계값은_설정에서_온다(embedded, config_dir):
    """코드에 숫자를 박지 않았는지 — 설정을 바꾸면 결과가 바뀌어야 한다."""
    config_dir.patch(
        "pipeline", lambda d: d["cluster"].update({"similarity_threshold": 0.999})
    )
    run_step(step4_cluster, embedded, "--method", "threshold", "--recluster")
    strict = len(connect(embedded).events_for(DATE))

    config_dir.patch(
        "pipeline", lambda d: d["cluster"].update({"similarity_threshold": 0.3})
    )
    run_step(step4_cluster, embedded, "--method", "threshold", "--recluster")
    loose = len(connect(embedded).events_for(DATE))
    assert strict != loose


# ── 전체 ────────────────────────────────────────────────────────────────


def test_step1부터_step4까지_dry_run이_키_없이_끝까지_돈다(seeded_db, config_dir):
    for module, extra in (
        (step1_collect, ["--gdelt-fixture", ARTLIST]),
        (step2_filter, []),
        (step3_embed, []),
        (step4_cluster, []),
    ):
        assert run_step(module, seeded_db, "--dry-run", *extra) == 0, module.__name__


def test_step1이_추적파라미터만_다른_중복을_접는다(seeded_db, config_dir):
    """fixture 에는 www/AMP/utm 변형만 다른 같은 기사가 한 벌 더 들어 있다."""
    import json as _json

    raw = _json.loads(open(ARTLIST, encoding="utf-8").read())["articles"]
    run_step(step1_collect, seeded_db, "--gdelt-fixture", ARTLIST)

    stored = connect(seeded_db).query("SELECT * FROM articles")
    assert len(stored) == len(raw) - 1, "정규화 후 중복이 접히지 않았다"
    assert len({r["url_canonical"] for r in stored}) == len(stored)


def test_step2의_중복_규칙이_단독으로도_동작한다(config_dir):
    """step1 이 이미 접지만, 여러 실행에 걸친 중복을 잡는 안전망이다."""
    from pipeline.step2_filter import DUPLICATE, Rules

    rules = Rules()
    row = {
        "url": "https://www.reuters.com/a/b",
        "url_canonical": "https://reuters.com/a/b",
        "domain": "reuters.com",
        "title": "A sufficiently long headline about markets today",
        "lead": "",
        "language": "English",
        "country_fips": "US",
    }
    seen: set[str] = set()
    assert rules.check(row, seen) is None
    assert rules.check(row, seen)[0] == DUPLICATE
