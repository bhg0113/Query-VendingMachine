"""
학습/증강용 시드 SQL이 테스트셋 SQL과 겹치지 않는지(데이터 누출) 점검하는 스크립트

주의:
- 이 스크립트는 "SQL 문자열이 동일(정규화 후)"한 경우만 탐지합니다.
- 의미적으로 비슷하지만 문자열이 다른 쿼리는 탐지하지 않습니다.
- 테스트 CSV에는 멀티라인 SQL이 들어갈 수 있어, pandas 대신 표준 csv 모듈로 파싱합니다.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path


def normalize_sql(sql: str) -> str:
    """
    SQL 비교를 위한 간단 정규화:
    - 대소문자 무시
    - 공백/개행 제거
    - 주석 제거(--, /* */)
    - 끝의 세미콜론 제거
    """
    if sql is None:
        return ""
    s = str(sql)

    # 블록 주석 제거
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    # 라인 주석 제거
    s = re.sub(r"--[^\n]*", "", s)

    s = s.strip()
    # 마지막 세미콜론 제거(여러 개 있을 수 있음)
    s = re.sub(r";+\s*$", "", s)

    # 공백 제거 + 소문자화
    s = re.sub(r"\s+", "", s).lower()
    return s


def _get_question_col(fieldnames: list[str] | None) -> str | None:
    """BOM(\\ufeff)이 섞인 question 컬럼명을 대응하기 위한 유틸 (참고용)"""
    if not fieldnames:
        return None
    for k in fieldnames:
        if k.replace("\ufeff", "").lower() == "question":
            return k
    return None


def load_sql_set(csv_path: Path) -> set[str]:
    """
    CSV에서 sql 컬럼을 읽어 정규화한 집합으로 반환합니다.
    - SQL이 멀티라인일 수 있으므로 반드시 csv 모듈 사용
    """
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"CSV 헤더를 읽지 못했습니다: {csv_path}")

        if "sql" not in reader.fieldnames:
            raise ValueError(f"CSV에 'sql' 컬럼이 없습니다: {csv_path} (columns={reader.fieldnames})")

        sql_set: set[str] = set()
        for row in reader:
            s = normalize_sql(row.get("sql", ""))
            if s:
                sql_set.add(s)
        return sql_set


def main() -> int:
    root = Path(__file__).resolve().parents[1]

    seed_path = root / "experiments" / "dvdrental_seed_trainset.csv"
    test_basic_path = root / "experiments" / "dvdrental_testset.csv"
    test_adv_path = root / "experiments" / "dvdrental_testset_advanced.csv"

    missing = [p for p in [seed_path, test_basic_path, test_adv_path] if not p.exists()]
    if missing:
        print("❌ 필요한 파일이 없습니다:")
        for p in missing:
            print(f" - {p}")
        return 2

    seed_set = load_sql_set(seed_path)
    test_basic_set = load_sql_set(test_basic_path)
    test_adv_set = load_sql_set(test_adv_path)

    test_all = test_basic_set | test_adv_set
    overlap = sorted(list(seed_set & test_all))

    print("=" * 80)
    print("🔎 시드 SQL ↔ 테스트셋 SQL 중복 검사")
    print("=" * 80)
    print(f"- seed 개수: {len(seed_set)}")
    print(f"- test(basic) 개수: {len(test_basic_set)}")
    print(f"- test(advanced) 개수: {len(test_adv_set)}")
    print(f"- 중복 개수: {len(overlap)}")

    if overlap:
        print("\n❌ 중복이 발견되었습니다. (정규화된 SQL 일부 출력)")
        for i, s in enumerate(overlap[:10]):
            print(f"{i+1:02d}. {s[:200]}{'…' if len(s) > 200 else ''}")
        print("\n👉 시드 SQL을 수정하여 테스트셋과 분리해 주세요.")
        return 1

    print("\n✅ 중복이 없습니다. (시드/테스트 분리 OK)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


