# news-lens

전 세계 뉴스를 **국가별 관심도 가중치**로 수집해 사건 단위로 LLM 분석하고,
3D 글로브 화면이 있는 정적 사이트로 배포하는 파이프라인.

```
GDELT 2.0 API ─┐
매체 RSS      ─┤
               ▼
        [ GitHub Actions · Python ]
        step0  국가 가중치 계산       ← 주 1회
        step1  가중치 비례 수집        ← 6시간마다
        step2  룰 필터 (LLM 없음)      ┐
        step3  임베딩 (512d int8)      │ 00:10 KST
        step4  클러스터링 → 사건        │
        step5  Batch 제출 후 종료       ┘
        ·  ·  ·  배치 대기 ~4h (러너 꺼짐, 과금 0)  ·  ·  ·
        step6  Batch 수거              ┐
        step7  일일 종합 1회            │ 04:10 KST
        step8  글로브 JSON + 정적 빌드   ┘
               ▼
        SQLite → SQL 덤프로 레포에 커밋
               ▼
        Cloudflare Pages (정적)
```

**필요한 계정은 세 곳이고 전부 무료 티어다.** GitHub(크론) · Google AI Studio(LLM) ·
Cloudflare(배포). GDELT·RSS는 인증이 없고, 저장소는 파일 하나다.

**핵심 규칙: 기사 1건마다 LLM을 호출하지 않는다.** 임베딩으로 같은 사건을 묶은 뒤
사건 단위로만 호출한다. 이 한 가지가 비용을 4~5배 가른다.
하루 1,000건 수집 → 필터 후 ~700건 → 사건 ~180개 → LLM 호출 181회.

설계 배경은 [docs/DESIGN.md](docs/DESIGN.md), 프로젝트 규칙은 [CLAUDE.md](CLAUDE.md).

---

## 처음 띄우기

1. 이 레포를 자기 GitHub 계정으로 fork 하거나 push 한다.
2. <https://aistudio.google.com/apikey> 에서 키를 받는다 (무료).
3. Cloudflare Pages 프로젝트를 한 번 만든다. 이름은 `news-lens`.

   ```bash
   npx wrangler pages project create news-lens --production-branch=main
   ```

   대시보드에서 만들어도 된다. **미리 만들어 두지 않으면** CI 는 대화형
   프롬프트를 띄울 수 없어 그대로 실패한다.
4. Cloudflare 대시보드에서 API 토큰을 발급한다
   (권한: Account → Cloudflare Pages → Edit). 계정 ID 도 같이 복사한다.
5. 레포 Settings → Secrets and variables → Actions → New repository secret
   에 세 개를 등록한다:
   `GEMINI_API_KEY` · `CLOUDFLARE_API_TOKEN` · `CLOUDFLARE_ACCOUNT_ID`
6. Actions 탭에서 순서대로 한 번씩 수동 실행한다:
   `weights` → `collect` → `analyze`(job=submit) → `analyze`(job=publish)
7. 이후로는 크론이 알아서 돈다.

각 실행이 끝나면 `data/news-lens.sql` 에 새 커밋이 올라온다. 그게 파이프라인의
상태다 — 러너는 매번 새로 뜨므로 이 파일이 없으면 다음 실행이 빈 DB로 시작한다.

---

## 빠르게 보기 — fixtures 로 전체 돌리기

API 키가 **하나도 없어도** 전 구간이 돈다. 모든 역할을 오프라인 어댑터로 바꾸면 된다.

```bash
pip install -r requirements-dev.txt

# 1) 모든 역할을 fake provider 로 (키 불필요)
cp config/models.yaml config/models.real.yaml
cp config/models.offline.yaml config/models.yaml

# 2) fixtures 로 step0 → step8
python -m pipeline.step0_weights  --fixture tests/fixtures/gdelt_timelinesourcecountry.json --report
python -m pipeline.step1_collect  --gdelt-fixture tests/fixtures/gdelt_artlist.json --report
python -m pipeline.step2_filter   --report
python -m pipeline.step3_embed
python -m pipeline.step4_cluster  --report      # 사건별 기사 제목을 눈으로 본다
python -m pipeline.step5_submit_batch            # 제출하고 즉시 끝난다
python -m pipeline.step6_fetch_batch --report    # 수거
python -m pipeline.step7_synthesize              # batch 모드라 exit 75 (제출만)
python -m pipeline.step7_synthesize  --report    # 두 번째 실행이 수거한다
python -m pipeline.step8_build_site  --report

# 3) 원복
cp config/models.real.yaml config/models.yaml
```

프론트만 보고 싶으면 파이프라인을 건너뛰어도 된다.

```bash
python -m pipeline.step8_build_site --from-fixtures --report
python -m http.server -d site/dist 8000     # http://localhost:8000
```

테스트:

```bash
python -m pytest
```

실제 네트워크를 타지 않는다. API 키가 설정돼 있어도 테스트가 지우고 시작한다.

---

## 자주 하는 변경

### 모델 바꾸기 — `config/models.yaml` 한 파일

파이썬 파일을 열 일이 없다. 역할별로 provider·model·단가만 적혀 있다.

```yaml
roles:
  event_analysis:
    provider: gemini             # ← gemini | anthropic | openai | fake
    model: gemini-2.5-flash-lite # ← 모델 ID는 여기에만 존재한다
    mode: batch                  # ← batch(반값, 비동기) | sync
    price_per_mtok_in: 0.10      # ← 비용 계산과 상한 판정에 쓰인다
    price_per_mtok_out: 0.40
```

기본값은 세 역할 모두 Gemini 다. 가입할 곳이 한 곳이고 전부 무료 티어가 있다.

분석 품질을 올리고 싶으면 `event_analysis` 를 Claude Haiku 4.5 로 바꾼다
(`models.yaml` 주석에 그대로 적어 뒀다). `ANTHROPIC_API_KEY` 가 추가로 필요하고
월 $4 → $21.60 이 된다. 파이썬은 한 줄도 안 고친다.

단가를 같이 안 고치면 비용 집계와 일일 상한이 틀어진다. 같이 고쳐라.

**`daily_synthesis` 의 `mode`:** 기본값은 `sync` 다 — 하루 1회뿐이라
당일 브리핑을 위해 즉시 부른다. `batch` 로 바꾸면 반값이지만 비동기라서
브리핑이 다음 실행(하루 뒤)에 올라온다. `event_analysis` 는 하루 180회라
`batch` 를 유지하는 편이 낫다.

**임베딩 모델을 바꾸면** 기존 벡터와 거리 계산이 호환되지 않는다.
`embeddings` 테이블의 `model` 컬럼이 섞이는 것을 막아 주지만, 바꾼 날은
그날 기사 전체를 다시 임베딩한다.

### 수집량 바꾸기 — `config/sources.yaml` 의 `budget`

```yaml
budget:
  articles_per_day: 1000
  top_n_countries: 25
  floor_per_country: 10     # 없으면 작은 나라는 영원히 0건
  cap_per_country: 200      # 없으면 미국이 예산의 절반을 먹는다
```

수집량을 올리면 사건 수가 늘고 LLM 호출이 비례해 는다.
`config/pipeline.yaml` 의 `max_daily_cost_usd` 를 함께 확인해라 —
넘으면 step5 가 **제출 자체를 하지 않는다.**

한 국가 할당이 `gdelt.maxrecords`(250)를 넘으면 테마별로 쿼리를 쪼갠다.
쪼갤 테마가 모자라면 경고가 뜨고 250건만 들어온다.

### 국가 추가 — `config/countries.yaml`

```yaml
- { iso2: "VN", fips: "VM", name_ko: 베트남, name_en: "Vietnam", lat: 14.1, lng: 108.3 }
```

**GDELT는 FIPS 10-4 코드를 쓴다. ISO와 다르다.**

| 나라 | ISO | FIPS |
|---|---|---|
| 한국 | `KR` | `KS` |
| 중국 | `CN` | `CH` (ISO에서 `CH`는 스위스) |
| 일본 | `JP` | `JA` |
| 독일 | `DE` | `GM` |
| 영국 | `GB` | `UK` |
| 호주 | `AU` | `AS` (FIPS에서 `AU`는 오스트리아) |

두 코드를 섞으면 조용히 엉뚱한 나라 기사를 모은다. 에러가 나지 않으므로
몇 주 뒤에야 안다. 변환은 항상 `config` 로더를 경유한다.

**코드는 반드시 따옴표로 감싼다.** YAML 1.1은 따옴표 없는 `NO`(노르웨이)를
boolean `False`로 읽는다. 로더가 잡아내지만 애초에 만들지 마라.

`name_en` 은 GDELT `timelinesourcecountry` 응답의 시리즈 이름과 맞추기 위한 것이다.
표기가 다르면 `config/__init__.py` 의 `NAME_ALIASES` 에 추가해라 —
매칭 실패는 조용히 지나가지 않고 경고를 찍는다.

### 특정 나라를 강제로 올리기 — manual provider

```yaml
# config/sources.yaml
weights:
  providers:
    - { name: gdelt,  enabled: true, blend: 0.8 }
    - { name: manual, enabled: true, blend: 0.2 }
  manual_scores:
    KS: 500        # FIPS 코드다. ISO(KR)를 적으면 무시되고 경고가 뜬다.
```

manual 은 `attention` 에만 기여하고 `surge` 에는 기여하지 않는다.
손으로 "급등"을 만들 수 있으면 그건 신호가 아니다.

### 클러스터 품질 조정 — `config/pipeline.yaml` 의 `cluster`

```bash
python -m pipeline.step4_cluster --recluster --report
```

`--recluster` 는 그날 사건을 지우고 처음부터 다시 묶는다. 임계값을 바꿔가며
`--report` 로 사건별 기사 제목을 눈으로 확인해라. 클러스터 품질은 숫자로 안 보인다.

---

## 운영

### 크론 3개

| 워크플로 | 시각 (UTC / KST) | 하는 일 | 예상 소요 |
|---|---|---|---|
| `weights.yml` | 일 18:00 / 월 03:00 | step0 국가 가중치 | 2~3분 |
| `collect.yml` | `5 */6 * * *` | step1 수집 | 2~4분 |
| `analyze.yml` (submit) | 15:10 / 00:10 | step2~5, 배치 제출 | 6~10분 |
| `analyze.yml` (publish) | 19:10 / 04:10 | step6~9, Cloudflare Pages 배포 | 5~70분 |

세 워크플로는 `news-lens-db` 라는 **같은 concurrency 그룹**을 쓴다.
그룹 이름은 레포 전체에서 공유되므로, 셋이 동시에 돌아 DB 덤프를 서로
덮어쓰는 일이 없다.

월 합계 대략 **1,100분**. public 레포는 Actions 분이 무제한이고,
private 레포는 월 2,000분이 무료다. publish 의 편차가 큰 이유는 배치 대기
재시도(20분 × 최대 3회) 때문이다 — 배치가 바로 끝나면 5분에 끝난다.

`collect` 를 6시간마다 도는 이유: RSS는 최신 N건만 노출한다. 하루 한 번
폴링하면 발행량이 많은 매체의 기사를 통째로 놓친다.

`analyze` 가 두 실행으로 나뉜 이유: Batch API는 반값이지만 비동기다
(통상 1~4h, 보장 24h). 결과를 기다리며 러너를 켜두면 그 비용이 절감액을 넘는다.

### 상태는 레포에 산다

러너는 실행이 끝나면 사라진다. 그래서 각 job 은 이렇게 돈다:

```
db-restore  →  data/news-lens.sql  →  news-lens.db
   step 실행
db-commit   →  news-lens.db  →  data/news-lens.sql  →  git push
```

이걸 빠뜨리면 `collect` 가 모은 기사가 4시간 뒤 `analyze` 실행에는
존재하지 않는다. 워크플로 테스트가 모든 job 에 두 단계가 있는지 검사한다.

**바이너리 DB 가 아니라 SQL 텍스트를 커밋한다.** git 은 바이너리를 델타
압축하지 못해서, SQLite 파일을 그대로 커밋하면 한 행만 바뀌어도 히스토리에
파일 전체가 쌓인다. 하루 5번 커밋하면 1년에 기가바이트 단위가 된다.

**크기는 `step9_prune` 이 매일 묶는다.**

| 대상 | 보관 | 이유 |
|---|---|---|
| 임베딩 | 클러스터링 직후 삭제 | 가장 큰 덩어리(벡터당 768B), 이후 쓸 데 없음 |
| 기사 행 | 14일 | 사건과 출처 링크는 `events` 에 남는다 |
| `filter_log` | 14일 | |
| 사건 분석 전문 | 60일 | 전문은 이미 정적 페이지에 구워져 있다 |
| 사건 헤드라인·논조 | 영구 | 목록과 글로브가 쓴다 |
| 브리핑·가중치 | 영구 / 365일 | 작다 |

`step9_prune` 은 DB 밖도 정리한다: `data/raw/` 의 원문 gzip(90일),
오프라인 배치 파일. 이걸 안 지우면 로컬에서 반복 실행할 때 디스크가
조용히 차오른다.

**실측 증가율** (하루 1,000건·사건 180개를 30일 반복):

| | |
|---|---|
| 기사 행 | 14일치 15,000행에서 **멈춘다** |
| 덤프 크기 | 30일차 10.2 MB |
| 14일 이후 증가 | **하루 224 KB** (연 약 80 MB) |
| git 히스토리 | 텍스트라 델타가 잘 잡힌다 — 커밋당 수 KB 수준 |

60일이 지나면 오래된 사건의 분석 전문이 비워져 증가율이 더 떨어진다.

`python -m store.dump size` 가 현재 덤프 크기를 알려준다.
50MB를 넘으면 경고가 뜬다 — 그때가 외부 DB로 옮길 때다.

### exit code 75

`step6` 은 배치가 아직 처리 중이면 **75** 로 끝낸다. 실패(1)가 아니다.
워크플로가 이 둘을 구분하지 않으면 매일 아침 가짜 이슈가 생기거나,
반대로 진짜 실패를 대기로 착각한다.

3회 재시도 후에도 대기 중이면 빌드와 배포를 건너뛴다 —
어제 내용으로 사이트를 덮어쓰지 않기 위해서다. 그 상태도 이슈로 올라온다.

### 실패 알림

크론은 조용히 죽는다. 세 워크플로 모두 실패 시 `cron-failure` 라벨로
이슈를 만든다. 같은 날 같은 단계면 새 이슈 대신 댓글로 잇는다 —
6시간마다 도는 `collect` 가 매번 새 이슈를 만들면 알림이 무의미해진다.

### 시크릿

**필수는 세 개다.**

```
GEMINI_API_KEY          https://aistudio.google.com/apikey
CLOUDFLARE_API_TOKEN    Cloudflare 대시보드 (권한: Cloudflare Pages → Edit)
CLOUDFLARE_ACCOUNT_ID   대시보드 우측 사이드바 또는 URL 의 해시값
```

나머지는 전부 선택이고, `config/models.yaml` 에서 해당 provider 를 켠
경우에만 필요하다. `.env.example` 에 무엇이 언제 필요한지 적어 뒀다.

```
ANTHROPIC_API_KEY   event_analysis 를 Claude 로 바꿨을 때
OPENAI_API_KEY      embedding 을 OpenAI 로 되돌렸을 때
R2_*                원문 아카이브를 Cloudflare R2 에 둘 때
```

### 비용이 이상할 때

```sql
SELECT date, role, model, SUM(cost_usd), COUNT(*) FROM runs GROUP BY date, role;
```

모든 호출 전에 예상 비용이 stderr 의 `[cost]` 줄로 찍힌다.
`config/pipeline.yaml` 의 `max_daily_cost_usd` 를 넘으면 **코드가 중단시킨다** —
경고만 찍고 진행하지 않는다. 피드 하나 잘못 추가해 수집량이 10배가 되는 사고는
반드시 한 번 난다.

### 분석 품질이 이상할 때

```sql
-- 스키마 검증에 실패한 응답 (버리지 않고 원문을 남긴다)
SELECT id, raw_response FROM events WHERE schema_ok = 0;

-- 어떤 이유로 기사가 걸러졌나
SELECT reason, COUNT(*) FROM filter_log WHERE date = '2026-09-12' GROUP BY reason;

-- 가중치 추이 (덮어쓰지 않고 날짜별로 쌓는다)
SELECT date, fips, weight, surge_raw FROM country_weights ORDER BY date DESC;
```

검증 실패율이 20%를 넘으면 `step6 --report` 가 경고한다.
`prompts/01_event_analysis.md` 를 고칠 근거는 `raw_response` 에만 있다.

---

## 예상 월 비용

| 항목 | 서비스 | 계정 | 월 비용 |
|---|---|---|---|
| 뉴스 수집 | GDELT 2.0 + RSS | 불필요 | $0 |
| 국가 가중치 | GDELT `timelinesourcecountry` | 불필요 | $0 |
| 스케줄러 | GitHub Actions (~1,100분) | GitHub | $0 |
| 저장소 | SQLite → 레포 커밋 | 불필요 | $0 |
| 정적 배포 | Cloudflare Pages | Cloudflare | $0 (요청 무제한) |
| 임베딩 | `gemini-embedding-2` | Google | **무료 티어** |
| 사건 분석 | `gemini-2.5-flash-lite` batch | Google | **무료 티어** |
| 일일 종합 | `gemini-2.5-flash-lite` | Google | **무료 티어** |
| **합계 (기본)** | | **3곳** | **$0 ~ $4** |

무료 티어 한도를 넘기면 유료로 전환되고, 그때 위 세 항목이 합쳐서 월 $4 수준이다.
`config/pipeline.yaml` 의 `max_daily_cost_usd: 1.20` 이 상한을 강제한다 —
넘으면 코드가 중단시킨다.

품질안(사건 분석을 Claude Haiku 4.5 로)으로 올리면 Anthropic 계정이 추가되고
월 $21.60 이 된다.

---

## 손으로 배포하기

자동 배포(매일 04:10)와 별개로, 언제든 직접 올릴 수 있다.

```bash
npm install -g wrangler
wrangler login
wrangler pages project create news-lens --production-branch=main   # 최초 1회
```

프로젝트를 미리 만들어 두지 않으면 CI 가 대화형 프롬프트를 띄울 수 없어 실패한다.

배포는 `scripts/deploy.py` 를 거친다. 이 레포는 배포 대상이 둘이고 명령이
서로 다르기 때문이다 — 섞으면 조용히 엉뚱한 곳에 올라간다.

| 대상 | wrangler 명령 |
|---|---|
| `site/dist/` 정적 사이트 | `pages deploy` |
| `site/worker/` 검색 API (선택) | `deploy` |

```bash
python -m scripts.deploy                    # 프리뷰 (기본)
python -m scripts.deploy --build            # 빌드부터 다시
python -m scripts.deploy --branch demo      # 임의 이름의 프리뷰
python -m scripts.deploy --production       # 프로덕션
python -m scripts.deploy --worker           # Worker (D1 필요)
python -m scripts.deploy --dry-run          # 명령만 확인
```

**기본이 프리뷰인 이유**는 여러 번 시험 배포하는 게 정상 작업이기 때문이다.
프리뷰는 `https://<브랜치>.news-lens.pages.dev` 로 따로 올라가고 실서비스를
건드리지 않는다. 브랜치 이름만 바꿔 가며 몇 번이든 올려도 된다.

**프로덕션 배포는 샘플 데이터가 섞여 있으면 거부한다.**
`--from-fixtures` 빌드는 구조가 진짜와 똑같아서 눈으로 구분되지 않는다.
탐지 문자열은 `config/pipeline.yaml` 의 `site.deploy.sample_markers` 에 있다.
알면서 올릴 때만 `--allow-sample` 을 붙인다.

---

## 내 컴퓨터에는 무엇이 쌓이나

**GitHub Actions 로 돌리는 경우 — 아무것도 쌓이지 않는다.** 러너는 GitHub 쪽에서
뜨고 실행이 끝나면 사라진다. 배포 산출물은 Cloudflare 로 바로 올라간다.
내려받는 것은 `git pull` 할 때의 레포뿐이다.

**로컬에서 돌리는 경우** 아래가 생긴다. 전부 `data/` 와 `site/dist/` 안이고,
`data/news-lens.sql` 을 뺀 나머지는 gitignore 돼 있다.

| 경로 | 무엇 | 정리 |
|---|---|---|
| `news-lens.db` | SQLite 작업 파일 | `step9_prune` |
| `data/news-lens.sql` | 상태 덤프 (레포에 커밋되는 유일한 것) | `step9_prune` |
| `data/raw/` | 원문 gzip | `step9_prune` (90일) |
| `data/fake-batches/` | 오프라인 배치 파일 | `step9_prune` |
| `site/dist/` | 빌드 산출물 | 빌드마다 통째로 교체 |

전부 지워도 안전하다. 다음 실행이 다시 만든다 — 단, `data/news-lens.sql` 을
지우면 수집한 기사와 분석한 사건이 함께 사라진다.

```bash
python -m pipeline.step9_prune --report      # 무엇이 얼마나 있는지 + 정리
python -m store.dump size                    # 덤프 크기만 확인
```

---

## 지켜야 할 선

- **기사 원문을 사이트에 싣지 않는다.** 요약 + 출처 링크만. 원문 재게시는
  저작권 문제로 직결된다. GDELT도 본문을 주지 않는다.
- **이용약관이 자동 접근을 금지하는 사이트를 스크래핑하지 않는다.**
  Investing.com은 공개 API가 없고 약관상 프로그래밍 방식 접근이 금지돼 있다.
  이 레포에 스크래퍼를 넣지 마라 — 공개 사이트의 데이터 소스로 삼으면
  서비스 전체가 막힌다.
- **기사 원문을 git에 커밋하지 않는다.** `data/raw/` 는 gitignore 돼 있다.
  레포에 싣는 것은 파생 데이터 덤프(`data/news-lens.sql`)뿐이다.
- **WebGL 폴백을 지운다면 그 전에 껐다 켜 봐라.** 글로브가 안 뜨면
  사이트 전체가 빈 화면이 된다.

---

## 프론트엔드 메모

`site/static/globe.js` 를 고칠 때 밟기 쉬운 지뢰들이다. 전부 실제로 밟았다.

- **`[hidden]` 은 `display: grid/flex` 에 진다.** `el.hidden = true` 로 숨기는
  요소가 여럿이라 `globe.css` 최상단에 `[hidden] { display: none !important; }`
  를 못박아 뒀다. 지우면 패널과 스켈레톤이 항상 떠 있게 된다.
- **글로브 스테이지에 그리드 높이를 물려주지 마라.** 목록이 길면 캔버스가
  수천 픽셀로 늘어나 지구본이 화면 밖으로 밀려난다. 뷰포트 높이 + `sticky` 다.
- **three.js 를 따로 로드하지 마라.** r160부터 UMD 빌드가 없어 404가 나고
  전역 `THREE` 도 안 생긴다. globe.gl 번들에 이미 들어 있다.
- **`labelsData` 로 한글을 쓸 수 없다.** three-globe의 라벨은 `TextGeometry` +
  helvetiker 폰트라 한글 글리프가 없어 **아무것도 안 그려진다**. 에러도 안 난다.
  그래서 `htmlElementsData`(DOM 라벨)를 쓴다.
- **마커 크기는 `sqrt(article_count)`.** 선형이면 미국만 거대해져 나머지가 안 보인다.

`earth-night.jpg` 는 자리표시자다. 진짜 야간 지구 이미지는
[NASA Black Marble](https://earthobservatory.nasa.gov/features/NightLights) 에서
받아 같은 경로에 덮어쓰면 된다 (equirectangular, 2048×1024 이상).

---

## 디렉토리

```
config/     ★ 모델·수집예산·국가·임계값. 코드에 숫자와 모델 ID를 두지 않는다
prompts/    ★ LLM 지시문과 JSON 스키마. 문구는 코드에 두지 않는다
llm/        ★ 벤더 SDK가 존재하는 유일한 곳. pipeline/ 은 registry만 안다
weights/    ★ 국가 관심도 provider 인터페이스. pipeline/ 은 소스를 모른다
pipeline/   파일명 번호 = 실행 순서. 각 step은 단독 실행 가능
store/      schema.sql · db.py(SQLite) · dump.py(레포 커밋용 SQL 덤프) · blobs.py
site/       templates(Jinja2) · static(globe.gl) · dist(gitignore, 배포 대상)
            worker/ 는 선택 — Cloudflare Workers 검색 API. 기본 구성에선 안 쓴다
            dist 는 `pages deploy`, worker 는 `wrangler deploy`. 섞지 마라
data/       news-lens.sql 만 커밋된다. 이게 파이프라인의 상태다
scripts/    운영 도구. deploy.py 가 Pages/Worker 배포를 한 군데로 모은다
tests/      fixtures 200건 + fake provider. 실제 API를 호출하지 않는다
```
