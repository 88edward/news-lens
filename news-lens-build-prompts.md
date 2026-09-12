# news-lens — 통합 구축 프롬프트

> 전 세계 뉴스를 국가별 가중치로 수집해 LLM으로 분석하고,
> 3D 글로브 화면으로 배포하는 파이프라인의 전체 구축 지시서.
>
> 기준일: 2026-09 · 규모: 1,000건/일 · 예산: 월 $10~50

---

## 0. 이 문서 사용법

- **섹션 1~5는 배경 정보**입니다. Claude Code에게 그대로 붙여넣지 말고, 프롬프트가 참조할 수 있게 레포에 `docs/DESIGN.md`로 두세요.
- **섹션 6의 프롬프트 7개를 순서대로 하나씩** 실행하세요. 한 번에 다 주면 반드시 망가집니다.
- 각 프롬프트가 끝나면 테스트를 돌리고 커밋한 뒤 다음으로 넘어가세요.
- 각 프롬프트 끝의 **완료 조건**을 통과하지 못하면 다음으로 넘어가지 마세요.

---

## 1. 프로젝트 개요

| 항목 | 값 |
|---|---|
| 수집 | 1,000건/일 (국가별 가중 배분) |
| 필터 후 | ~700건/일 |
| 사건 클러스터 | ~180개/일 |
| LLM 호출 | 사건 단위 180회/일 + 종합 1회/일 |
| 언어 | Python 3.12 (파이프라인) / TypeScript (Worker) / JS (글로브 프론트) |
| 실행 | GitHub Actions 크론 3개 |
| 저장 | Cloudflare R2(원문) + Turso 또는 D1(파생) |
| 배포 | Cloudflare Pages(정적) + Workers(검색/아카이브) |
| 월 비용 | LLM $6~24 + 인프라 $0~5 |

**절대 규칙: 기사 1건마다 LLM을 호출하지 않습니다.** 임베딩으로 같은 사건을 묶은 뒤 사건 단위로만 호출합니다. 이 한 가지가 비용을 4~5배 가릅니다.

---

## 2. 아키텍처 요약

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
        R2(원문) / Turso(파생)
               ▼
        Pages(정적) + Workers(검색)
```

**크론이 나뉘는 이유:** Batch API는 반값이지만 비동기(통상 1~4h, 최대 24h 보장)입니다. 결과를 기다리며 러너를 켜둘 수 없으므로 제출과 수거를 다른 실행으로 쪼갭니다.

---

## 3. 디렉토리 구조

핵심 원칙: **모델을 바꿀 때 파이썬 파일을 열 일이 없어야 한다.**

```
news-lens/
├─ config/
│  ├─ models.yaml            ★ 모델 ID·단가·batch 여부
│  ├─ sources.yaml           ★ 수집 예산·GDELT 쿼리·RSS 목록
│  ├─ countries.yaml         ★ 국가 목록·좌표·표시 이름
│  └─ pipeline.yaml          클러스터 임계값·비용 상한·보관 기간
├─ prompts/
│  ├─ 01_event_analysis.md       ★ 사건 분석 지시문
│  ├─ 02_daily_synthesis.md      ★ 일일 브리핑 지시문
│  └─ _shared/
│     ├─ event_schema.json
│     └─ briefing_schema.json
├─ llm/                      ★ 벤더 SDK가 존재하는 유일한 곳
│  ├─ base.py                embed / submit_batch / fetch_batch / complete
│  ├─ anthropic_client.py
│  ├─ gemini_client.py
│  ├─ openai_embeddings.py
│  ├─ registry.py            역할 이름 → 클라이언트
│  └─ cost.py                usage × 단가 → runs 테이블 누적
├─ weights/                  ★ 국가 관심도 신호 제공자
│  ├─ base.py                AttentionProvider 인터페이스
│  ├─ gdelt_provider.py      기본값 — GDELT 경제 테마 국가별 볼륨
│  ├─ manual_provider.py     수동 가중치 (yaml에서 직접 지정)
│  └─ blend.py               여러 provider 결합 + 정규화
├─ pipeline/                 파일명 번호 = 실행 순서
│  ├─ step0_weights.py       국가 관심도 → country_weights 테이블
│  ├─ step1_collect.py       가중치 비례 쿼리 예산 배분 → 수집
│  ├─ step2_filter.py
│  ├─ step3_embed.py
│  ├─ step4_cluster.py       사건 생성 + primary_country 지정
│  ├─ step5_submit_batch.py
│  ├─ step6_fetch_batch.py
│  ├─ step7_synthesize.py
│  └─ step8_build_site.py    globe.json + 정적 페이지
├─ store/
│  ├─ schema.sql
│  ├─ db.py                  Turso(libSQL) / D1
│  └─ blobs.py               R2 원문 업로드·다운로드
├─ site/
│  ├─ templates/             Jinja2
│  │  ├─ index.html          글로브 화면 (첫 진입)
│  │  ├─ country.html        국가별 사건 목록
│  │  ├─ event.html          사건 상세
│  │  └─ briefing.html       일일 브리핑
│  ├─ static/
│  │  ├─ globe.js            globe.gl 초기화 + 인터랙션
│  │  ├─ globe.css
│  │  └─ earth-night.jpg     야간 지구 텍스처
│  ├─ worker/                Cloudflare Worker (TypeScript)
│  └─ dist/                  빌드 산출물 (gitignore)
├─ .github/workflows/
│  ├─ weights.yml            주 1회 — step0
│  ├─ collect.yml            6시간마다 — step1
│  └─ analyze.yml            00:10 — step2~5 / 04:10 — step6~8
├─ tests/
│  ├─ fixtures/              실제 GDELT·RSS 응답 샘플
│  └─ fake_provider.py
├─ docs/DESIGN.md            이 문서
├─ CLAUDE.md
├─ .env.example
└─ README.md
```

---

## 4. 국가별 가중치 수집 설계

### 4.1 Investing.com은 쓸 수 없습니다 — 대신 이것을 쓰세요

Investing.com은 **공개 API를 제공하지 않는다고 공식적으로 명시**하고 있고, 데이터 제공사와의 계약상 프로그래밍 방식 접근을 허용하지 않습니다. 스크래핑은 이용약관 위반이며, 공개 사이트의 데이터 소스로 삼으면 나중에 서비스 전체가 막힙니다. **이 프로젝트에 스크래퍼를 넣지 마세요.**

원하는 신호는 "지금 금융·경제 뉴스에서 자주 언급되는 나라 순서"인데, **GDELT가 바로 그걸 측정하는 도구입니다.** DOC 2.0 API의 `timelinesourcecountry` 모드는 국가별 보도량을 그대로 돌려주고, GKG 테마 필터(`ECON_*`)로 경제 뉴스에 한정할 수 있습니다. 무료이고, 허용되어 있고, 목적에 정확히 맞습니다.

그래서 가중치 계층을 **교체 가능한 provider 인터페이스**로 만듭니다. 기본값은 GDELT, 나중에 정식 라이선스를 얻은 금융 데이터 소스가 생기면 provider 파일 하나만 추가하면 됩니다.

### 4.2 관심도 스코어 계산

```
attention  = 최근 7일 경제 테마 기사량의 국가별 점유율   (정규화 0~1)
surge      = 어제 기사량 ÷ 최근 7일 일평균              (정규화 0~1)
weight     = 0.7 × attention + 0.3 × surge              (계수는 config)
```

- `attention`만 쓰면 미국·중국이 영구히 상위를 독식합니다.
- `surge`를 섞어야 "평소엔 조용한데 오늘 갑자기 터진 나라"가 올라옵니다. 뉴스 사이트에서 실제로 보고 싶은 건 이쪽입니다.

### 4.3 수집 예산 배분

하루 총 1,000건을 상위 25개국에 가중치 비례로 나누되, 두 개의 안전장치를 둡니다.

| 규칙 | 값 | 이유 |
|---|---|---|
| 국가당 최소 보장 (floor) | 10건 | 없으면 작은 나라는 영원히 0건 |
| 국가당 최대 상한 (cap) | 200건 | 없으면 미국이 예산의 절반을 먹음 |
| GDELT 쿼리당 상한 | 250건 | API 하드 리밋. 초과분은 테마로 쿼리를 쪼갬 |

**GDELT DOC 2.0 API는 한 쿼리당 최대 250건**(파라미터 미지정 시 기본 75건)입니다. 그래서 실제 수집량은 `쿼리 개수 × 250`으로 결정됩니다. 한 국가 할당이 250건을 넘으면 그 국가를 테마별(정치/경제/분쟁) 쿼리로 분할해야 합니다.

### 4.4 설정 파일 예시

```yaml
# config/sources.yaml
budget:
  articles_per_day: 1000
  top_n_countries: 25
  floor_per_country: 10
  cap_per_country: 200

weights:
  providers:
    - { name: gdelt, enabled: true,  blend: 1.0 }
    - { name: manual, enabled: false, blend: 0.0 }   # 수동 개입용
  formula:
    attention_coef: 0.7
    surge_coef: 0.3
    lookback_days: 7
  refresh: weekly          # step0 실행 주기

gdelt:
  maxrecords: 250          # API 상한. 기본 75이므로 반드시 명시
  timespan: 6h             # step1이 6시간마다 돌므로
  themes_for_weights:      # 가중치 계산용 경제 테마
    - ECON_STOCKMARKET
    - ECON_INTEREST_RATE
    - ECON_INFLATION
    - ECON_TRADE_DISPUTE
    - WB_1104_MACROECONOMIC_VULNERABILITY

rss:
  poll_per_day: 4
  feeds: []                # 국가 코드 태그를 달아 country별로 귀속시킨다
```

```yaml
# config/countries.yaml — 글로브 좌표와 표시 이름을 한곳에
- { iso2: US, fips: US, name_ko: 미국,   lat: 39.8,  lng: -98.6 }
- { iso2: CN, fips: CH, name_ko: 중국,   lat: 35.9,  lng: 104.2 }
- { iso2: JP, fips: JA, name_ko: 일본,   lat: 36.2,  lng: 138.3 }
- { iso2: KR, fips: KS, name_ko: 한국,   lat: 36.5,  lng: 127.8 }
# ... 주의: GDELT는 FIPS 2자리 코드를 쓰고 ISO와 다르다 (한국 KS, 중국 CH)
```

> **FIPS vs ISO 함정** — GDELT의 `sourcecountry` 필터는 ISO가 아니라 **FIPS 10-4 코드**를 씁니다. 한국은 `KS`(ISO는 `KR`), 중국은 `CH`(ISO는 `CN`, 그런데 ISO에서 `CH`는 스위스입니다). 이 매핑을 `countries.yaml`에 명시적으로 두지 않으면 조용히 엉뚱한 나라 기사를 모읍니다.

---

## 5. 글로브 화면 설계

### 5.1 목표 화면

첫 진입 시 **어두운 3D 지구본**이 보이고, 관심도가 높은 나라 위에 발광하는 마커가 떠 있습니다. 급등한 나라에는 퍼져나가는 링 애니메이션이 붙습니다. 마커를 클릭하면 우측 패널에 그 나라의 오늘 사건 목록이 나타납니다.

| 요소 | 매핑 |
|---|---|
| 마커 크기 | `sqrt(article_count)` — 선형이면 미국만 거대해짐 |
| 마커 색 | 평균 논조(tone): 부정 → 주황/적색, 중립 → 청색 |
| 확산 링 | `surge` 상위 3개국에만. 그 이상은 화면이 시끄러워짐 |
| 야간 텍스처 | 지구 밤 이미지 + 반투명 대기 레이어 |
| 국가 라벨 | 상위 12개국만 표시. 전부 켜면 겹쳐서 못 읽음 |

### 5.2 기술 선택

- **globe.gl** (three.js 기반 WebGL 컴포넌트). `pointsData()` / `ringsData()` / `onPointClick()` 을 그대로 씁니다.
- 정적 사이트이므로 데이터는 **빌드 시점에 생성된 JSON**을 읽습니다. 런타임 API 호출 없음.
- 국가 25개 수준이면 성능은 전혀 문제되지 않습니다.

### 5.3 데이터 계약

`step8`이 생성하는 파일 두 종류입니다.

```jsonc
// site/dist/data/globe.json — 글로브가 읽는 단일 파일
{
  "generated_at": "2026-09-12T04:25:00Z",
  "countries": [
    {
      "iso2": "CN",
      "name_ko": "중국",
      "lat": 35.9, "lng": 104.2,
      "weight": 0.87,          // 0~1, 마커 밝기
      "article_count": 143,    // 마커 크기 → sqrt
      "event_count": 21,
      "avg_tone": -3.2,        // 마커 색
      "surge": 2.4,            // >1.8이면 링 표시
      "top_event": {
        "id": "evt_20260912_0031",
        "headline": "한 줄 요약",
        "source_count": 7
      }
    }
  ]
}
```

```jsonc
// site/dist/data/country/CN.json — 마커 클릭 시 패널이 읽는 파일
{
  "iso2": "CN",
  "date": "2026-09-12",
  "events": [
    {
      "id": "evt_20260912_0031",
      "headline": "...",
      "summary": "...",
      "category": "economy",
      "tone": -3.2,
      "source_count": 7,
      "sources": [{ "domain": "reuters.com", "title": "...", "url": "..." }]
    }
  ]
}
```

### 5.4 반드시 지킬 것

- **원문을 싣지 마세요.** 요약 + 출처 링크만. 원문 재게시는 저작권 문제로 직결됩니다.
- **WebGL 폴백을 만드세요.** WebGL이 없거나 실패하면 국가 목록 테이블로 대체합니다. 글로브가 안 뜨면 사이트 전체가 빈 화면이 됩니다.
- **`prefers-reduced-motion`을 존중하세요.** 링 애니메이션과 자동 회전을 끕니다.
- **모바일에서 글로브는 작게, 목록을 크게.** 폰에서 지구본을 돌려 나라를 찾는 사람은 없습니다. 400px 폭에서는 글로브를 상단 고정 높이로 두고 목록을 주인공으로 만드세요.

---

## 6. 프롬프트

> 아래 7개를 **순서대로 하나씩** 주세요. 각 단계 후 테스트 + 커밋.

---

### 프롬프트 1 — 프로젝트 규칙부터 (코드 없음)

```text
news-lens 프로젝트를 시작한다. 먼저 CLAUDE.md만 작성해라. 코드는 아직 쓰지 마라.

프로젝트: 전 세계 뉴스를 국가별 가중치로 수집해 LLM으로 분석하고,
3D 글로브 화면이 있는 정적 사이트로 배포하는 파이프라인.

규모: 수집 1,000건/일 → 룰 필터 후 700건 → 임베딩·클러스터링으로 약 180개 사건
      → 사건 단위로만 LLM 호출. 기사 단위로 호출하면 비용이 5배가 된다.
예산: LLM 월 $25 이내. 인프라는 무료 티어만 사용.
스택: Python 3.12 파이프라인 / GitHub Actions 크론 3개 /
      Cloudflare R2 + Turso + Pages + Workers / 프론트는 globe.gl.

CLAUDE.md에 다음을 프로젝트 규칙으로 명시해라:

- 모델 ID와 프롬프트 문구를 코드에 하드코딩 금지.
  모델은 config/models.yaml, 지시문은 prompts/*.md 에만 둔다.
- LLM 호출은 llm/ 의 어댑터를 통해서만 한다.
  pipeline/ 코드가 anthropic·openai·google SDK를 직접 import 하면 안 된다.
- 국가 관심도 신호는 weights/ 의 provider 인터페이스를 통해서만 얻는다.
  pipeline/ 코드가 특정 데이터 소스를 직접 알면 안 된다.
- 이용약관이 자동 접근을 금지하는 사이트를 스크래핑하지 않는다.
  Investing.com은 공개 API가 없고 약관상 금지이므로 절대 대상으로 삼지 마라.
- 모든 LLM 호출은 Batch API를 기본으로 한다.
  동기 호출은 models.yaml에 mode: sync 로 명시한 역할만.
- pipeline/step*.py 는 각각 단독 실행 가능해야 한다.
  이전 단계 결과는 인자로 받지 않고 DB나 R2에서 읽는다.
- 모든 step은 --dry-run 과 --limit N 을 지원한다. API 키 없이도 dry-run이 돌아야 한다.
- 원문은 R2에, 파생 데이터는 SQL DB에. 원문을 절대 git에 커밋하지 않는다.
- 사이트에는 기사 원문을 싣지 않는다. 요약 + 출처 링크만.
- 비용이 드는 호출 전에 예상 토큰 수와 예상 비용을 stderr에 찍고,
  config/pipeline.yaml 의 max_daily_cost_usd 를 넘으면 중단한다.
- GDELT는 FIPS 10-4 국가코드를 쓴다. ISO와 다르므로(한국 KS, 중국 CH)
  config/countries.yaml 의 매핑을 항상 경유한다.
- 커밋은 단계별로 작게. 한 커밋에 여러 step을 섞지 않는다.
```

---

### 프롬프트 2 — 설정 계층과 LLM 어댑터

```text
CLAUDE.md 규칙에 따라 뼈대를 만들어라.
파이프라인 로직은 아직 구현하지 말고, 설정 계층과 LLM 어댑터 계층만 완성해라.

1. 디렉토리와 빈 파일을 생성해라. 구조는 docs/DESIGN.md 의 3장과 같다.
   (없으면 내가 붙여넣을 테니 요청해라)

2. config/models.yaml — embedding / event_analysis / daily_synthesis 세 역할.
   각 역할은 provider, model, mode(batch|sync), max_output_tokens,
   prompt 경로, schema 경로, price_per_mtok_in/out 을 갖는다.
   기본값:
     embedding:        openai / text-embedding-3-small / 512차원
     event_analysis:   anthropic / claude-haiku-4-5 / batch
     daily_synthesis:  anthropic / claude-sonnet-5 / batch

3. llm/base.py — 추상 인터페이스:
     embed(texts) -> list[vector]
     submit_batch(requests) -> batch_id
     fetch_batch(batch_id) -> Results | PENDING
     complete(prompt) -> text
   모든 반환값에 usage(입력·출력 토큰)를 포함시켜라.

4. llm/anthropic_client.py, llm/gemini_client.py, llm/openai_embeddings.py
   — base 구현. 벤더별 batch API 차이는 어댑터 안에서 흡수한다.

5. llm/registry.py — get_client("event_analysis") 처럼 역할 이름으로 부르면
   models.yaml을 읽어 맞는 클라이언트를 돌려준다.
   역할을 추가할 때 registry.py를 수정할 필요가 없게 설계해라.

6. llm/cost.py — usage와 models.yaml 단가로 호출별 비용을 계산하고
   runs 테이블에 누적. 일일 누적이 상한을 넘으면 예외를 던진다.

7. tests/ — fake provider를 만들고
   (a) provider를 바꿔도 registry가 같은 인터페이스를 돌려주는지
   (b) cost 계산이 정확한지
   를 테스트해라. 실제 API는 절대 호출하지 마라.

완료 조건: pytest 통과. 그리고 models.yaml의 event_analysis provider를
anthropic → gemini 로 한 줄 바꿨을 때, 파이썬 코드를 한 줄도 안 고치고
테스트가 그대로 통과해야 한다. 이게 안 되면 설계를 다시 해라.
```

---

### 프롬프트 3 — 국가 가중치 계층 (step0)

```text
국가별 관심도 가중치 계층을 구현해라. 이게 수집량 배분을 결정한다.

config/countries.yaml
  국가 목록. 각 항목은 iso2, fips, name_ko, lat, lng.
  주요 40개국을 채워 넣어라.
  중요: GDELT는 FIPS 10-4 코드를 쓴다. ISO와 다르다
  (한국 ISO=KR / FIPS=KS, 중국 ISO=CN / FIPS=CH).
  두 코드를 모두 갖고 상호 변환하는 헬퍼를 store/ 아니라 config 로더에 둬라.

weights/base.py
  AttentionProvider 인터페이스:
    fetch(lookback_days) -> dict[fips_code, raw_volume]
  provider는 원시 볼륨만 돌려주고, 정규화는 blend.py가 담당한다.

weights/gdelt_provider.py   ← 기본 provider
  GDELT DOC 2.0 API의 mode=timelinesourcecountry 를 사용해
  config/sources.yaml 의 themes_for_weights 테마에 해당하는
  기사의 국가별 보도량을 최근 7일치 가져온다.
  요청 실패·부분 실패 시 이전 주 값을 재사용하고 경고를 남겨라.

weights/manual_provider.py
  config/sources.yaml 에 직접 적은 국가별 점수를 그대로 돌려준다.
  특정 나라를 강제로 올리거나 내릴 때 쓴다.

weights/blend.py
  attention = 국가별 점유율 정규화 (0~1)
  surge     = 어제 볼륨 ÷ 최근 7일 일평균, 정규화 (0~1)
  weight    = attention_coef × attention + surge_coef × surge
  계수는 config/sources.yaml 의 weights.formula 에서 읽어라.
  enabled=true 인 provider들을 blend 값으로 가중 평균한다.

pipeline/step0_weights.py
  blend 결과를 country_weights 테이블에 저장한다.
  컬럼: date, fips, iso2, attention, surge, weight, provider_breakdown(JSON).
  이전 실행 값을 덮어쓰지 말고 날짜별로 쌓아라 — 가중치 변화 추이를 봐야 한다.
  --report 옵션으로 상위 25개국을 weight 순으로 출력해라.

완료 조건:
- tests/fixtures/ 에 GDELT timelinesourcecountry 응답 샘플을 넣고,
  step0을 --dry-run 으로 돌려 country_weights 가 채워지는 것을 검증.
- surge 계산 테스트: 평소 10건인 나라가 어제 40건이면 surge가 크게 나오고,
  평소 500건인 나라가 어제 520건이면 surge가 작게 나오는지 확인.
- manual_provider 를 켜서 특정 국가 가중치를 올렸을 때
  최종 weight 순위가 실제로 바뀌는지 확인.
```

---

### 프롬프트 4 — 가중치 기반 수집과 무료 구간 (step1~4)

```text
LLM이 필요 없는 구간을 구현해라: step1~step4.
여기가 LLM 호출 수를 결정하므로 제일 정확하게 만들어야 한다.

step1_collect.py
  (a) 예산 배분
      country_weights 의 상위 N개국(sources.yaml 의 top_n_countries)에
      budget.articles_per_day 를 weight 비례로 배분한다.
      floor_per_country 를 하한, cap_per_country 를 상한으로 클램프하고,
      클램프 후 남거나 모자란 분량은 나머지 국가에 재배분해라.
      배분 결과를 collect_plan 테이블에 저장한다 (날짜, fips, 할당량).

  (b) 수집
      국가별 할당량만큼 GDELT DOC 2.0 API에서 기사를 받는다.
      maxrecords 는 sources.yaml 값(250)을 반드시 명시해라. 기본값 75로 돌면 안 된다.
      한 국가 할당이 250을 넘으면 테마별로 쿼리를 쪼개라.
      sourcecountry 필터에는 FIPS 코드를 써라.
      RSS 피드도 함께 폴링하고, 각 피드에 태그된 국가 코드로 귀속시킨다.

  (c) 저장
      원문 HTML은 store/blobs.py 로 R2에 하루 1개 gzip JSONL 파일로.
      DB에는 url, 매체, 발행시각, 제목, 리드, country_fips, blob 위치만.
      이미 수집한 url은 다시 받지 않는다.

  --report 옵션으로 국가별 계획 대비 실제 수집량을 표로 출력해라.
  계획 1,000건인데 실제 400건이 들어오면 즉시 알아야 한다.

step2_filter.py
  LLM 없이 룰로만 걸러낸다:
  URL 정규화 후 중복 제거 / 언어 필터 / 매체 화이트리스트 / 본문 최소 길이.
  중요: 걸러낸 기사마다 이유를 filter_log 테이블에 남겨라.
  이게 없으면 임계값을 손댈 근거가 영원히 없다.

step3_embed.py
  제목 + 리드만 임베딩한다. 본문 전체를 넣지 마라.
  registry.get_client("embedding") 사용. 512차원으로 잘라 int8 양자화.
  embeddings 테이블에 model 컬럼을 둬라 — 모델을 바꾸면 기존 벡터와
  거리 계산이 호환되지 않으므로 어떤 모델로 만든 벡터인지 남아야 한다.
  이미 임베딩된 기사는 건너뛴다.

step4_cluster.py
  코사인 유사도 + HDBSCAN으로 같은 사건을 묶는다.
  events 테이블에 사건을 만들고 articles.event_id 를 채운다.
  사건마다 primary_country 를 정해라 — 소속 기사의 country_fips 최빈값,
  동률이면 매체 수가 많은 쪽.
  사건별 대표 기사 3건을 고른다. 매체 다양성을 우선하고,
  같은 매체 기사 2건이 대표로 뽑히지 않게 해라.
  모든 임계값은 config/pipeline.yaml 에서 읽어라. 코드에 숫자를 박지 마라.

store/schema.sql 도 함께 작성:
  articles / events / embeddings / briefings / runs / filter_log
  / country_weights / collect_plan

완료 조건:
- tests/fixtures/ 에 실제 GDELT·RSS 응답 샘플 200건을 넣고
  step1~4를 --dry-run 으로 돌려 사건 클러스터가 생기는 것을 테스트로 검증.
- 예산 배분 테스트: 가중치가 극단적으로 치우쳐도
  (한 나라가 weight 0.9) cap 때문에 200건을 넘지 않고,
  하위 국가도 floor 10건을 받는지 확인.
- `python -m pipeline.step4_cluster --report` 로 사건별로 묶인 기사 제목
  목록을 출력하는 옵션을 만들어라. 클러스터 품질은 눈으로 봐야 한다.
```

---

### 프롬프트 5 — 배치 LLM 구간 (step5~7)

```text
Batch API 구간을 구현해라. 제출과 수거가 서로 다른 크론 실행이라는 걸 전제로 짜라.

prompts/01_event_analysis.md
  사건 1개 + 대표 기사 3건을 받아 분석한다.
  출력은 prompts/_shared/event_schema.json 스키마의 JSON.
  필드: 한줄요약, 핵심사실[], 배경, 이해관계자[], 영향분석,
        출처간_불일치[] (매체별로 사실이 엇갈리는 지점),
        카테고리, 신뢰도, 논조점수(-10~+10).
  논조점수는 글로브 마커 색에 쓰이므로 반드시 숫자로 받아라.

prompts/02_daily_synthesis.md
  그날 사건 요약 전체를 받아 일일 브리핑을 만든다.
  필드: 헤드라인, 오늘의_3대_흐름[], 지역별_요약, 주목할_신호[].

step5_submit_batch.py
  아직 분석되지 않은 사건들로 batch 요청 파일을 만들어 제출한다.
  batch_id 를 runs 테이블에 저장하고 즉시 종료한다. 결과를 기다리지 마라.
  제출 전에 예상 비용을 로그로 찍고, max_daily_cost_usd 를 넘으면 중단한다.

step6_fetch_batch.py
  runs 테이블의 미완료 batch_id 를 폴링한다.
  아직 PENDING이면 exit code 75로 종료해서 워크플로가 재시도하게 한다.
  완료되면 결과를 파싱해 events 테이블에 저장한다.
  스키마 검증에 실패한 응답은 버리지 말고 raw 그대로 남기고
  실패 카운트를 올려라. 프롬프트를 고칠 근거가 된다.

step7_synthesize.py
  그날의 사건 요약을 모아 daily_synthesis 를 1회 호출하고
  briefings 테이블에 저장한다.

완료 조건: fake provider로 step5 → step6 → step7 전체가 돌아가는 통합 테스트.
PENDING 상태와 스키마 검증 실패 케이스를 둘 다 테스트에 포함시켜라.
```

---

### 프롬프트 6 — 글로브 화면 (step8 + 프론트엔드)

```text
첫 진입 화면인 3D 글로브와 정적 사이트 빌드를 구현해라.

────────── 목표 화면 ──────────
어두운 3D 지구본이 전체 화면에 뜨고, 관심도가 높은 나라 위에
발광하는 원형 마커가 떠 있다. 급등한 나라에는 바깥으로 퍼져나가는
링 애니메이션이 붙는다. 마커를 클릭하면 우측 패널이 슬라이드로 열리며
그 나라의 오늘 사건 목록이 나타난다. 사건을 클릭하면 상세 페이지로 이동.
지구본에는 야간 텍스처와 반투명 대기 레이어를 씌워 어둡고 차분하게 만든다.
────────────────────────────

step8_build_site.py — Jinja2로 정적 사이트 생성

  (a) site/dist/data/globe.json 생성
      countries[] 각 항목:
        iso2, name_ko, lat, lng,
        weight (0~1, country_weights에서),
        article_count, event_count,
        avg_tone (소속 사건 논조점수 평균),
        surge,
        top_event { id, headline, source_count }
      좌표는 config/countries.yaml 에서 가져온다.

  (b) site/dist/data/country/{ISO2}.json 생성
      그 나라의 오늘 사건 목록. 각 사건은
        id, headline, summary, category, tone, source_count,
        sources[] { domain, title, url }
      기사 원문은 절대 넣지 마라. 요약 + 출처 링크만.

  (c) 페이지 생성
      index.html      글로브 화면
      country/{ISO2}  국가별 사건 목록 (JS 없이도 읽히는 폴백 페이지)
      event/{id}      사건 상세
      briefing/{date} 일일 브리핑
      최근 30일만 정적 빌드. 그보다 오래된 것은 Worker가 처리한다.

site/static/globe.js — globe.gl 로 글로브 구현

  라이브러리는 globe.gl (three.js 기반) 을 script 태그로 로드한다.
  - pointsData(globe.json 의 countries)
  - pointRadius: Math.sqrt(article_count) 를 정규화. 선형은 쓰지 마라.
    미국이 혼자 거대해져서 나머지가 안 보인다.
  - pointColor: avg_tone 으로 보간. 부정(-10) → 주황/적, 중립(0) → 청색.
  - pointAltitude: weight 에 비례. 살짝만.
  - ringsData: surge > 1.8 인 상위 3개국만. 그 이상 켜면 화면이 시끄럽다.
  - onPointClick: 우측 패널을 열고 data/country/{ISO2}.json 을 fetch 해 렌더.
  - labelsData: 상위 12개국만. 전부 켜면 겹쳐서 못 읽는다.
  - 자동 회전은 켜되, 사용자가 드래그하면 멈추고 5초 뒤 재개.

  반드시 지킬 것:
  - WebGL 초기화에 실패하면 글로브를 숨기고 국가 목록 테이블을 보여줘라.
    글로브가 안 뜨면 사이트 전체가 빈 화면이 되는 건 용납 못 한다.
  - prefers-reduced-motion 이면 자동 회전과 링 애니메이션을 끈다.
  - 화면 폭 640px 이하에서는 글로브를 상단 고정 높이(40vh)로 줄이고
    국가 목록을 주인공으로 만들어라. 폰에서 지구본을 돌려
    나라를 찾는 사람은 없다.
  - 패널은 Esc로 닫히고, 포커스 트랩과 가시적 포커스 링을 넣어라.
  - globe.json 로딩 중에는 스켈레톤을 보여라. 빈 화면 금지.

site/worker/ — Cloudflare Worker (TypeScript)
  Turso/D1을 읽어:
    GET /api/search?q=        FTS5 인덱스 사용
    GET /archive/:date
  D1 무료 한도(읽기 500만 행/일)를 넘지 않게 페이지네이션을 강제해라.

완료 조건:
- python -m pipeline.step8_build_site --from-fixtures 로
  샘플 데이터만 가지고 site/dist/ 가 생성된다.
- index.html 을 로컬 서버로 열면 글로브가 뜨고, 마커 클릭 시 패널이 열린다.
- 브라우저에서 WebGL을 끈 상태로 열어도 국가 목록이 정상적으로 보인다.
- 400px 폭에서 가로 스크롤이 생기지 않는다.
```

---

### 프롬프트 7 — 배포 자동화

```text
GitHub Actions 워크플로와 배포를 구현해라. 크론은 세 개다.

.github/workflows/weights.yml
  cron '0 18 * * 0'  (= 월요일 03:00 KST). step0 실행.
  국가 가중치를 주 1회 갱신한다.

.github/workflows/collect.yml
  cron '5 */6 * * *' (= 6시간마다). step1만 실행.
  RSS는 최신 N건만 노출되므로 하루 1회 폴링하면 발행량이 많은 매체를 놓친다.
  수집은 API 호출뿐이라 2분이면 끝난다.

.github/workflows/analyze.yml
  두 개의 job:
    submit  — cron '10 15 * * *' (= 00:10 KST). step2~step5.
    publish — cron '10 19 * * *' (= 04:10 KST). step6~step8 후 Pages 배포.
  publish job에서 step6이 exit 75면 20분 뒤 재시도, 최대 3회.
  3회 다 실패하면 이슈를 생성한다.

세 워크플로 공통:
  - concurrency 그룹으로 중복 실행을 막아라.
  - 실패 시 GitHub 이슈를 자동 생성해라. 크론은 조용히 죽는다.
  - 시크릿: ANTHROPIC_API_KEY, OPENAI_API_KEY, TURSO_DATABASE_URL,
    TURSO_AUTH_TOKEN, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY, CF_API_TOKEN. .env.example 에 전부 나열해라.
  - public repo면 Actions 분은 무제한이지만,
    private면 월 2,000분이므로 각 job의 예상 소요를 README에 적어라.

추가로 README.md 를 작성해라:
  - 로컬에서 전체 파이프라인을 fixtures 로 돌리는 방법
  - 모델을 바꾸는 방법 (config/models.yaml 한 파일)
  - 수집량을 바꾸는 방법 (config/sources.yaml 의 budget)
  - 국가를 추가하는 방법 (config/countries.yaml, FIPS 코드 주의)
  - 현재 예상 월 비용 표

완료 조건: act 또는 워크플로 문법 검증 도구로 세 yml이 유효하고,
job 간 의존과 재시도 로직이 의도대로 걸려 있다.
```

---

## 7. 처음부터 지킬 것

| # | 항목 | 이유 |
|---|---|---|
| 01 | **FIPS ↔ ISO 매핑을 config로 일원화** | GDELT는 FIPS를 쓴다. 한국 `KS`, 중국 `CH`. 섞이면 조용히 엉뚱한 나라 기사를 모은다 |
| 02 | **`filter_log`에 걸러낸 이유 기록** | 없으면 "왜 이 기사가 안 들어왔지?"를 영원히 답할 수 없다 |
| 03 | **`embeddings` 테이블에 `model` 컬럼** | 임베딩 모델을 바꾸면 기존 벡터와 거리 계산이 호환되지 않는다 |
| 04 | **`max_daily_cost_usd`를 코드가 강제** | 피드 하나 잘못 추가해 수집량이 10배 되는 사고는 반드시 한 번 난다 |
| 05 | **배분에 floor와 cap 둘 다** | floor 없으면 작은 나라는 영원히 0건, cap 없으면 미국이 예산 절반을 먹는다 |
| 06 | **`maxrecords=250`을 명시** | 미지정 시 기본 75건. 수집량이 3분의 1로 조용히 줄어든다 |
| 07 | **배치 대기는 최대 24시간** | 통상 1~4h지만 보장은 24h. 재시도 경로를 처음부터 만들어라 |
| 08 | **WebGL 폴백** | 글로브가 안 뜨면 사이트 전체가 빈 화면이 된다 |
| 09 | **원문은 링크만** | GDELT는 본문을 주지 않는다. 직접 크롤링 시 `robots.txt` 확인, 게시는 요약+링크만 |
| 10 | **약관이 금지하는 사이트는 건드리지 않는다** | Investing.com은 공개 API가 없고 약관상 금지. 공개 사이트의 데이터 소스로 삼으면 서비스 전체가 위험해진다 |

---

## 8. 예상 월 비용

| 항목 | 서비스 | 월 비용 |
|---|---|---|
| 뉴스 수집 | GDELT 2.0 + RSS | $0 |
| 국가 가중치 | GDELT timelinesourcecountry | $0 |
| 스케줄러 | GitHub Actions (~1,100분) | $0 |
| 원문 보관 | Cloudflare R2 (60MB/월) | $0 |
| 파생 데이터 | Turso 또는 D1 (32MB/월) | $0 |
| 정적 배포 | Cloudflare Pages | $0 |
| 임베딩 | text-embedding-3-small | $0.17 |
| 사건 분석 | Haiku 4.5 batch (5,400 호출) | $21.60 |
| 사건 분석 (저가안) | Gemini 2.5 Flash-Lite | $4.00 |
| 일일 종합 | Sonnet 5 batch (30 호출) | $1.80 |
| 검색 API (선택) | Workers Paid | $5.00 |
| **합계** | 저가안 → 품질안 | **$6 – $29** |

---

## 참고 자료

- [GDELT DOC 2.0 API — 쿼리당 최대 250건, 기본 75건](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- [gdelt-doc-api — timelinesourcecountry 등 모드와 필터](https://github.com/alex9smith/gdelt-doc-api)
- [globe.gl — points / rings / onPointClick API](https://github.com/vasturiano/globe.gl)
- [Investing.com — 공개 API 미제공 공식 안내](https://www.investing-support.com/hc/en-us/articles/115005473825-Do-You-Offer-API-Access-at-Investing-com)
- [Claude API 요금](https://platform.claude.com/docs/en/about-claude/pricing)
- [GitHub Actions 요금 — public repo 무제한 / private 2,000분](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
