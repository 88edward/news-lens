"""step5 → step6 → step7 통합 테스트.

완료 조건: fake provider 로 전체가 돌고, PENDING 상태와 스키마 검증 실패
케이스를 둘 다 포함한다.
"""
from __future__ import annotations

import json

import pytest

import config
from llm import cost
from pipeline import step5_submit_batch, step6_fetch_batch, step7_synthesize
from pipeline.schema import extract_json, parse_and_validate
from store.db import connect

DATE = "2026-09-12"
PENDING_EXIT = 75


@pytest.fixture
def clustered_db(tmp_path, config_dir, fake_client):
    """step0~4 를 돌려 사건이 들어 있는 DB. 모든 역할이 fake provider."""
    from pipeline import (
        step0_weights,
        step1_collect,
        step2_filter,
        step3_embed,
        step4_cluster,
    )

    config_dir.patch(
        "models",
        lambda d: [r.update({"provider": "fake"}) for r in d["roles"].values()],
    )
    path = tmp_path / "batch.db"
    args = ["--db", str(path), "--date", DATE]
    step0_weights.main(["--fixture", "tests/fixtures/gdelt_timelinesourcecountry.json", *args])
    step1_collect.main(["--gdelt-fixture", "tests/fixtures/gdelt_artlist.json", *args])
    step2_filter.main(args)
    step3_embed.main(args)
    step4_cluster.main(args)
    assert connect(path).events_for(DATE), "전제: 사건이 있어야 한다"
    return path


def run_step(module, db_path, *extra):
    return module.main(["--db", str(db_path), "--date", DATE, *extra])


def valid_event_json(custom_id: str = "") -> str:
    return json.dumps(
        {
            "한줄요약": "중앙은행이 기준금리를 동결하며 물가 위험을 언급했다",
            "핵심사실": ["기준금리 동결", "물가 상승률 목표 상회"],
            "배경": "물가가 목표를 웃도는 상황이 이어지며 완화 전환이 미뤄지고 있다.",
            "이해관계자": [{"이름": "중앙은행", "입장": "물가 안정 우선"}],
            "영향분석": "차입 비용이 유지되어 부동산과 소비에 부담이 이어진다.",
            "출처간_불일치": [{"쟁점": "인하 시점", "설명": "매체별 전망이 갈린다"}],
            "카테고리": "economy",
            "신뢰도": 0.82,
            "논조점수": -2.5,
        },
        ensure_ascii=False,
    )


def valid_briefing_json(event_ids) -> str:
    ids = list(event_ids)[:2]
    return json.dumps(
        {
            "헤드라인": "주요국 통화정책이 동시에 관망으로 돌아선 하루",
            "오늘의_3대_흐름": [
                {
                    "제목": "긴축 유지",
                    "설명": "여러 중앙은행이 같은 방향으로 관망을 택했다는 점이 공통이다.",
                    "관련사건": ids,
                },
                {
                    "제목": "통화 약세",
                    "설명": "신흥국 통화가 동시에 약세를 보이며 자본 유출 우려가 커졌다.",
                    "관련사건": [],
                },
            ],
            "지역별_요약": [
                {"지역": "동아시아", "요약": "정책 변화 없이 관망세가 이어졌고 환율만 움직였다."}
            ],
            "주목할_신호": [{"신호": "채권 스프레드 확대", "근거": "소수 매체만 다뤘다"}],
        },
        ensure_ascii=False,
    )


# ── JSON 파싱 ───────────────────────────────────────────────────────────


def test_코드펜스가_붙어도_파싱한다():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_앞뒤_설명이_붙어도_파싱한다():
    assert extract_json('네, 분석 결과입니다:\n{"a": 1}\n이상입니다.') == {"a": 1}


def test_JSON이_아니면_None():
    assert extract_json("죄송하지만 분석할 수 없습니다") is None
    assert extract_json("") is None


def test_스키마_위반을_잡아낸다(config_dir):
    schema = config.schema_for("event_analysis")
    bad = json.loads(valid_event_json())
    bad["논조점수"] = "부정적"          # 숫자여야 한다 — 마커 색이 깨진다
    _, errors = parse_and_validate(json.dumps(bad, ensure_ascii=False), schema)
    assert errors and "논조점수" in errors[0]


def test_유효한_응답은_통과한다(config_dir):
    _, errors = parse_and_validate(valid_event_json(), config.schema_for("event_analysis"))
    assert errors == []


# ── step5 ───────────────────────────────────────────────────────────────


def test_step5_dry_run은_제출하지_않는다(clustered_db, fake_client):
    assert run_step(step5_submit_batch, clustered_db, "--dry-run") == 0
    assert fake_client.batch_store == {}
    assert all(e["status"] == "pending" for e in connect(clustered_db).events_for(DATE))


def test_step5가_제출하고_즉시_끝낸다(clustered_db, fake_client):
    assert run_step(step5_submit_batch, clustered_db) == 0

    db = connect(clustered_db)
    assert db.events_for(DATE, status="submitted")
    assert db.events_for(DATE, status="pending") == []

    open_batches = db.open_batches()
    assert len(open_batches) == 1
    assert open_batches[0]["status"] == "submitted"
    assert open_batches[0]["batch_id"] in fake_client.batch_store


def test_제출_요청에_원문이_들어가지_않는다(clustered_db, fake_client):
    run_step(step5_submit_batch, clustered_db)
    reqs = next(iter(fake_client.batch_store.values()))
    assert reqs
    for req in reqs:
        assert "대표 기사" in req.user
        assert len(req.user) < 4000, "원문이 섞여 들어갔을 가능성"


def test_step5는_사건_단위로만_부른다(clustered_db, fake_client):
    """기사 단위로 부르면 비용이 5배가 된다."""
    db = connect(clustered_db)
    n_events = len(db.events_for(DATE))
    n_articles = len(db.articles_by_status("clustered"))

    run_step(step5_submit_batch, clustered_db)
    reqs = next(iter(fake_client.batch_store.values()))
    assert len(reqs) == n_events < n_articles


def test_step5는_비용_상한을_넘으면_중단한다(clustered_db, config_dir, fake_client):
    config_dir.patch(
        "pipeline", lambda d: d["cost"].update({"max_daily_cost_usd": 0.0000001})
    )
    assert run_step(step5_submit_batch, clustered_db) == 1
    assert fake_client.batch_store == {}, "상한을 넘겼는데 제출했다"
    assert connect(clustered_db).events_for(DATE, status="pending")


def test_step5_예상비용이_stderr에_찍힌다(clustered_db, fake_client, capsys):
    run_step(step5_submit_batch, clustered_db, "--dry-run")
    err = capsys.readouterr().err
    assert "[cost]" in err and "예상 $" in err


# ── step6 ───────────────────────────────────────────────────────────────


def test_step6은_PENDING이면_75로_끝낸다(clustered_db, fake_client):
    run_step(step5_submit_batch, clustered_db)
    batch_id = next(iter(fake_client.batch_store))
    fake_client.pending_batches.add(batch_id)

    assert run_step(step6_fetch_batch, clustered_db) == PENDING_EXIT
    assert connect(clustered_db).events_for(DATE, status="analyzed") == []


def test_step6이_결과를_저장한다(clustered_db, fake_client):
    fake_client.responder = lambda req: valid_event_json(req.custom_id)
    run_step(step5_submit_batch, clustered_db)
    assert run_step(step6_fetch_batch, clustered_db) == 0

    db = connect(clustered_db)
    analyzed = db.events_for(DATE, status="analyzed")
    assert analyzed
    for event in analyzed:
        assert event["schema_ok"] == 1
        assert event["headline"]
        assert isinstance(event["tone"], (int, float)), "논조점수는 숫자여야 한다"
        assert json.loads(event["analysis"])["카테고리"]


def test_검증_실패해도_원문을_버리지_않는다(clustered_db, fake_client):
    """프롬프트를 고칠 근거는 raw 응답에만 있다."""
    fake_client.responder = lambda req: "분석할 수 없습니다. 죄송합니다."
    run_step(step5_submit_batch, clustered_db)
    run_step(step6_fetch_batch, clustered_db)

    db = connect(clustered_db)
    failed = db.events_for(DATE, status="failed")
    assert failed
    for event in failed:
        assert event["schema_ok"] == 0
        assert event["raw_response"] == "분석할 수 없습니다. 죄송합니다."


def test_논조점수가_문자열이면_검증에_걸린다(clustered_db, fake_client):
    def bad(req):
        payload = json.loads(valid_event_json())
        payload["논조점수"] = "부정적"
        return json.dumps(payload, ensure_ascii=False)

    fake_client.responder = bad
    run_step(step5_submit_batch, clustered_db)
    run_step(step6_fetch_batch, clustered_db)
    assert connect(clustered_db).events_for(DATE, status="failed")


def test_step6_report가_실패_사유를_낸다(clustered_db, fake_client, capsys):
    fake_client.responder = lambda req: "{}"
    run_step(step5_submit_batch, clustered_db)
    run_step(step6_fetch_batch, clustered_db, "--report")
    out = capsys.readouterr().out
    assert "검증 실패 사유" in out and "raw_response" in out


def test_step6은_비용을_runs에_누적한다(clustered_db, fake_client):
    fake_client.responder = lambda req: valid_event_json()
    run_step(step5_submit_batch, clustered_db)
    run_step(step6_fetch_batch, clustered_db)
    assert connect(clustered_db).spent_today(DATE) > 0


def test_step6은_수거할게_없으면_0(clustered_db, fake_client):
    assert run_step(step6_fetch_batch, clustered_db) == 0


# ── step7 ───────────────────────────────────────────────────────────────


@pytest.fixture
def analyzed_db(clustered_db, fake_client):
    fake_client.responder = lambda req: valid_event_json()
    run_step(step5_submit_batch, clustered_db)
    run_step(step6_fetch_batch, clustered_db)
    fake_client.responder = None
    return clustered_db


def test_step7_sync모드가_브리핑을_만든다(analyzed_db, config_dir, fake_client):
    config_dir.patch(
        "models", lambda d: d["roles"]["daily_synthesis"].update({"mode": "sync"})
    )
    ids = [e["id"] for e in connect(analyzed_db).events_for(DATE, status="analyzed")]
    fake_client.responder = lambda req: valid_briefing_json(ids)

    assert run_step(step7_synthesize, analyzed_db) == 0
    row = connect(analyzed_db).briefing_for(DATE)
    assert row and row["schema_ok"] == 1
    assert json.loads(row["content"])["오늘의_3대_흐름"]


def test_step7_batch모드는_제출하고_75로_끝낸다(analyzed_db, fake_client):
    """models.yaml 기본이 batch 다. 제출과 수거가 다른 실행이어야 한다."""
    assert config.role("daily_synthesis")["mode"] == "batch"
    ids = [e["id"] for e in connect(analyzed_db).events_for(DATE, status="analyzed")]
    fake_client.responder = lambda req: valid_briefing_json(ids)

    assert run_step(step7_synthesize, analyzed_db) == PENDING_EXIT
    assert connect(analyzed_db).briefing_for(DATE) is None

    assert run_step(step7_synthesize, analyzed_db) == 0     # 두 번째 실행이 수거
    assert connect(analyzed_db).briefing_for(DATE)["schema_ok"] == 1


def test_step7은_하루에_한_번만_부른다(analyzed_db, config_dir, fake_client):
    config_dir.patch(
        "models", lambda d: d["roles"]["daily_synthesis"].update({"mode": "sync"})
    )
    ids = [e["id"] for e in connect(analyzed_db).events_for(DATE, status="analyzed")]
    calls = []

    def responder(req):
        calls.append(req.custom_id)
        return valid_briefing_json(ids)

    fake_client.responder = responder
    run_step(step7_synthesize, analyzed_db)
    run_step(step7_synthesize, analyzed_db)      # 이미 있으므로 부르지 않는다
    assert len(calls) == 1


def test_없는_사건_id를_지어내면_거부한다(analyzed_db, config_dir, fake_client):
    """스키마는 형식만 본다. 죽은 링크는 여기서만 막을 수 있다."""
    config_dir.patch(
        "models", lambda d: d["roles"]["daily_synthesis"].update({"mode": "sync"})
    )
    fake_client.responder = lambda req: valid_briefing_json(
        ["evt_20260912_9999", "evt_20260912_8888"]
    )
    assert run_step(step7_synthesize, analyzed_db) == 1

    row = connect(analyzed_db).briefing_for(DATE)
    assert row["schema_ok"] == 0
    assert row["raw_response"], "원문은 남아 있어야 한다"


def test_step7_dry_run은_호출하지_않는다(analyzed_db, fake_client):
    assert run_step(step7_synthesize, analyzed_db, "--dry-run") == 0
    assert connect(analyzed_db).briefing_for(DATE) is None


def test_step7은_분석된_사건이_없으면_넘어간다(clustered_db, fake_client):
    assert run_step(step7_synthesize, clustered_db) == 0
    assert connect(clustered_db).briefing_for(DATE) is None


# ── 전체 ────────────────────────────────────────────────────────────────


def test_step5부터_step7까지_전체가_돈다(clustered_db, config_dir, fake_client):
    config_dir.patch(
        "models", lambda d: d["roles"]["daily_synthesis"].update({"mode": "sync"})
    )
    fake_client.responder = lambda req: valid_event_json()
    assert run_step(step5_submit_batch, clustered_db) == 0
    assert run_step(step6_fetch_batch, clustered_db) == 0

    db = connect(clustered_db)
    ids = [e["id"] for e in db.events_for(DATE, status="analyzed")]
    fake_client.responder = lambda req: valid_briefing_json(ids)
    assert run_step(step7_synthesize, clustered_db) == 0

    db = connect(clustered_db)
    assert db.events_for(DATE, status="analyzed")
    assert db.briefing_for(DATE)["schema_ok"] == 1
    assert db.spent_today(DATE) > 0


def test_step5_6_7_dry_run이_키_없이_돈다(clustered_db, fake_client):
    for module in (step5_submit_batch, step6_fetch_batch, step7_synthesize):
        assert run_step(module, clustered_db, "--dry-run") == 0, module.__name__
