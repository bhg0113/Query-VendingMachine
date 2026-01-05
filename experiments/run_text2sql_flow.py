"""
TEXT2SQL-FLOW 증강 파이프라인 실행 스크립트

목적:
- 시드(question, sql) CSV를 입력으로 받아
- SQL 증강 → 실행 필터 → 질문 생성 을 거쳐
- 새로운 증강 데이터셋 CSV를 생성합니다.

주의:
- 비용이 큽니다(LLM 호출 + DB 실행).
- 테스트셋 SQL을 시드로 쓰면 데이터 누출(leakage)이 생기므로 피하세요.
"""

from __future__ import annotations

import os
import sys
import argparse
from pathlib import Path

# 로컬에서 `python experiments/run_text2sql_flow.py`처럼 실행해도
# 프로젝트 루트 패키지(augmentation, utils 등)를 찾을 수 있도록 경로를 추가합니다.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from augmentation.text2sql_flow import run_text2sql_flow_augmentation


def main() -> int:
    p = argparse.ArgumentParser(description="TEXT2SQL-FLOW 스타일 데이터 증강 실행")
    p.add_argument(
        "--seed-csv",
        type=str,
        default="experiments/dvdrental_seed_trainset.csv",
        help="시드 CSV 경로 (columns: question, sql)",
    )
    p.add_argument(
        "--out-csv",
        type=str,
        default="experiments/text2sql_flow_augmented.csv",
        help="출력 CSV 경로",
    )
    p.add_argument("--sql-aug-per-seed", type=int, default=2, help="시드 1개당 방향별 SQL 증강 시도 횟수")
    p.add_argument("--questions-per-sql", type=int, default=2, help="증강 SQL 1개당 질문 생성 개수(스타일 수)")
    p.add_argument("--timeout-ms", type=int, default=2000, help="실행 필터 statement_timeout(ms)")
    p.add_argument("--max-rows", type=int, default=20, help="실행 필터에서 서브쿼리 결과 LIMIT")

    args = p.parse_args()

    run_text2sql_flow_augmentation(
        seed_csv_path=Path(args.seed_csv),
        out_csv_path=Path(args.out_csv),
        sql_aug_per_seed=args.sql_aug_per_seed,
        questions_per_sql=args.questions_per_sql,
        statement_timeout_ms=args.timeout_ms,
        max_rows=args.max_rows,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

