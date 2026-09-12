"""DB 영속화와 정리 테스트.

이 구성의 급소다. 러너는 매 실행마다 새로 뜨므로, 덤프가 왕복하지 않으면
step1 이 모은 기사가 4시간 뒤 step2 실행에는 존재하지 않는다.
"""
from __future__ import annotations

import json

import pytest

import config
from pipeline import step9_prune
from store import dump
from store.db import connect

DATE = "2026-09-12"


@pytest.fixture
def populated(tmp_path, config_dir):
    db_path = tmp_path / "nl.db"
    db = connect(db_path)
    db.save_weights(
        DATE, [{"fips": "US", "iso2": "US", "attention": 0.9, "surge": 0.1, "weight": 1.0}]
    )
    db.insert_article(
        {
            "id": "a1",
            "url": "https://example.com/a",
            "url_canonical": "https://example.com/a",
            "domain": "example.com",
            "title": "제목 'A' 와 \"따옴표\"",
            "lead": "리드\n줄바꿈 포함",
            "language": "en",
            "collected_at": f"{DATE}T06:00:00+00:00",
            "country_fips": "US",
            "source_kind": "gdelt",
        }
    )
    db.insert_event(
        {
            "id": "evt_20260912_0001",
            "date": DATE,
            "primary_country": "US",
            "article_count": 1,
            "source_count": 1,
            "representatives": '["a1"]',
        }
    )
    db.save_event_analysis(
        "evt_20260912_0001",
        analysis={"한줄요약": "한글 요약", "논조점수": -3.5, "카테고리": "economy"},
        raw="{}",
        schema_ok=True,
    )
    db.assign_event("a1", "evt_20260912_0001")
    db.save_embedding("a1", "m", 768, "int8", bytes(range(256)) * 3)
    db.log_filtered(DATE, "a2", "https://example.com/b", "language", "ja")
    db.save_briefing(DATE, "헤드라인", {"헤드라인": "한글"}, raw="{}", schema_ok=True)
    db.commit()
    db.close()
    return db_path


# ── 덤프 왕복 ───────────────────────────────────────────────────────────


def test_덤프가_텍스트다(populated, tmp_path):
    """바이너리를 커밋하면 git 이 델타를 못 잡아 히스토리가 폭증한다."""
    sql = dump.export(populated, tmp_path / "d.sql")
    text = sql.read_text(encoding="utf-8")
    assert text.startswith("BEGIN TRANSACTION;")
    assert "CREATE TABLE" in text
    assert "\x00" not in text


def test_왕복이_내용을_보존한다(populated, tmp_path):
    sql = dump.export(populated, tmp_path / "d.sql")
    restored = dump.restore(sql, tmp_path / "restored.db")

    db = connect(restored)
    article = db.one("SELECT * FROM articles WHERE id = 'a1'")
    assert article["title"] == "제목 'A' 와 \"따옴표\""
    assert "줄바꿈" in article["lead"]

    event = db.one("SELECT * FROM events WHERE id = 'evt_20260912_0001'")
    assert event["tone"] == -3.5
    assert json.loads(event["analysis"])["한줄요약"] == "한글 요약"

    assert db.briefing_for(DATE)["headline"] == "헤드라인"
    assert db.weights_for(DATE)[0]["fips"] == "US"
    assert db.query("SELECT * FROM embeddings")


def test_덤프가_없으면_빈_DB를_만든다(tmp_path, config_dir):
    """첫 실행이다. 여기서 죽으면 파이프라인이 시작조차 못 한다."""
    target = dump.restore(tmp_path / "없는파일.sql", tmp_path / "new.db")
    db = connect(target)
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "articles" in tables and "events" in tables


def test_DB가_없으면_빈_덤프를_남긴다(tmp_path, config_dir):
    out = dump.export(tmp_path / "없는DB.db", tmp_path / "out.sql")
    assert out.exists()


def test_복원이_기존_DB를_덮어쓴다(populated, tmp_path):
    """이전 실행의 찌꺼기가 남으면 상태가 뒤섞인다."""
    sql = dump.export(populated, tmp_path / "d.sql")
    stale = tmp_path / "stale.db"
    db = connect(stale)
    db.insert_event(
        {
            "id": "evt_99999999_9999",
            "date": "1999-01-01",
            "primary_country": "US",
            "article_count": 0,
            "source_count": 0,
            "representatives": "[]",
        }
    )
    db.commit()
    db.close()

    dump.restore(sql, stale)
    assert connect(stale).one("SELECT * FROM events WHERE id = 'evt_99999999_9999'") is None


def test_덤프_크기_보고가_경고를_낸다(tmp_path, config_dir, monkeypatch):
    big = tmp_path / "big.sql"
    big.write_text("x" * (51 * 1024 * 1024), encoding="utf-8")
    assert "경고" in dump.size_report(big)


# ── 정리 ────────────────────────────────────────────────────────────────


def test_클러스터링된_기사의_임베딩을_지운다(populated, config_dir):
    """가장 큰 덩어리다. 클러스터링이 끝나면 쓸 데가 없다."""
    assert connect(populated).query("SELECT * FROM embeddings")
    step9_prune.main(["--db", str(populated), "--date", DATE])
    assert connect(populated).query("SELECT * FROM embeddings") == []


def test_아직_묶이지_않은_기사의_임베딩은_남긴다(populated, config_dir):
    db = connect(populated)
    db.insert_article(
        {
            "id": "a9",
            "url": "https://example.com/z",
            "url_canonical": "https://example.com/z",
            "domain": "example.com",
            "title": "아직 안 묶인 기사",
            "lead": "",
            "language": "en",
            "collected_at": f"{DATE}T06:00:00+00:00",
            "country_fips": "US",
            "source_kind": "gdelt",
        }
    )
    db.save_embedding("a9", "m", 768, "int8", b"\x01" * 768)
    db.commit()
    db.close()

    step9_prune.main(["--db", str(populated), "--date", DATE])
    left = {r["article_id"] for r in connect(populated).query("SELECT * FROM embeddings")}
    assert left == {"a9"}


def test_오래된_기사와_로그를_지운다(populated, config_dir):
    step9_prune.main(["--db", str(populated), "--date", "2027-01-01"])
    db = connect(populated)
    assert db.query("SELECT * FROM articles") == []
    assert db.query("SELECT * FROM filter_log") == []


def test_사건과_브리핑은_남는다(populated, config_dir):
    """이게 이 서비스의 실제 산출물이다. 기사는 지워도 이건 남아야 한다."""
    step9_prune.main(["--db", str(populated), "--date", "2027-01-01"])
    db = connect(populated)
    event = db.one("SELECT * FROM events WHERE id = 'evt_20260912_0001'")
    assert event is not None
    assert event["headline"] and event["tone"] == -3.5
    assert db.briefing_for(DATE) is not None


def test_오래된_사건의_분석_전문만_비운다(populated, config_dir):
    """전문은 이미 정적 페이지에 구워져 있다. 헤드라인·논조는 목록에 필요하다."""
    step9_prune.main(["--db", str(populated), "--date", "2027-01-01"])
    event = connect(populated).one("SELECT * FROM events WHERE id = 'evt_20260912_0001'")
    assert event["analysis"] == ""
    assert event["headline"] == "한글 요약"
    assert event["category"] == "economy"


def test_dry_run은_아무것도_지우지_않는다(populated, config_dir):
    before = len(connect(populated).query("SELECT * FROM embeddings"))
    step9_prune.main(["--db", str(populated), "--date", "2027-01-01", "--dry-run"])
    assert len(connect(populated).query("SELECT * FROM embeddings")) == before


def test_정리가_덤프를_줄인다(populated, tmp_path, config_dir):
    before = dump.export(populated, tmp_path / "before.sql").stat().st_size
    step9_prune.main(["--db", str(populated), "--date", DATE, "--vacuum"])
    after = dump.export(populated, tmp_path / "after.sql").stat().st_size
    assert after < before


def test_report가_무엇을_지웠는지_보여준다(populated, config_dir, capsys):
    step9_prune.main(["--db", str(populated), "--date", DATE, "--report"])
    out = capsys.readouterr().out
    assert "임베딩" in out and "행 수" in out


def test_보관기간은_설정에서_온다(populated, config_dir):
    config_dir.patch(
        "pipeline", lambda d: d["retention"].update({"drop_embeddings_after_cluster": False})
    )
    step9_prune.main(["--db", str(populated), "--date", DATE])
    assert connect(populated).query("SELECT * FROM embeddings"), "설정을 무시했다"


# ── 디스크 정리 ─────────────────────────────────────────────────────────


@pytest.fixture
def blob_dir(tmp_path, monkeypatch):
    from store.blobs import BlobStore

    return BlobStore(local_only=True, local_dir=tmp_path / "data")


def test_원문_경로가_중복되지_않는다(blob_dir):
    """local_dir 이 raw 로 끝나면 data/raw/raw/... 가 된다."""
    from store.blobs import blob_key

    path = blob_dir._local_path(blob_key("2026-09-12"))
    assert "raw" in str(path)
    assert str(path).count("raw") == 1, path


def test_오래된_원문_파일을_지운다(blob_dir):
    """이걸 안 지우면 디스크가 조용히 차오른다."""
    from datetime import date, timedelta

    old = (date.today() - timedelta(days=200)).isoformat()
    recent = (date.today() - timedelta(days=3)).isoformat()
    blob_dir.append_day(old, [{"id": "a", "title": "옛날 기사"}])
    blob_dir.append_day(recent, [{"id": "b", "title": "최근 기사"}])
    assert blob_dir.usage()[0] == 2

    removed, freed = blob_dir.prune(90)
    assert removed == 1 and freed > 0
    assert blob_dir.usage()[0] == 1
    assert list(blob_dir.read_day(recent)), "최근 것은 남아야 한다"
    assert list(blob_dir.read_day(old)) == []


def test_원문_정리_dry_run은_지우지_않는다(blob_dir):
    from datetime import date, timedelta

    old = (date.today() - timedelta(days=200)).isoformat()
    blob_dir.append_day(old, [{"id": "a"}])
    removed, _ = blob_dir.prune(90, dry_run=True)
    assert removed == 1
    assert blob_dir.usage()[0] == 1, "dry-run 인데 지웠다"


def test_오프라인_배치_파일이_정리된다(tmp_path, monkeypatch):
    import os
    import time

    from llm.fake_client import prune_batches

    monkeypatch.setenv("NEWS_LENS_FAKE_DIR", str(tmp_path / "fake"))
    (tmp_path / "fake").mkdir(parents=True, exist_ok=True)
    old = tmp_path / "fake" / "fake_batch_old_001.json"
    old.write_text("[]", encoding="utf-8")
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    new = tmp_path / "fake" / "fake_batch_new_002.json"
    new.write_text("[]", encoding="utf-8")

    removed, _ = prune_batches(days=2)
    assert removed == 1
    assert new.exists() and not old.exists()


def test_step9가_디스크도_정리한다(populated, config_dir, tmp_path, monkeypatch, capsys):
    """DB 만 정리하고 파일을 놔두면 반쪽짜리다."""
    monkeypatch.setattr("store.blobs.LOCAL_DIR", tmp_path / "data")
    step9_prune.main(["--db", str(populated), "--date", DATE, "--report"])
    out = capsys.readouterr().out
    assert "디스크 정리" in out
    assert "원문 파일" in out
