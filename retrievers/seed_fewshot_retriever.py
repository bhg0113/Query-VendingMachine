"""
SEED few-shot 예시 검색 유틸 (임베딩 기반)

논문 SEED는 evidence 생성 프롬프트에 few-shot 예시를 포함하고,
질문 유사도(임베딩 + cosine)로 예시를 선택합니다.

이 프로젝트에서는 OpenAI 임베딩(text-embedding-3-small) + pgvector(<=>)로
간단히 구현합니다.
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from utils.db_utils import get_embedding, run_command, run_query
from utils.logging_utils import log_step


SEED_EXAMPLES_TABLE = "seed_examples"
SEED_EXAMPLES_DIM = 1536  # text-embedding-3-small


def _workspace_path() -> Path:
    # retrievers/ 아래 파일이므로 repo root는 parents[1]
    return Path(__file__).resolve().parents[1]


def _seed_trainset_csv_path() -> Path:
    return _workspace_path() / "experiments" / "dvdrental_seed_trainset.csv"


def _ensure_seed_examples_table() -> None:
    """pgvector + seed_examples 테이블을 보장합니다."""
    create_sql = f"""
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE IF NOT EXISTS {SEED_EXAMPLES_TABLE} (
        id SERIAL PRIMARY KEY,
        question TEXT NOT NULL,
        sql TEXT NOT NULL,
        embedding VECTOR({SEED_EXAMPLES_DIM}),
        created_at TIMESTAMP DEFAULT now(),
        UNIQUE(question, sql)
    );
    """
    run_command(create_sql, dvd=False)


def _count_seed_examples() -> int:
    row = run_query(f"SELECT COUNT(*) AS cnt FROM {SEED_EXAMPLES_TABLE};", dvd=False)[0]
    return int(row["cnt"])


def _load_seed_examples_from_csv(csv_path: Path) -> int:
    """
    CSV(question, sql)를 읽어 seed_examples 테이블에 임베딩과 함께 적재합니다.
    - 작은 데이터(수십~수백) 기준으로 단순 반복 insert
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"시드 학습 CSV를 찾을 수 없습니다: {csv_path}")

    inserted = 0
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "question" not in reader.fieldnames or "sql" not in reader.fieldnames:
            raise ValueError(f"CSV 컬럼이 올바르지 않습니다: {csv_path} (columns={reader.fieldnames})")

        for r in reader:
            q = (r.get("question") or "").strip()
            s = (r.get("sql") or "").strip()
            if not q or not s:
                continue

            emb = get_embedding(q)

            # NOTE: 기존 table_docs 삽입과 동일하게 embedding 파라미터를 그대로 전달합니다.
            # 환경에 따라 vector 캐스팅이 필요할 수 있어, 안전하게 ::vector 캐스팅을 함께 둡니다.
            upsert_sql = f"""
            INSERT INTO {SEED_EXAMPLES_TABLE} (question, sql, embedding)
            VALUES (:question, :sql, (:embedding)::vector)
            ON CONFLICT (question, sql) DO UPDATE SET
                embedding = EXCLUDED.embedding;
            """
            run_command(
                upsert_sql,
                {"question": q, "sql": s, "embedding": emb},
                dvd=False,
            )
            inserted += 1

    return inserted


@lru_cache(maxsize=1)
def ensure_seed_examples_loaded() -> bool:
    """
    seed_examples 테이블이 비어있으면 CSV로부터 로딩합니다.
    - 최초 1회만 수행되도록 캐시
    """
    _ensure_seed_examples_table()
    cnt = _count_seed_examples()
    if cnt > 0:
        return True

    csv_path = _seed_trainset_csv_path()
    log_step("SEED few-shot: 예시 테이블 로딩 시작", {"csv_path": str(csv_path)})
    inserted = _load_seed_examples_from_csv(csv_path)
    log_step("SEED few-shot: 예시 테이블 로딩 완료", {"inserted": inserted})
    return inserted > 0


def get_seed_fewshot_examples(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    질문과 유사한 few-shot 예시(question, sql)를 반환합니다.
    - distance는 cosine distance(<=>) 기준
    """
    if not ensure_seed_examples_loaded():
        return []

    query_emb = get_embedding(query)
    sql = f"""
        SELECT question, sql,
               embedding <=> (:query_emb)::vector AS distance
        FROM {SEED_EXAMPLES_TABLE}
        ORDER BY embedding <=> (:query_emb)::vector
        LIMIT :limit;
    """
    rows = run_query(sql, {"query_emb": query_emb, "limit": limit}, dvd=False)
    return rows


