"""
테스트셋 CSV에 존재하는 SQL을 실행해 label 컬럼을 자동으로 채우는 스크립트
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

# 루트 경로를 파이썬 모듈 검색 경로에 추가
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from utils import run_query


def format_rows(rows: list[dict]) -> str:
    """쿼리 실행 결과를 label 셀에 맞게 문자열로 직렬화한다."""
    if not rows:
        return ""

    columns = list(rows[0].keys())
    lines: list[str] = []

    for row in rows:
        formatted = []
        for col in columns:
            value = row.get(col)

            if value is None:
                formatted.append("NULL")
            else:
                formatted.append(str(value))

        lines.append(",".join(formatted))

    return "\n".join(lines)


def update_labels(csv_path: str) -> None:
    """CSV를 읽어 각 SQL을 실행하고 label 컬럼을 갱신한다."""
    df = pd.read_csv(csv_path)
    new_labels: list[str] = []

    for idx, row in df.iterrows():
        question = row["question"]
        sql = row["sql"]

        try:
            rows = run_query(sql, dvd=True)
            label_value = format_rows(rows)
            new_labels.append(label_value)
            print(f"[성공] ({idx + 1}/{len(df)}) {question[:40]}...")
        except Exception as exc:
            print(f"[실패] ({idx + 1}/{len(df)}) {question[:40]}...")
            print(f"       원인: {exc}")
            new_labels.append("")

    df["label"] = new_labels
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"✅ label 갱신 완료: {csv_path}")


def main():
    parser = argparse.ArgumentParser(description="테스트셋 label 자동 생성기")
    parser.add_argument(
        "--csv",
        default="experiments/dvdrental_testset_advanced.csv",
        help="label을 갱신할 CSV 경로",
    )
    args = parser.parse_args()

    update_labels(args.csv)


if __name__ == "__main__":
    main()

