# TEXT2SQL-FLOW 통합 가이드 (Query VendingMachine)

이 문서는 현재 프로젝트에 **TEXT2SQL-FLOW(SQL-aware Data Augmentation)** 스타일의 데이터 증강 파이프라인을 추가한 구현을 설명합니다.  
목표는 **소수의 시드 SQL로부터 실행 가능한(유효/효율) 대규모 NL/SQL 페어를 생성**해, 추후 **few-shot 풀 확장** 또는 **학습(SFT) 데이터 생성**에 활용하는 것입니다.

- 참고(논문 PDF): `TEXT2SQL-FLOW_ A Robust SQL-Aware Data Augmentation Framework for Text-to-SQL.pdf`
- 참고(개요/요약): `https://www.themoonlight.io/ko/review/text2sql-flow-a-robust-sql-aware-data-augmentation-framework-for-text-to-sql`

---

## 1) 이번 프로젝트에서 구현한 구성요소(논문 매핑)

### A. SQL Augmentation
- 입력: 시드 SQL + 스키마(S) + 샘플 값(V) + 증강 방향(direction)
- 출력: 증강 SQL 1개(SELECT/WITH만 허용)
- 구현: `augmentation/text2sql_flow.py`의 `augment_sql_once()`

### B. SQL Execution Filter
- 목적: 구문 오류, 실행 불가, 과도한 실행시간(비효율)을 필터링
- 구현(실용 버전):
  - SELECT/WITH만 허용(DML/DDL 차단)
  - statement_timeout(ms) 내 실행되는지 확인
  - 결과는 `SELECT * FROM (<sql>) LIMIT N` wrapper로 “안전 실행”
- 구현: `augmentation/text2sql_flow.py`의 `_execution_filter()`
- DB timeout 유틸: `utils/db_utils.py`의 `run_query_with_statement_timeout()`

### C. Question Generation
- 목적: 유효한 증강 SQL에 대해 자연어 질문(NL) 생성
- 구현: `augmentation/text2sql_flow.py`의 `generate_questions_for_sql()`
- 현재는 간단히 3가지 스타일(formal/colloquial/imperative)로 시작

---

## 2) 입력(시드) 데이터

- 기본 시드 파일: `experiments/dvdrental_seed_trainset.csv` (columns: `question, sql`)
- 주의: 테스트셋(`dvdrental_testset*.csv`) SQL을 시드로 쓰면 **데이터 누출(leakage)** 위험이 있습니다.
- 중복 검사: `experiments/check_seed_leakage.py`

---

## 3) 실행 방법

### 실행 스크립트
- `experiments/run_text2sql_flow.py`

### 예시 커맨드

```bash
python experiments/run_text2sql_flow.py \
  --seed-csv experiments/dvdrental_seed_trainset.csv \
  --out-csv experiments/text2sql_flow_augmented.csv \
  --sql-aug-per-seed 2 \
  --questions-per-sql 2 \
  --timeout-ms 2000 \
  --max-rows 20
```

### 출력 포맷
`experiments/text2sql_flow_augmented.csv`에 아래 컬럼으로 저장됩니다.
- `seed_id`: 시드 row id
- `direction`: 증강 방향
- `augmented_sql`: 증강 SQL
- `question_style`: 질문 스타일 키
- `augmented_question`: 생성된 질문
- `exec_ok`: 실행 필터 통과 여부(1/0)
- `exec_ms`: 실행 시간(ms, 성공 시)

---

## 4) 논문과의 유사점/차이점(현재 구현 기준)

### 유사점
- “시드 SQL → 다양한 방향으로 SQL 증강 → 실행 필터 → NL 질문 생성” 파이프라인 구조는 동일
- 스키마(S)와 샘플 값(V)을 프롬프트에 포함해 실행 가능성을 높이는 접근

### 차이점(간소화/프로젝트 현실화)
- 증강 방향은 논문 축을 반영하되, 현재는 프롬프트 지침을 단순화하여 사용
- DB Manager(다중 DB 커넥터/캐시 관리)는 아직 별도 추상화로 분리하지 않고, 프로젝트의 `utils/db_utils.py` 기반으로 구현
- Question Generation의 스타일 다양성은 논문 수준(다수 스타일/카테고리)까지 확장하지 않고, 우선 3가지로 시작
- Execution Filter는 “성능/효율” 측정 지표를 별도 계산하지 않고, statement_timeout 기반으로 실용 구현

---

## 5) 다음 확장(추천)

- 증강 데이터셋을 `pgvector` 테이블에 적재해 **few-shot 풀**로 사용
- SQL 구조 마스킹 기반 **structure-aware retrieval** 도입(논문에서 언급한 방향)
- 스타일 카테고리 확장(질문 다양성 증가)
- Execution Filter 강화(EXPLAIN ANALYZE 기반 VES/시간 측정 등)

---

## 6) (권장) 지금 프로젝트에서 바로 성능에 연결하기: few-shot 컬렉션으로 적재

TEXT2SQL-FLOW가 “성능 향상”에 기여하려면, 생성된 증강 데이터가 **추론 단계에서 활용**되거나(**few-shot**) **학습(SFT)**에 사용되어야 합니다.  
이 프로젝트에서는 우선 **few-shot 컬렉션으로 적재**하는 방식을 권장합니다.

### 6-1) 증강 데이터 생성

```bash
python experiments/run_text2sql_flow.py \
  --seed-csv experiments/dvdrental_seed_trainset.csv \
  --out-csv experiments/text2sql_flow_augmented.csv
```

### 6-2) 증강 데이터 → few-shot 테이블(seed_examples) 적재

아래 스크립트는 `text2sql_flow_augmented.csv`의 `augmented_question/augmented_sql`을
임베딩해서 `seed_examples` 테이블에 넣습니다.

```bash
python experiments/load_text2sql_flow_fewshot.py \
  --in-csv experiments/text2sql_flow_augmented.csv
```

> 주의: 임베딩 생성(OpenAI)이 포함되므로 비용이 발생합니다.

### 6-3) 추론에서 활용

현재 프로젝트의 **SEED full 모드**는 evidence 생성 시 `seed_examples`에서 유사 예시를 검색해 few-shot으로 사용합니다.  
즉, 위 적재를 완료하면 SEED(full)가 활용하는 few-shot 풀이 자동으로 확장됩니다.

