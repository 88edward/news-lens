"""Cloudflare 배포 도구.

이 레포에는 배포 대상이 **둘**이고 명령이 서로 다르다. 섞으면 조용히
엉뚱한 곳에 올라가므로 한 군데로 모은다.

    site/dist/      정적 사이트   wrangler pages deploy
    site/worker/    검색 API      wrangler deploy      (선택, D1 필요)

사용법:

    python -m scripts.deploy                  프리뷰 배포 (기본, 안전)
    python -m scripts.deploy --build          빌드부터 다시 하고 배포
    python -m scripts.deploy --production     프로덕션 배포
    python -m scripts.deploy --branch demo    임의 이름의 프리뷰
    python -m scripts.deploy --worker         Worker 배포
    python -m scripts.deploy --dry-run        실행할 명령만 보여준다

기본이 프리뷰인 이유: 프리뷰는 별도 URL 로 올라가 프로덕션을 건드리지 않는다.
여러 번 시험 배포하는 게 정상적인 작업 흐름이고, 그때마다 실서비스가
바뀌면 안 된다.

프로덕션 배포는 산출물에 샘플 데이터 흔적이 있으면 **거부한다.**
`--from-fixtures` 빌드는 구조가 진짜와 똑같아서 눈으로 구분되지 않는다.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import config

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "site" / "dist"
WORKER = REPO_ROOT / "site" / "worker"

EXIT_OK = 0
EXIT_FAIL = 1


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def deploy_config() -> dict:
    return (config.pipeline().get("site") or {}).get("deploy") or {}


# ── wrangler 찾기 ───────────────────────────────────────────────────────


def wrangler_cmd() -> list[str]:
    """전역 설치가 있으면 그걸, 없으면 npx 로 떨어진다.

    Windows 에서 wrangler 는 .cmd 셸이라 shutil.which 가 확장자까지 찾아 준다.
    """
    found = shutil.which("wrangler")
    if found:
        return [found]
    if shutil.which("npx"):
        log("[deploy] 전역 wrangler 가 없다 — npx 로 실행한다 (느릴 수 있다)")
        return ["npx", "--yes", "wrangler"]
    raise RuntimeError(
        "wrangler 를 찾지 못했다. `npm install -g wrangler` 하거나 Node 를 설치해라."
    )


def dist_arg() -> str:
    """wrangler 에 넘길 산출물 경로.

    레포 안이면 짧은 상대경로로 줘서 로그가 읽히게 한다. 밖이면 절대경로다 —
    relative_to 를 그냥 부르면 ValueError 로 죽는다.
    """
    try:
        return str(DIST.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(DIST)


def run(cmd: list[str], *, cwd: Path | None = None, dry_run: bool = False) -> int:
    printable = " ".join(cmd)
    log(f"[deploy] $ {printable}" + (f"   (cwd={cwd})" if cwd else ""))
    if dry_run:
        log("[deploy] dry-run — 실행하지 않는다")
        return EXIT_OK
    return subprocess.call(cmd, cwd=str(cwd) if cwd else None)


# ── 산출물 점검 ─────────────────────────────────────────────────────────


def find_sample_markers(root: Path) -> dict[str, int]:
    """샘플 데이터 흔적을 센다. 빈 dict 면 진짜 데이터로 본다."""
    markers = [str(m) for m in (deploy_config().get("sample_markers") or [])]
    if not markers:
        return {}

    hits: dict[str, int] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in (".html", ".json"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in markers:
            if marker in text:
                hits[marker] = hits.get(marker, 0) + 1
    return hits


def check_dist(*, production: bool, allow_sample: bool) -> None:
    if not DIST.exists():
        raise RuntimeError(
            f"{DIST} 가 없다. 먼저 빌드해라:\n"
            "  python -m pipeline.step8_build_site            (진짜 데이터)\n"
            "  python -m pipeline.step8_build_site --from-fixtures   (샘플)\n"
            "또는 이 명령에 --build 를 붙여라."
        )
    if not (DIST / "index.html").exists():
        raise RuntimeError(f"{DIST} 에 index.html 이 없다. 빌드가 깨졌다.")

    files = [p for p in DIST.rglob("*") if p.is_file()]
    size = sum(p.stat().st_size for p in files)
    log(f"[deploy] 산출물 {len(files)}개 · {size / 1024:.0f}KB")

    hits = find_sample_markers(DIST)
    if not hits:
        return

    detail = ", ".join(f"{m} ({n}개 파일)" for m, n in hits.items())
    if not production:
        log(f"[deploy] 참고: 샘플 데이터가 들어 있다 — {detail}")
        log("[deploy] 프리뷰라 그대로 올린다. 프로덕션에는 올라가지 않는다.")
        return

    if allow_sample:
        log(f"[deploy] 경고: --allow-sample 로 샘플 데이터를 프로덕션에 올린다 — {detail}")
        return

    raise RuntimeError(
        f"프로덕션 배포를 막는다: 산출물에 샘플 데이터가 있다 — {detail}\n"
        "\n"
        "--from-fixtures 빌드는 구조가 진짜와 똑같아 눈으로 구분되지 않는다.\n"
        "진짜 데이터로 다시 빌드해라:\n"
        "  python -m pipeline.step8_build_site\n"
        "\n"
        "알면서 올리는 거라면 --allow-sample 을 붙여라."
    )


# ── 배포 ────────────────────────────────────────────────────────────────


def deploy_pages(args) -> int:
    cfg = deploy_config()
    project = cfg.get("project_name") or "news-lens"
    production_branch = cfg.get("production_branch") or "main"

    if args.branch:
        branch = args.branch
    elif args.production:
        branch = production_branch
    else:
        branch = cfg.get("preview_branch") or "preview"

    production = branch == production_branch
    check_dist(production=production, allow_sample=args.allow_sample)

    log(
        f"[deploy] Pages · 프로젝트 {project} · 브랜치 {branch}"
        f" · {'프로덕션' if production else '프리뷰'}"
    )

    cmd = [
        *wrangler_cmd(),
        "pages",
        "deploy",
        dist_arg(),
        f"--project-name={project}",
        f"--branch={branch}",
    ]
    code = run(cmd, cwd=REPO_ROOT, dry_run=args.dry_run)
    if code == 0 and not args.dry_run:
        log("")
        log(f"[deploy] 완료. 프로덕션 URL: https://{project}.pages.dev")
        if not production:
            log(f"[deploy] 이번 프리뷰: https://{branch}.{project}.pages.dev")
    return code


def deploy_worker(args) -> int:
    """site/worker/ — 검색 API. 기본 구성에서는 쓰지 않는다."""
    if not (WORKER / "wrangler.toml").exists():
        raise RuntimeError(f"{WORKER}/wrangler.toml 이 없다")

    toml = (WORKER / "wrangler.toml").read_text(encoding="utf-8")
    if "REPLACE_WITH_YOUR_D1_ID" in toml:
        raise RuntimeError(
            "site/worker/wrangler.toml 의 database_id 가 자리표시자 그대로다.\n"
            "D1 을 만들고 id 를 채워라:\n"
            "  wrangler d1 create news-lens\n"
            "\n"
            "Worker 는 선택 기능이다. 기본 구성(Pages 정적 사이트)에는 필요 없다."
        )

    log("[deploy] Worker · site/worker")
    # Workers 는 `deploy`, Pages 는 `pages deploy`. 이걸 섞으면 안 된다.
    return run([*wrangler_cmd(), "deploy"], cwd=WORKER, dry_run=args.dry_run)


# ── CLI ─────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cloudflare 에 배포한다 (기본: 프리뷰)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--production",
        action="store_true",
        help="프로덕션 배포. 샘플 데이터가 있으면 거부한다.",
    )
    p.add_argument(
        "--branch",
        default=None,
        help="배포할 브랜치 이름. 프로덕션 브랜치가 아니면 프리뷰 URL 로 간다.",
    )
    p.add_argument(
        "--build", action="store_true", help="배포 전에 step8 로 다시 빌드한다"
    )
    p.add_argument(
        "--from-fixtures",
        action="store_true",
        help="--build 와 함께: 샘플 데이터로 빌드한다 (프리뷰 전용)",
    )
    p.add_argument("--worker", action="store_true", help="Pages 대신 Worker 를 배포한다")
    p.add_argument(
        "--allow-sample",
        action="store_true",
        help="샘플 데이터인 걸 알면서 프로덕션에 올린다",
    )
    p.add_argument("--dry-run", action="store_true", help="실행할 명령만 출력한다")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.build:
            from pipeline import step8_build_site

            extra = ["--from-fixtures"] if args.from_fixtures else []
            log(f"[deploy] 빌드 중{' (샘플 데이터)' if extra else ''}…")
            code = step8_build_site.main(extra)
            if code != EXIT_OK:
                log("[deploy] 빌드가 실패해 배포하지 않는다")
                return code

        return deploy_worker(args) if args.worker else deploy_pages(args)
    except RuntimeError as exc:
        log(f"[deploy] {exc}")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
