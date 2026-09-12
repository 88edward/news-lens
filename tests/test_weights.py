"""step0 / weights 계층 테스트.

완료 조건 3개를 그대로 옮겼다:
  1. fixture 로 step0 을 돌려 country_weights 가 채워진다
  2. surge 가 '평소 10 → 어제 40' 은 크게, '평소 500 → 어제 520' 은 작게 나온다
  3. manual_provider 를 켜면 최종 weight 순위가 실제로 바뀐다
"""
from __future__ import annotations

import json

import pytest

import config
from pipeline import step0_weights
from store.db import connect
from weights import blend as blend_mod
from weights.base import VolumeSeries, get_provider, provider_names
from weights.blend import compute_surge, normalize_attention

FIXTURE = "tests/fixtures/gdelt_timelinesourcecountry.json"


@pytest.fixture
def db(tmp_path):
    d = connect(tmp_path / "test.db")
    yield d
    d.close()


# ── provider 인터페이스 ─────────────────────────────────────────────────


def test_provider가_자동_발견된다(config_dir):
    assert {"gdelt", "manual"} <= set(provider_names())


def test_gdelt_파서가_영문_이름을_FIPS로_되돌린다(config_dir):
    provider = get_provider("gdelt", {"fixture": FIXTURE})
    series = provider.fetch_series(7)
    assert "US" in series.daily        # United States
    assert "KS" in series.daily        # South Korea → KS, KR 이 아니다
    assert "CH" in series.daily        # China → CH
    assert "AS" in series.daily        # Australia → AS, AU 가 아니다
    assert len(series.daily["US"]) == 7


def test_모르는_국가명은_버려지되_경고를_남긴다(config_dir):
    provider = get_provider("gdelt", {"fixture": FIXTURE})
    series = provider.fetch_series(7)
    assert any("FIPS로 못 되돌린" in w for w in series.warnings)


def test_테마가_비면_거부한다(config_dir):
    config_dir.patch("sources", lambda d: d["gdelt"].update({"themes_for_weights": []}))
    provider = get_provider("gdelt", {"fixture": FIXTURE})
    with pytest.raises(ValueError, match="themes_for_weights"):
        provider.query_string()


def test_요청_실패시_이전_주_값을_재사용한다(config_dir, db, tmp_path):
    db.save_weights(
        "2026-09-01",
        [{"fips": "US", "iso2": "US", "attention": 1.0, "weight": 1.0},
         {"fips": "KS", "iso2": "KR", "attention": 0.4, "weight": 0.4}],
    )
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"timeline": []}), encoding="utf-8")

    provider = get_provider("gdelt", {"fixture": empty, "db": db})
    series = provider.fetch_series(7)
    assert series.stale is True
    assert set(series.daily) == {"US", "KS"}
    assert any("재사용" in w for w in series.warnings)


def test_이전_값도_없으면_빈_시리즈와_경고(config_dir, db, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"timeline": []}), encoding="utf-8")
    series = get_provider("gdelt", {"fixture": empty, "db": db}).fetch_series(7)
    assert series.is_empty() and series.stale


# ── surge ───────────────────────────────────────────────────────────────


def test_surge_평소10_어제40은_크고_평소500_어제520은_작다(config_dir):
    """완료 조건 그대로."""
    series = VolumeSeries(
        daily={
            "AR": [10, 10, 10, 10, 10, 10, 40],    # 평소 10 → 어제 40
            "US": [500, 500, 500, 500, 500, 500, 520],  # 평소 500 → 어제 520
        }
    )
    surge, raw = compute_surge(series)
    assert raw["AR"] > 2.5
    assert raw["US"] < 1.1
    assert surge["AR"] > 0.7
    assert surge["US"] < 0.1
    assert surge["AR"] > surge["US"] * 5


def test_surge는_0과_1_사이에_머문다(config_dir):
    series = VolumeSeries(daily={"XX": [1, 1, 1, 1, 1, 1, 10_000], "YY": [100, 100, 0]})
    surge, _ = compute_surge(series)
    assert all(0.0 <= v <= 1.0 for v in surge.values())


def test_볼륨이_0인_나라는_surge가_0(config_dir):
    surge, raw = compute_surge(VolumeSeries(daily={"ZZ": [0, 0, 0]}))
    assert surge["ZZ"] == 0.0 and raw["ZZ"] == 0.0


def test_attention은_최상위가_1이_된다(config_dir):
    norm = normalize_attention({"US": 500.0, "KS": 100.0, "XX": 0.0})
    assert norm["US"] == pytest.approx(1.0)
    assert norm["KS"] == pytest.approx(0.2)
    assert norm["XX"] == 0.0


# ── blend ───────────────────────────────────────────────────────────────


def test_blend가_두_신호를_계수대로_섞는다(config_dir):
    results = blend_mod.blend(
        [get_provider("gdelt", {"fixture": FIXTURE})], options={"fixture": FIXTURE}
    )
    assert results
    top = results[0]
    assert 0.0 <= top.weight <= 1.0
    assert top.weight == pytest.approx(1.0)  # 최상위는 1.0 으로 정규화된다
    assert all(r.provider_breakdown["gdelt"]["blend"] == 1.0 for r in results)


def test_급등한_나라가_attention만으로는_못_올라갈_순위로_올라온다(config_dir):
    """surge 의 존재 이유. 계수를 0으로 만들면 순위가 내려가야 한다."""
    provider = get_provider("gdelt", {"fixture": FIXTURE})

    with_surge = {r.fips: i for i, r in enumerate(blend_mod.blend([provider]))}

    config_dir.patch(
        "sources",
        lambda d: d["weights"]["formula"].update(
            {"attention_coef": 1.0, "surge_coef": 0.0}
        ),
    )
    without = {r.fips: i for i, r in enumerate(blend_mod.blend([provider]))}

    # AR(아르헨티나)은 평소 10 → 어제 40. 볼륨은 작지만 급등했다.
    assert with_surge["AR"] < without["AR"], "surge 를 섞으면 순위가 올라와야 한다"


def test_manual_provider가_순위를_바꾼다(config_dir):
    """완료 조건: manual 을 켜서 특정 국가를 올리면 최종 순위가 실제로 바뀐다."""
    gdelt_only = blend_mod.blend([get_provider("gdelt", {"fixture": FIXTURE})])
    before = [r.fips for r in gdelt_only]
    assert before.index("KS") > 3, "전제: 한국은 원래 상위권이 아니다"

    config_dir.patch(
        "sources",
        lambda d: (
            d["weights"]["providers"].__setitem__(
                1, {"name": "manual", "enabled": True, "blend": 0.6}
            ),
            d["weights"]["providers"].__setitem__(
                0, {"name": "gdelt", "enabled": True, "blend": 0.4}
            ),
            d["weights"].update({"manual_scores": {"KS": 10_000}}),
        ),
    )
    after = [r.fips for r in blend_mod.blend(options={"fixture": FIXTURE})]
    assert after[0] == "KS", f"manual 로 올린 한국이 1위여야 한다: {after[:5]}"
    assert after.index("KS") < before.index("KS")


def test_manual에_ISO코드를_적으면_무시하고_FIPS를_알려준다(config_dir, capsys):
    config_dir.patch(
        "sources",
        lambda d: (
            d["weights"]["providers"].__setitem__(
                1, {"name": "manual", "enabled": True, "blend": 1.0}
            ),
            d["weights"].update({"manual_scores": {"KR": 500}}),  # KR 은 ISO
        ),
    )
    scores = get_provider("manual").scores()
    assert scores == {}
    assert "FIPS 는 'KS'" in capsys.readouterr().err


def test_enabled_provider가_없으면_거부한다(config_dir):
    config_dir.patch(
        "sources",
        lambda d: [p.update({"enabled": False}) for p in d["weights"]["providers"]],
    )
    with pytest.raises(ValueError, match="enabled=true"):
        blend_mod.enabled_providers()


def test_countries_yaml에_없는_나라는_결과에_안_들어온다(config_dir):
    results = blend_mod.blend([get_provider("gdelt", {"fixture": FIXTURE})])
    assert all(config.by_fips(r.fips) is not None for r in results)


# ── step0 통합 ──────────────────────────────────────────────────────────


def test_step0_dry_run은_DB에_쓰지_않는다(config_dir, tmp_path):
    path = tmp_path / "dry.db"
    rc = step0_weights.main(
        ["--dry-run", "--fixture", FIXTURE, "--db", str(path), "--date", "2026-09-12"]
    )
    assert rc == 0
    assert connect(path).weights_for("2026-09-12") == []


def test_step0이_country_weights를_채운다(config_dir, tmp_path):
    path = tmp_path / "run.db"
    rc = step0_weights.main(
        ["--fixture", FIXTURE, "--db", str(path), "--date", "2026-09-12"]
    )
    assert rc == 0

    rows = connect(path).weights_for("2026-09-12")
    assert len(rows) >= 25
    assert rows[0]["weight"] == pytest.approx(1.0)
    assert rows[0]["weight"] >= rows[-1]["weight"]

    for row in rows:
        assert 0.0 <= row["attention"] <= 1.0
        assert 0.0 <= row["surge"] <= 1.0
        assert config.by_fips(row["fips"]).iso2 == row["iso2"]
        assert "gdelt" in json.loads(row["provider_breakdown"])


def test_step0은_날짜별로_쌓고_덮어쓰지_않는다(config_dir, tmp_path):
    path = tmp_path / "hist.db"
    step0_weights.main(["--fixture", FIXTURE, "--db", str(path), "--date", "2026-09-05"])
    step0_weights.main(["--fixture", FIXTURE, "--db", str(path), "--date", "2026-09-12"])

    d = connect(path)
    assert len(d.weights_for("2026-09-05")) > 0
    assert len(d.weights_for("2026-09-12")) > 0
    assert d.latest_weight_date() == "2026-09-12"
    assert d.latest_weight_date(before="2026-09-12") == "2026-09-05"


def test_step0_report가_상위국을_출력한다(config_dir, tmp_path, capsys):
    step0_weights.main(
        ["--fixture", FIXTURE, "--db", str(tmp_path / "r.db"), "--report", "--top", "10"]
    )
    out = capsys.readouterr().out
    assert "weight" in out and "attention" in out and "어제/평소" in out
    assert len([ln for ln in out.splitlines() if ln.strip()]) >= 10


def test_step0은_limit을_존중한다(config_dir, tmp_path):
    path = tmp_path / "lim.db"
    step0_weights.main(
        ["--fixture", FIXTURE, "--db", str(path), "--date", "2026-09-12", "--limit", "5"]
    )
    assert len(connect(path).weights_for("2026-09-12")) == 5
