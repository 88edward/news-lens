# news-lens — 프로젝트 규칙

전 세계 뉴스를 **국가별 가중치**로 수집해 LLM으로 분석하고, 3D 글로브 화면이 있는
정적 사이트로 배포하는 파이프라인.

설계 배경은 `docs/DESIGN.md`에 있다. 규칙과 배경이 충돌하면 이 파일이 우선한다.

## 규모와 예산

| 항목 | 값 |
|---|---|
| 수집 | 1,000건/일 (국가별 가중 배분) |
| 룰 필터 후 | ~700건/일 |
| 클러스터링 후 사건 | ~180개/일 |
| LLM 호출 | 사건 단위 180회/일 + 종합 1회/일 |
| LLM 예산 | **월 $25 이내** (기본 설정은 월 $4 수준) |
| 인프라 | 무료 티어만 사용 |
| 필요한 계정 | **GitHub · Google AI Studio · Cloudflare 세 곳** (전부 무료 티어) |

**절대 규칙: 기사 1건마다 LLM을 호출하지 않는다.** 임베딩으로 같은 사건을 묶은 뒤
사건 단위로만 호출한다. 기사 단위로 호출하면 비용이 5배가 된다.

## 스택

- 파이프라인: Python 3.12
- 스케줄러: GitHub Actions 크론 3개
- 저장: SQLite 한 파일. 레포에 **SQL 텍스트 덤프**로 커밋해 실행 간 상태를 잇는다
- 배포: Cloudflare Pages (`wrangler pages deploy site/dist`)
- 프론트: globe.gl (three.js 기반)
- LLM: Gemini 하나 (임베딩·분석·종합 전부)

`site/dist` 는 순수 정적 파일이다 — 빌드 단계도 서버도 없다.
`pages deploy` 와 `wrangler deploy`(Workers, `site/worker/` 용)를 섞지 마라.
손으로 올릴 때는 `scripts/deploy.py` 를 거친다 — 두 명령을 한 군데로 모으고,
샘플 데이터가 프로덕션으로 가는 것을 막는다. 기본은 프리뷰다.

계정을 늘리는 선택(Anthropic, Cloudflare R2/Workers, Turso)은 전부 **선택**이고
설정 파일에서만 켠다.

## 프로젝트 규칙

### 설정과 프롬프트

- **모델 ID와 프롬프트 문구를 코드에 하드코딩 금지.**
  모델은 `config/models.yaml`, 지시문은 `prompts/*.md` 에만 둔다.
  파이썬 파일 안에 `"claude-..."` 같은 문자열이 나타나면 그 자체로 규칙 위반이다.
- 임계값·상한·기간 같은 숫자도 코드에 박지 않는다. `config/pipeline.yaml` 에서 읽는다.

### LLM 접근

- **LLM 호출은 `llm/` 의 어댑터를 통해서만 한다.**
  `pipeline/` 코드가 `anthropic`·`openai`·`google` SDK를 직접 import 하면 안 된다.
  벤더 SDK가 존재해도 되는 유일한 디렉토리는 `llm/` 이다.
- 역할(role) 이름으로만 클라이언트를 얻는다: `registry.get_client("event_analysis")`.
  역할을 추가할 때 `registry.py` 를 고칠 필요가 없어야 한다.
- **모든 LLM 호출은 Batch API를 기본으로 한다.**
  동기 호출은 `models.yaml` 에 `mode: sync` 로 명시한 역할만 허용한다.

### 국가 관심도 신호

- **국가 관심도 신호는 `weights/` 의 provider 인터페이스를 통해서만 얻는다.**
  `pipeline/` 코드가 특정 데이터 소스(GDELT 등)를 직접 알면 안 된다.
- provider는 원시 볼륨만 돌려주고, 정규화·혼합은 `weights/blend.py` 가 담당한다.

### 데이터 수집 윤리

- **이용약관이 자동 접근을 금지하는 사이트를 스크래핑하지 않는다.**
  Investing.com은 공개 API가 없고 약관상 프로그래밍 방식 접근이 금지되어 있다.
  **절대 대상으로 삼지 마라.** 스크래퍼를 이 레포에 넣지 마라.
- 직접 크롤링이 필요하면 먼저 `robots.txt` 를 확인한다.
- **사이트에는 기사 원문을 싣지 않는다. 요약 + 출처 링크만.**

### 파이프라인 구조

- **`pipeline/step*.py` 는 각각 단독 실행 가능해야 한다.**
  이전 단계 결과를 인자로 받지 않고 DB나 R2에서 읽는다.
  `python -m pipeline.step4_cluster` 만으로 돌아야 한다.
- **모든 step은 `--dry-run` 과 `--limit N` 을 지원한다.**
  API 키가 하나도 없어도 `--dry-run` 이 끝까지 돌아야 한다.
- 파일명 앞의 번호가 실행 순서다. 번호를 건너뛰는 의존을 만들지 마라.

### 저장

- **파생 데이터는 SQLite 한 파일.** 계정이 필요 없다.
- **러너는 매 실행마다 새로 뜬다.** 상태를 잇는 유일한 수단은 레포 커밋이다.
  `store/dump.py` 가 SQL 텍스트로 내보내고, 다음 실행이 복원한다.
  이걸 빠뜨리면 step1이 모은 기사가 step2 실행에는 존재하지 않는다.
- **바이너리 DB 파일을 커밋하지 않는다.** git은 바이너리를 델타 압축하지 못해
  한 행만 바뀌어도 히스토리에 파일 전체가 쌓인다. 텍스트 덤프를 커밋한다.
- **DB 크기는 우리 책임이다.** 레포에 싣는 이상 방치하면 한 해 만에 클론이
  불가능해진다. `step9_prune` 이 매일 정리한다 — 특히 임베딩은 클러스터링이
  끝나면 지운다(가장 큰 덩어리다).
- **기사 원문은 절대 git에 커밋하지 않는다.** R2를 붙였다면 거기로, 아니면
  `data/raw/`에 두고 gitignore 한다. GDELT는 본문을 주지 않으므로
  이 아카이브 없이도 파이프라인은 완결된다.
- `embeddings` 테이블에는 반드시 `model` 컬럼을 둔다.
  임베딩 모델이 바뀌면 기존 벡터와 거리 계산이 호환되지 않는다.
- 걸러낸 기사는 이유와 함께 `filter_log` 에 남긴다.

### 비용 통제

- **비용이 드는 호출 전에 예상 토큰 수와 예상 비용을 stderr에 찍는다.**
- `config/pipeline.yaml` 의 `max_daily_cost_usd` 를 넘으면 **코드가 중단시킨다.**
  경고만 찍고 진행하는 것은 통제가 아니다.
- 모든 호출의 usage를 `runs` 테이블에 누적한다.

### 국가 코드

- **GDELT는 FIPS 10-4 국가코드를 쓴다. ISO 3166-1 과 다르다.**
  한국 ISO=`KR` / FIPS=`KS`, 중국 ISO=`CN` / FIPS=`CH` (ISO에서 `CH`는 스위스).
- 두 코드 사이의 변환은 항상 `config/countries.yaml` 의 매핑을 경유한다.
  코드 안에서 `iso.upper()` 같은 임시 변환을 만들지 마라.

### 커밋

- **커밋은 단계별로 작게.** 한 커밋에 여러 step을 섞지 않는다.
- 각 프롬프트 단계가 끝나면 테스트를 돌리고 커밋한 뒤 다음으로 넘어간다.

## 디렉토리 구조

구조는 `docs/DESIGN.md` 3장을 따른다. 핵심 원칙:

> **모델을 바꿀 때 파이썬 파일을 열 일이 없어야 한다.**

## 검증 기준

새 코드를 추가할 때마다 아래가 계속 참인지 확인한다.

1. `models.yaml` 의 `event_analysis.provider` 를 `anthropic` → `gemini` 로 한 줄
   바꿨을 때, 파이썬 코드를 한 줄도 안 고치고 테스트가 통과한다.
2. `grep -rn "claude-\|gpt-\|gemini-" pipeline/ weights/ store/ site/` 가 비어 있다.
3. `grep -rn "^import anthropic\|^import openai\|^from anthropic\|^from openai" pipeline/` 이 비어 있다.
4. 모든 step이 API 키 없이 `--dry-run` 으로 돈다.
5. `pytest` 가 실제 네트워크 호출 없이 통과한다.
6. 모든 워크플로 job이 `db-restore` 로 시작하고 `db-commit` 으로 끝난다.
   하나라도 빠지면 그 실행의 결과가 사라진다.
7. 워크플로가 참조하는 시크릿이 전부 `.env.example` 에 있다
   (GitHub 이 자동 제공하는 `GITHUB_TOKEN` 은 예외).
   문서에 없는 서비스를 코드가 요구하거나, 구현하지 않은 서비스를
   문서가 약속하면 안 된다. 현재 필수는 `GEMINI_API_KEY` 와
   `CLOUDFLARE_API_TOKEN` · `CLOUDFLARE_ACCOUNT_ID` 세 개다.
