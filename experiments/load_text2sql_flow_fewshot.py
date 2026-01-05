"""
TEXT2SQL-FLOW로 생성한 증강 데이터셋을 few-shot 컬렉션(seed_examples)에 적재하는 스크립트

목적:
- experiments/text2sql_flow_augmented.csv(증강 결과)를 읽어서
- question(augmented_question) 임베딩을 생성하고
- seed_examples 테이블에 (question, sql, embedding) 형태로 적재합니다.

주의:
- 임베딩 생성(OpenAI)이 포함되므로 비용이 발생합니다.
- 출력 데이터(증강 CSV)가 매우 크면 적재 시간이 길어질 수 있습니다.
"""

from __future__ import annotations

import os
import sys
import argparse
import csv
from pathlib import Path
from typing import Dict, List, Tuple

# 로컬에서 `python experiments/load_text2sql_flow_fewshot.py`처럼 실행해도
# 프로젝트 루트 패키지(retrievers, utils 등)를 찾을 수 있도록 경로를 추가합니다.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from retrievers.seed_fewshot_retriever import SEED_EXAMPLES_TABLE, ensure_seed_examples_loaded
from utils.db_utils import get_embedding, run_command, run_query
from utils.logging_utils import log_step


def _load_existing_pairs() -> set[Tuple[str, str]]:
    """seed_examples에 이미 존재하는 (question, sql) 페어를 모두 읽어와 중복 적재를 방지합니다."""
    rows = run_query(f"SELECT question, sql FROM {SEED_EXAMPLES_TABLE};", dvd=False)
    return {(str(r["question"]), str(r["sql"])) for r in rows}


def _upsert_example(question: str, sql: str, embedding: List[float]) -> None:
    upsert_sql = f"""
    INSERT INTO {SEED_EXAMPLES_TABLE} (question, sql, embedding)
    VALUES (:question, :sql, (:embedding)::vector)
    ON CONFLICT (question, sql) DO UPDATE SET
        embedding = EXCLUDED.embedding;
    """
    run_command(
        upsert_sql,
        {"question": question, "sql": sql, "embedding": embedding},
        dvd=False,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="TEXT2SQL-FLOW 증강 데이터를 seed_examples(few-shot) 테이블에 적재")
    p.add_argument(
        "--in-csv",
        type=str,
        default="experiments/text2sql_flow_augmented.csv",
        help="입력 증강 CSV 경로",
    )
    p.add_argument(
        "--only-exec-ok",
        type=int,
        default=1,
        help="exec_ok=1 인 행만 적재(기본 1). 0이면 전체 적재",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="적재할 최대 행 수(0이면 제한 없음). 테스트용으로 사용",
    )
    args = p.parse_args()

    in_path = Path(args.in_csv)
    if not in_path.exists():
        raise FileNotFoundError(f"입력 CSV가 없습니다: {in_path}")

    # seed_examples 테이블 보장(+ 비어있으면 기본 시드도 로딩)
    ensure_seed_examples_loaded()

    existing = _load_existing_pairs()
    log_step("FLOW few-shot 적재 시작", {"in_csv": str(in_path), "기존_페어_수": len(existing)})

    inserted = 0
    skipped_dup = 0
    skipped_invalid = 0

    with in_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV 헤더를 읽지 못했습니다.")

        required = {"augmented_question", "augmented_sql", "exec_ok"}
        if not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"증강 CSV 컬럼이 예상과 다릅니다: need={sorted(required)} got={reader.fieldnames}")

        for i, row in enumerate(reader, 1):
            if args.max_rows and i > args.max_rows:
                break

            q = (row.get("augmented_question") or "").strip()
            s = (row.get("augmented_sql") or "").strip()
            exec_ok = str(row.get("exec_ok") or "").strip()

            if not q or not s:
                skipped_invalid += 1
                continue

            if args.only_exec_ok and exec_ok != "1":
                skipped_invalid += 1
                continue

            pair = (q, s)
            if pair in existing:
                skipped_dup += 1
                continue

            emb = get_embedding(q)
            _upsert_example(q, s, emb)
            existing.add(pair)
            inserted += 1

            if inserted % 50 == 0:
                log_step("FLOW few-shot 적재 진행", {"inserted": inserted, "skipped_dup": skipped_dup})

    log_step(
        "FLOW few-shot 적재 완료",
        {"inserted": inserted, "skipped_dup": skipped_dup, "skipped_invalid": skipped_invalid},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

