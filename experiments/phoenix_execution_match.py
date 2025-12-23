"""
Phoenix Experiments용 실행결과 동치(Execution-match) 평가기

Text-to-SQL에서 SQL 문자열이 달라도 "실행 결과"가 같으면 정답으로 보는 평가를 제공합니다.
Phoenix 실험(Evaluators)에서 사용할 수 있는 커스텀 evaluator 함수입니다.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from utils.db_utils import run_query


def _to_str(v: Any) -> str:
    return "" if v is None else str(v)


def _normalize_scalar(v: Any) -> Any:
    """Row 비교를 위한 값 정규화 (타입 차이/표현 차이를 최대한 흡수)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        # 4.20 vs 4.2 같은 표현 차이를 줄이기 위해 normalize 사용
        try:
            return str(v.normalize())
        except Exception:
            return str(v)
    if isinstance(v, float):
        # 부동소수점은 문자열 기반 Decimal로 정규화
        try:
            return str(Decimal(str(v)).normalize())
        except Exception:
            return str(v)
    if isinstance(v, (int,)):
        return str(v)
    if isinstance(v, bytes):
        return v.hex()
    return v


def _normalize_rows(rows: List[Dict[str, Any]]) -> Tuple[Sequence[str], Counter]:
    """
    결과 row list를 (정렬된 컬럼명, row multiset) 형태로 정규화합니다.
    - ORDER BY가 없는 쿼리는 row 순서가 불안정할 수 있어 multiset으로 비교합니다.
    """
    if not rows:
        return (), Counter()

    # 컬럼은 키 집합으로 판단 (둘 다 같은 컬럼 집합이면 순서가 달라도 맞다고 봄)
    cols = sorted(list(rows[0].keys()))
    bag: Counter = Counter()
    for r in rows:
        bag[tuple(_normalize_scalar(r.get(c)) for c in cols)] += 1
    return cols, bag


def _run_sql(sql: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """SQL을 실행하고 (rows, error_message)를 반환."""
    try:
        rows = run_query(sql, dvd=True)
        return rows, None
    except Exception as e:
        return None, f"{type(e).__name__}: {_to_str(e)}"


def execution_match_evaluator(
    *,
    input: Optional[Mapping[str, Any]] = None,
    output: Any = None,
    expected: Any = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Phoenix Experiments evaluator.

    기대 형태:
    - input: {"question": "..."} (없어도 동작)
    - output: 모델이 생성한 SQL 문자열 또는 {"pred_sql": "..."} 형태
    - expected: 정답 SQL 문자열 또는 {"sql": "..."} 형태

    Returns:
        dict: {"score": float, "label": str, "explanation": str}
    """
    question = ""
    if isinstance(input, Mapping):
        question = _to_str(input.get("question", ""))

    # output에서 예측 SQL 추출
    pred_sql = ""
    if isinstance(output, Mapping):
        pred_sql = _to_str(output.get("pred_sql") or output.get("sql") or "")
    else:
        pred_sql = _to_str(output)

    # expected에서 정답 SQL 추출
    gold_sql = ""
    if isinstance(expected, Mapping):
        gold_sql = _to_str(expected.get("sql") or "")
    else:
        gold_sql = _to_str(expected)

    if not pred_sql.strip():
        return {
            "score": 0.0,
            "label": "no_sql",
            "explanation": f"예측 SQL이 비어있습니다. question={question[:80]}",
        }
    if not gold_sql.strip():
        return {
            "score": 0.0,
            "label": "no_gold",
            "explanation": "정답 SQL이 비어있어 평가할 수 없습니다.",
        }

    pred_rows, pred_err = _run_sql(pred_sql)
    gold_rows, gold_err = _run_sql(gold_sql)

    if gold_err:
        return {
            "score": 0.0,
            "label": "gold_error",
            "explanation": f"정답 SQL 실행이 실패했습니다: {gold_err}",
        }
    if pred_err:
        return {
            "score": 0.0,
            "label": "pred_error",
            "explanation": f"예측 SQL 실행이 실패했습니다: {pred_err}",
        }

    gold_cols, gold_bag = _normalize_rows(gold_rows or [])
    pred_cols, pred_bag = _normalize_rows(pred_rows or [])

    if set(gold_cols) != set(pred_cols):
        return {
            "score": 0.0,
            "label": "schema_mismatch",
            "explanation": f"컬럼 집합 불일치 gold={list(gold_cols)} pred={list(pred_cols)}",
        }

    match = gold_bag == pred_bag
    if match:
        return {"score": 1.0, "label": "match", "explanation": "실행 결과가 일치합니다."}

    # 너무 큰 diff는 출력 폭주 방지
    return {
        "score": 0.0,
        "label": "mismatch",
        "explanation": f"실행 결과가 다릅니다. gold_rows={len(gold_rows or [])}, pred_rows={len(pred_rows or [])}",
    }

