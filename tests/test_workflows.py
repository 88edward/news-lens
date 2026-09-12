"""워크플로 문법과 의도 검증.

완료 조건: 세 yml 이 유효하고, job 간 의존과 재시도 로직이 의도대로 걸려 있다.

act 를 CI 에서 돌리려면 도커가 필요하므로, 여기서는 파싱 + 구조 단언으로
같은 것을 확인한다. 문법 오류는 물론이고 "조용히 잘못 도는" 설정도 잡는다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
ACTION = ROOT / ".github" / "actions" / "report-failure" / "action.yml"

WORKFLOWS = ["weights", "collect", "analyze"]


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / f"{name}.yml").read_text(encoding="utf-8"))


def raw(name: str) -> str:
    return (WORKFLOW_DIR / f"{name}.yml").read_text(encoding="utf-8")


def triggers(wf: dict) -> dict:
    # YAML 1.1 은 따옴표 없는 on 을 boolean True 로 읽는다. 둘 다 받는다.
    return wf.get("on") or wf.get(True)


# ── 기본 ────────────────────────────────────────────────────────────────


def test_워크플로가_세_개다():
    files = {p.stem for p in WORKFLOW_DIR.glob("*.yml")}
    assert files == set(WORKFLOWS), files


@pytest.mark.parametrize("name", WORKFLOWS)
def test_유효한_yaml이다(name):
    wf = load(name)
    assert wf["name"] == name
    assert wf["jobs"]


@pytest.mark.parametrize("name", WORKFLOWS)
def test_YAML_앵커를_쓰지_않는다(name):
    """GitHub Actions 는 앵커(&, *, <<)를 지원하지 않는다. 쓰면 파싱 에러다."""
    text = raw(name)
    assert not re.search(r"^\s+\w+: &\w+", text, re.MULTILINE), "앵커 정의"
    assert not re.search(r"^\s+<<: \*", text, re.MULTILINE), "머지 키"
    assert not re.search(r"^\s+env: \*\w+", text, re.MULTILINE), "별칭 참조"


@pytest.mark.parametrize("name", WORKFLOWS)
def test_중복_실행을_막는다(name):
    """크론이 겹쳐 돌면 같은 기사를 두 번 수집하거나 배치를 두 번 제출한다."""
    wf = load(name)
    assert wf["concurrency"]["group"]
    assert wf["concurrency"]["cancel-in-progress"] is False


@pytest.mark.parametrize("name", WORKFLOWS)
def test_수동_실행이_가능하다(name):
    assert "workflow_dispatch" in triggers(load(name))


@pytest.mark.parametrize("name", WORKFLOWS)
def test_이슈를_만들_권한이_있다(name):
    assert load(name)["permissions"]["issues"] == "write"


@pytest.mark.parametrize("name", WORKFLOWS)
def test_실패하면_이슈를_만든다(name):
    """크론은 조용히 죽는다."""
    wf = load(name)
    for job in wf["jobs"].values():
        reporters = [
            s for s in job["steps"]
            if s.get("uses") == "./.github/actions/report-failure"
        ]
        assert reporters, f"{name}: 실패 보고 단계가 없다"
        for step in reporters:
            assert step["if"] == "failure()"
            assert step["with"]["stage"]


@pytest.mark.parametrize("name", WORKFLOWS)
def test_타임아웃이_걸려있다(name):
    """러너가 무한정 돌면 private 레포의 분이 녹는다."""
    for job in load(name)["jobs"].values():
        assert job["timeout-minutes"] <= 90


# ── 크론 스케줄 ─────────────────────────────────────────────────────────


def test_weights는_주1회다():
    schedules = [s["cron"] for s in triggers(load("weights"))["schedule"]]
    assert schedules == ["0 18 * * 0"]      # 월요일 03:00 KST


def test_collect는_6시간마다다():
    """RSS 는 최신 N건만 노출한다. 하루 1회면 발행량 많은 매체를 놓친다."""
    schedules = [s["cron"] for s in triggers(load("collect"))["schedule"]]
    assert schedules == ["5 */6 * * *"]


def test_analyze는_제출과_수거가_다른_크론이다():
    schedules = [s["cron"] for s in triggers(load("analyze"))["schedule"]]
    assert schedules == ["10 15 * * *", "10 19 * * *"]  # 00:10 / 04:10 KST


def test_analyze의_두_job이_크론으로_갈린다():
    """한 워크플로에 크론이 둘이면 게이트가 없을 때 두 job 이 매번 다 돈다."""
    jobs = load("analyze")["jobs"]
    assert "10 15 * * *" in jobs["submit"]["if"]
    assert "10 19 * * *" in jobs["publish"]["if"]
    assert "github.event.schedule" in jobs["submit"]["if"]


def test_제출과_수거_사이에_충분한_간격이_있다():
    """Batch API 는 통상 1~4시간 걸린다. 간격이 짧으면 매번 재시도로 들어간다."""
    submit_h, publish_h = 15, 19
    assert publish_h - submit_h >= 4


# ── step 순서 ───────────────────────────────────────────────────────────


def step_commands(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in job["steps"])


def test_submit이_step2부터_step5까지_순서대로_돈다():
    cmds = step_commands(load("analyze")["jobs"]["submit"])
    order = [
        "pipeline.step2_filter",
        "pipeline.step3_embed",
        "pipeline.step4_cluster",
        "pipeline.step5_submit_batch",
    ]
    positions = [cmds.index(step) for step in order]
    assert positions == sorted(positions), "step 순서가 뒤바뀌었다"


def test_publish가_step6부터_step8까지_돈다():
    cmds = step_commands(load("analyze")["jobs"]["publish"])
    for step in ("step6_fetch_batch", "step7_synthesize", "step8_build_site"):
        assert step in cmds


def test_collect는_step1만_돈다():
    cmds = step_commands(load("collect")["jobs"]["collect"])
    assert "pipeline.step1_collect" in cmds
    for other in ("step2_", "step3_", "step4_", "step5_"):
        assert other not in cmds


def test_weights는_step0만_돈다():
    cmds = step_commands(load("weights")["jobs"]["weights"])
    assert "pipeline.step0_weights" in cmds
    assert "step1_collect" not in cmds


# ── 재시도 로직 ─────────────────────────────────────────────────────────


def fetch_step(job: dict) -> dict:
    return next(s for s in job["steps"] if s.get("id") == "fetch")


def test_step6_대기와_실패를_구분한다():
    """exit 75 는 대기, 나머지는 실패. 구분하지 않으면 매일 가짜 이슈가 뜬다."""
    script = fetch_step(load("analyze")["jobs"]["publish"])["run"]
    assert "-eq 0" in script, "0 이면 수거 성공으로 끝내야 한다"
    assert "-ne 75" in script, "75 가 아닌 코드는 즉시 실패로 처리해야 한다"
    assert "exit $code" in script, "실패 코드를 그대로 올려야 한다"
    # set +e 가 없으면 첫 비영(非零) 종료에서 스텝이 죽어 재시도가 무의미해진다
    assert "set +e" in script


def test_step6이_20분_간격으로_최대_3회_재시도한다():
    script = fetch_step(load("analyze")["jobs"]["publish"])["run"]
    assert "sleep 1200" in script, "20분 = 1200초"
    assert "for attempt in 1 2 3" in script, "최대 3회"


def test_재시도가_다_실패하면_빌드를_건너뛴다():
    """배치가 안 끝났는데 빌드하면 어제 내용으로 사이트를 덮어쓴다."""
    job = load("analyze")["jobs"]["publish"]
    for step in job["steps"]:
        run = step.get("run", "") + str(step.get("with", ""))
        if "step8_build_site" in run or "pages deploy" in run:
            assert "steps.fetch.outputs.pending != 'true'" in step["if"]


def test_배포는_dry_run에서_돌지_않는다():
    job = load("analyze")["jobs"]["publish"]
    deploy = next(
        s for s in job["steps"] if "wrangler-action" in str(s.get("uses", ""))
    )
    assert "inputs.dry_run != true" in deploy["if"]


def test_step7이_대기중이어도_빌드를_막지_않는다():
    """브리핑이 없어도 사건 페이지는 구울 수 있다."""
    job = load("analyze")["jobs"]["publish"]
    step7 = next(s for s in job["steps"] if "step7_synthesize" in s.get("run", ""))
    assert "exit 0" in step7["run"]


# ── 시크릿 ──────────────────────────────────────────────────────────────


def test_참조하는_시크릿이_전부_env_example에_있다():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    referenced = set()
    for name in WORKFLOWS:
        referenced |= set(re.findall(r"secrets\.([A-Z0-9_]+)", raw(name)))
    missing = {s for s in referenced if s not in example}
    assert not missing, f".env.example 에 없는 시크릿: {missing}"


def test_env_example이_키_없이도_돈다고_알린다():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "--dry-run" in text
    assert "커밋하지 않는다" in text


# ── composite action ────────────────────────────────────────────────────


def test_실패보고_액션이_유효하다():
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    assert action["runs"]["using"] == "composite"
    assert set(action["inputs"]) == {"stage", "note"}


def test_같은_날_같은_단계는_이슈를_새로_만들지_않는다():
    """collect 는 6시간마다 돈다. 매번 새 이슈면 알림이 무의미해진다."""
    text = ACTION.read_text(encoding="utf-8")
    assert "createComment" in text and "issues.create" in text
    assert "listForRepo" in text
