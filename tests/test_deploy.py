"""배포 도구 테스트.

여기서 지키려는 것: **샘플 데이터가 프로덕션에 올라가지 않는다.**
`--from-fixtures` 빌드는 구조가 진짜와 똑같아서 눈으로 구분되지 않는다.
한 번 올라가면 실서비스에 가짜 기사가 뜬다.
"""
from __future__ import annotations

import pytest

import config
from scripts import deploy

DATE = "2026-09-27"


@pytest.fixture
def dist(tmp_path, monkeypatch):
    """가짜 빌드 산출물. 내용은 테스트가 채운다."""
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("<html>진짜 사이트</html>", encoding="utf-8")
    (root / "data").mkdir()
    monkeypatch.setattr(deploy, "DIST", root)
    return root


def real_data(dist):
    (dist / "data" / "country" ).mkdir(parents=True, exist_ok=True)
    (dist / "data" / "country" / "US.json").write_text(
        '{"events":[{"sources":[{"domain":"reuters.com"}]}]}', encoding="utf-8"
    )


def sample_data(dist):
    (dist / "data" / "country").mkdir(parents=True, exist_ok=True)
    (dist / "data" / "country" / "US.json").write_text(
        '{"events":[{"sources":[{"domain":"example-news-1.com"}]}]}', encoding="utf-8"
    )


def run(argv):
    return deploy.main(argv)


# ── 샘플 데이터 감지 ────────────────────────────────────────────────────


def test_샘플_마커를_찾아낸다(dist, config_dir):
    sample_data(dist)
    hits = deploy.find_sample_markers(dist)
    assert "example-news-" in hits


def test_진짜_데이터는_마커가_없다(dist, config_dir):
    real_data(dist)
    assert deploy.find_sample_markers(dist) == {}


def test_마커는_설정에서_온다(dist, config_dir):
    """코드에 문자열을 박으면 샘플 형태가 바뀔 때 조용히 못 잡는다."""
    (dist / "data" / "x.json").write_text('{"a":"내부전용표식"}', encoding="utf-8")
    assert deploy.find_sample_markers(dist) == {}

    config_dir.patch(
        "pipeline",
        lambda d: d["site"]["deploy"].update({"sample_markers": ["내부전용표식"]}),
    )
    assert "내부전용표식" in deploy.find_sample_markers(dist)


# ── 프로덕션 가드 ───────────────────────────────────────────────────────


def test_샘플_데이터는_프로덕션에_못_올라간다(dist, config_dir, capsys):
    sample_data(dist)
    assert run(["--production", "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "프로덕션 배포를 막는다" in err
    assert "--allow-sample" in err, "빠져나갈 방법을 알려 줘야 한다"


def test_프리뷰는_샘플이어도_올라간다(dist, config_dir, capsys):
    """여러 번 시험 배포하는 게 정상 작업이다. 프리뷰까지 막으면 못 쓴다."""
    sample_data(dist)
    assert run(["--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "프리뷰" in err
    assert "--branch=preview" in err


def test_allow_sample로_알고_올릴_수_있다(dist, config_dir, capsys):
    sample_data(dist)
    assert run(["--production", "--allow-sample", "--dry-run"]) == 0
    assert "경고" in capsys.readouterr().err


def test_진짜_데이터는_프로덕션에_올라간다(dist, config_dir, capsys):
    real_data(dist)
    assert run(["--production", "--dry-run"]) == 0
    assert "--branch=main" in capsys.readouterr().err


# ── 브랜치 선택 ─────────────────────────────────────────────────────────


def test_기본은_프리뷰다(dist, config_dir, capsys):
    """기본이 프로덕션이면 실수 한 번으로 실서비스가 바뀐다."""
    real_data(dist)
    run(["--dry-run"])
    err = capsys.readouterr().err
    assert "--branch=preview" in err
    assert "--branch=main" not in err


def test_임의_브랜치는_프리뷰로_간다(dist, config_dir, capsys):
    real_data(dist)
    run(["--branch", "demo", "--dry-run"])
    err = capsys.readouterr().err
    assert "--branch=demo" in err and "프리뷰" in err


def test_프로덕션_브랜치를_직접_주면_프로덕션이다(dist, config_dir, capsys):
    """--branch main 은 --production 과 같은 뜻이어야 한다. 가드도 걸려야 한다."""
    sample_data(dist)
    assert run(["--branch", "main", "--dry-run"]) == 1
    assert "프로덕션 배포를 막는다" in capsys.readouterr().err


def test_프로젝트_이름은_설정에서_온다(dist, config_dir, capsys):
    real_data(dist)
    config_dir.patch(
        "pipeline", lambda d: d["site"]["deploy"].update({"project_name": "다른이름"})
    )
    run(["--dry-run"])
    assert "--project-name=다른이름" in capsys.readouterr().err


# ── 산출물 점검 ─────────────────────────────────────────────────────────


def test_dist가_없으면_빌드법을_알려준다(tmp_path, monkeypatch, config_dir, capsys):
    monkeypatch.setattr(deploy, "DIST", tmp_path / "없음")
    assert run(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "step8_build_site" in err and "--build" in err


def test_index_html이_없으면_거부한다(dist, config_dir, capsys):
    (dist / "index.html").unlink()
    assert run(["--dry-run"]) == 1
    assert "index.html" in capsys.readouterr().err


# ── Pages 와 Worker 를 섞지 않는다 ──────────────────────────────────────


def test_Pages는_pages_deploy를_쓴다(dist, config_dir, capsys):
    real_data(dist)
    run(["--dry-run"])
    err = capsys.readouterr().err
    assert "pages deploy" in err, "`wrangler deploy` 는 Workers 용이다"


def test_Worker는_D1이_없으면_거부한다(config_dir, capsys):
    """자리표시자 그대로 배포하면 런타임에 죽는다."""
    assert run(["--worker", "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "database_id" in err and "d1 create" in err


# ── 빌드 연동 ───────────────────────────────────────────────────────────


def test_빌드가_실패하면_배포하지_않는다(dist, config_dir, monkeypatch, capsys):
    from pipeline import step8_build_site

    monkeypatch.setattr(step8_build_site, "main", lambda argv=None: 1)
    assert run(["--build", "--dry-run"]) == 1
    assert "빌드가 실패해 배포하지 않는다" in capsys.readouterr().err


def test_wrangler가_없으면_npx로_떨어진다(monkeypatch):
    calls = []
    monkeypatch.setattr(
        deploy.shutil, "which", lambda name: (calls.append(name), None)[1]
        if name == "wrangler"
        else "/usr/bin/npx"
    )
    assert deploy.wrangler_cmd()[:2] == ["npx", "--yes"]


def test_wrangler도_npx도_없으면_설치법을_알려준다(monkeypatch):
    monkeypatch.setattr(deploy.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="npm install -g wrangler"):
        deploy.wrangler_cmd()
