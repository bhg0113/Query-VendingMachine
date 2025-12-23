## SEED 통합 가이드 (Query VendingMachine)

이 문서는 현재 프로젝트에 **SEED(Automatic Evidence Generation)** 스타일의 evidence 생성 단계를 통합한 구현을 설명합니다.  
핵심은 **(질문만 주어지는 현실 시나리오에서) 스키마/값/조인 힌트 기반 evidence를 자동 생성**하고, 이를 **SQL 생성 프롬프트에 주입**해 정확도를 높이는 것입니다.  

- 참고: [SEED 논문 PDF](file:///Users/kane/Query-VendingMachine/SEED_%20Enhancing%20Text-to-SQL%20Performance%20and%20Practical%20Usability%20Through%20Automatic%20Evidence%20Generation.pdf), [SEED 리뷰](https://www.themoonlight.io/ko/review/seed-enhancing-text-to-sql-performance-and-practical-usability-through-automatic-evidence-generation)

---

## 1) 현재 프로젝트에서 SEED가 “어디에” 붙었나?

### 전체 흐름(요약)
- **질문 입력**
- **테이블 리트리버**(`DVDRentalRetriever`)가 질문과 관련된 테이블 문서(`table_docs`)를 최대 10개 검색
- (옵션) **SEED evidence 생성**(`chains/seed_evidence_chain.py`)
- **SQL 생성 프롬프트**에 아래 변수를 함께 주입
  - `question`: 원본 질문
  - `clarified_question`: evidence 생성 시 함께 만든 “더 명확한 질문”
  - `evidence`: 자동 생성된 힌트 텍스트
  - `context`: 리트리버가 가져온 테이블 문서(DDL/설명)
- LLM이 최종 SQL을 출력

---

## 2) 모드: lite / full (2개만 유지)

현재 구현은 **`SEED_MODE=lite|full` 2가지 모드만** 제공합니다.

### SEED_MODE=lite (초기 SEED-lite: 가볍고 안정적)
- **스키마 입력**: 리트리버가 뽑은 테이블 중심(후보 최대 10개)
- **evidence 재료**
  - 후보 테이블 DDL
  - 후보 테이블 간 FK 조인 힌트
  - 일부 컬럼의 대표값(유니크 값) 샘플(휴리스틱)
- **few-shot**: 사용 안 함
- **Sample SQL Execution**: 사용 안 함
- **특징**: 비용/지연이 비교적 작고, “조인 키 실수/집계 기준 혼동” 같은 오류를 완화하는 데 초점

### SEED_MODE=full (논문 SEED에 최대한 근접: 느리지만 강함)
- **스키마 입력**: 가능한 한 **public 스키마 전체 테이블 DDL**을 포함(=SEEDgpt 방향에 근접)
  - 전체 스키마 로드가 실패하면 자동으로 lite 방식으로 폴백합니다.
- **evidence 재료(논문 프롬프트 구성요소에 근접)**
  - 전체 스키마 DDL
  - “description files” 대체: 리트리버의 `table_docs` 문서
  - **few-shot 예시(임베딩 검색 + 자동 필터링)**
  - **Sample SQL Execution(값 중심, 노이즈 최소화)**
- **특징**: 비용/지연이 늘지만, 값 매핑/조인 경로/집계 정의를 더 강하게 유도할 수 있음

---

## 3) 실행 방법(환경변수)

### 기본(기존과 동일)
- evidence를 사용하지 않음(기본값)
- 환경변수 `ENABLE_SEED_EVIDENCE`를 설정하지 않거나 `0`

### lite 모드 ON

```bash
ENABLE_SEED_EVIDENCE=1
SEED_MODE=lite
```

### full 모드 ON

```bash
ENABLE_SEED_EVIDENCE=1
SEED_MODE=full
```

> 주의: full 모드는 **추가 임베딩 검색 + 추가 DB 샘플 쿼리 + 추가 LLM 호출** 때문에 비용/지연이 더 증가합니다.

---

## 4) few-shot 예시는 “어디에 있고”, “어떻게 추가되나?”

### 원본(사람이 관리)
- `experiments/dvdrental_seed_trainset.csv` (컬럼: `question,sql`)
- 여기에 페어를 추가하면 few-shot 풀을 늘릴 수 있습니다.

### 적재/검색(자동)
- full 모드에서 evidence 생성 시, `retrievers/seed_fewshot_retriever.py`가 아래를 수행합니다.
  - `seed_examples` 테이블이 비어 있으면 CSV를 읽어 **질문 임베딩과 함께 자동 적재**
  - 현재 질문 임베딩으로 top-N을 검색한 뒤
  - **노이즈 최소화 규칙**으로 “질문에 필요한 예시만” 선택해 evidence 프롬프트에 포함

### 고정 K 대신 “자동 선택” (SEED_FEWSHOT_K 대체)
과거처럼 `K=5`로 고정하지 않고, full 모드에서는 다음 휴리스틱으로 자동 선택합니다.
- top-N(pool=20)을 넓게 가져온 뒤
- 가장 가까운 예시의 distance \(d_0\) 기준으로 \(d \le d_0 + 0.12\)만 유지
- SQL이 후보 테이블을 전혀 언급하지 않으면 제외(도메인 노이즈 감소)
- 최소 2개는 유지(부족하면 상위 2개로 폴백)
- 최대 8개까지

---

## 5) Sample SQL Execution: “필요한 값만” 활용하는 방식

논문 SEED의 Sample SQL Execution 아이디어를 최대한 따라가되, 노이즈를 줄이기 위해 아래처럼 구현했습니다.

- **값 후보 추출**: 질문에서 **따옴표로 명시된 값**만 추출
  - 예: `'Horror'`, `‘PG’`, `'Bergman'`
  - 결과 컬럼 라벨(대부분 snake_case)은 노이즈로 보고 제외
- **후보 생성(LLM)**: “ALLOWED VALUES에 있는 값만 사용”하도록 강제 → 값 hallucination 최소화
- **DB 샘플 실행**: candidate_value 중심으로만 조회
  - equals → case-insensitive exact match → 없으면 ILIKE로 유사값만
  - contains → ILIKE
- **결과가 비어있으면 evidence에 포함하지 않음**(노이즈 제거)

---

## 6) 논문(SEED)과의 유사점/차이점

### 유사점 (핵심 아이디어는 동일)
- **증거(evidence)가 없는 현실 시나리오**를 가정하고, evidence를 자동 생성해 SQL 성능을 올리려는 목표
- evidence 생성에 **스키마 + 값(샘플링) + few-shot**을 활용한다는 구성
- evidence를 **최종 SQL 생성에 조건/힌트로 주입**한다는 사용 방식

### 차이점 (프로젝트 특성/간소화/대체 구현)
- **SEEDgpt vs SEEDdeepseek**
  - 논문: 컨텍스트 길이에 따라 “전체 스키마 사용(SEEDgpt)” vs “스키마 요약(SEEDdeepseek)”
  - 우리: `SEED_MODE=full`에서 “전체 스키마 DDL”을 최대한 포함, `lite`는 리트리버 기반으로 범위를 축소(LLM 기반 스키마 요약을 직접 구현하진 않음)
- **few-shot 유사도 모델**
  - 논문: all-mpnet-base-v2 + cosine 유사도
  - 우리: OpenAI 임베딩(text-embedding-3-small) + pgvector cosine distance(<=>)로 대체
- **Sample SQL Execution의 값 후보**
  - 논문: 질문에서 키워드/값 후보를 더 폭넓게 뽑고(문자열이면 LIKE/유사값 등), 샘플 실행 결과를 evidence 생성에 사용
  - 우리: 노이즈 최소화를 위해 “따옴표로 명시된 값” 중심 + DB 매칭 결과가 있는 항목만 evidence에 포함(간단하지만 안정적)
- **description files**
  - 논문: BIRD의 description/evidence 파일을 전제로 하는 구성
  - 우리: `table_docs`(프로젝트의 테이블 문서)로 대체하여 evidence 프롬프트에 포함
- **평가/벤치마크**
  - 논문: BIRD/Spider에서 다양한 모델을 evidence 유무로 비교 평가
  - 우리: dvdrental 기반 실험(실험 1/2)에서 “evidence ON/OFF”를 비교하는 형태로 확장 가능(현재 파이프라인은 준비됨)

---

## 7) 데이터 누출(leakage) 방지

테스트셋 SQL을 학습/증강용(또는 few-shot 풀)로 쓰면 누출 문제가 생깁니다.  
그래서 few-shot 풀은 별도 파일로 관리합니다.

- `experiments/dvdrental_seed_trainset.csv`: 학습/증강/예시 풀(분리)
- `experiments/check_seed_leakage.py`: 시드 SQL과 테스트셋 SQL의 “정규화 후 문자열 동일” 중복 검사

```bash
python experiments/check_seed_leakage.py
```


