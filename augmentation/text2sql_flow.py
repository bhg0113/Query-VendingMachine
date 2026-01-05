"""
TEXT2SQL-FLOW (SQL-aware Data Augmentation) 파이프라인 구현 (프로젝트용 실용 버전)

논문 요지(요약):
- 소수의 시드(seed) SQL을 입력으로 받아, 다양한 증강 방향에 따라 새로운 SQL을 생성하고
- 실제 DB에서 실행해 유효/효율을 필터링한 뒤
- 해당 SQL에 대응되는 자연어 질문을 여러 스타일로 생성해 대규모 NL/SQL 페어를 만듭니다.

본 구현은 dvdrental(PostgreSQL) + 현재 프로젝트의 LLM/DB 유틸을 사용해
"돌아가는 파이프라인"을 만드는 것을 목표로 합니다.

참고:
- TEXT2SQL-FLOW 논문: TEXT2SQL-FLOW_ A Robust SQL-Aware Data Augmentation Framework for Text-to-SQL.pdf
- 개요/요약: https://www.themoonlight.io/ko/review/text2sql-flow-a-robust-sql-aware-data-augmentation-framework-for-text-to-sql
"""

from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from config.llm_config import get_llm
from utils.db_utils import extract_ddl, run_query_with_statement_timeout, run_query
from utils.logging_utils import log_step


# -----------------------------
# 설정/상수
# -----------------------------

FLOW_DIRECTIONS: Dict[str, str] = {
    # 논문에서 언급된 증강 축을 프로젝트에 맞게 간단히 정리한 프롬프트 지침입니다.
    "Data Value Transformations": "WHERE 조건의 값/범위를 바꾸거나 LIKE 패턴을 변형하는 등 데이터 값 중심으로 변형하되, 의미적으로 말이 되게 작성하세요.",
    "Query Structure Modifications": "JOIN 순서 변경, CTE로 리팩토링, EXISTS/IN 변환 등 쿼리 구조를 바꾸되 결과 의미는 합리적이어야 합니다.",
    "Business Logic Changes": "집계 기준/그룹핑/필터 조건을 바꿔 비즈니스 로직이 달라진 새로운 질문/SQL이 되도록 만드세요.",
    "Complexity Enhancements": "서브쿼리/윈도우 함수/추가 집계 등 난이도를 올리되 실행 가능해야 합니다.",
    "Advanced SQL Features": "CTE, window, DISTINCT, CASE WHEN 등 고급 문법을 활용하되 과도한 복잡도는 피하세요.",
    "Performance and Optimization": "동일한 의미를 유지하며 성능을 개선하는 형태로 변형하세요(예: EXISTS 사용). 단, 의미가 바뀌면 안 됩니다.",
}

QUESTION_STYLES: Dict[str, str] = {
    # 논문은 다양한 스타일을 언급하지만, 여기서는 실용적으로 몇 가지로 시작합니다.
    "formal": "정중하고 설명형 문장으로 작성",
    "colloquial": "구어체로 자연스럽게 작성",
    "imperative": "명령형/요청형으로 간결하게 작성",
}


# -----------------------------
# 데이터 구조
# -----------------------------


@dataclass(frozen=True)
class FlowSeed:
    """시드 데이터(질문-정답 SQL)"""

    seed_id: str
    question: str
    sql: str


@dataclass(frozen=True)
class FlowAugmented:
    """증강 결과(질문-증강 SQL)"""

    seed_id: str
    direction: str
    augmented_sql: str
    question_style: str
    augmented_question: str
    exec_ok: bool
    exec_ms: Optional[int]


# -----------------------------
# 유틸: SQL 정리/검증/추출
# -----------------------------


def _strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:sql)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _normalize_sql_for_dedup(sql: str) -> str:
    """
    중복 제거를 위한 간단 정규화:
    - 주석 제거
    - 공백 제거
    - 대소문자 무시
    - 끝의 세미콜론 제거
    """
    s = (sql or "").strip()
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"--[^\n]*", "", s)
    s = s.strip()
    s = re.sub(r";+\s*$", "", s)
    s = re.sub(r"\s+", "", s).lower()
    return s


def _is_safe_select_sql(sql: str) -> bool:
    """
    안전을 위해 SELECT/WITH만 허용 (DML/DDL 차단).
    """
    s = (sql or "").strip().lower()
    if not s:
        return False
    # 시작 토큰
    if not (s.startswith("select") or s.startswith("with")):
        return False
    # 위험 키워드(보수적으로 차단)
    forbidden = ["insert", "update", "delete", "drop", "alter", "truncate", "create", "grant", "revoke"]
    if any(re.search(rf"\b{kw}\b", s) for kw in forbidden):
        return False
    return True


def _extract_table_names_from_sql(sql: str) -> List[str]:
    """
    SQL에서 FROM/JOIN 뒤의 테이블명을 간단히 추출합니다(휴리스틱).
    - 완벽한 SQL 파서는 아니며, 증강 프롬프트용 "대략의 후보 테이블"을 만들기 위한 용도입니다.
    """
    s = (sql or "")
    tables = []
    for pat in [r"\bfrom\s+([a-zA-Z_][a-zA-Z0-9_]*)", r"\bjoin\s+([a-zA-Z_][a-zA-Z0-9_]*)"]:
        tables.extend(re.findall(pat, s, flags=re.IGNORECASE))
    # 중복 제거 + 입력 순서 유지
    seen: set[str] = set()
    out: List[str] = []
    for t in tables:
        tt = t.strip()
        if not tt:
            continue
        low = tt.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(tt)
    return out


def _sample_distinct_values_for_table(
    table_name: str,
    ddl: Dict[str, Dict[str, Any]],
    *,
    max_columns: int = 6,
    max_values_per_column: int = 10,
) -> Dict[str, List[Any]]:
    """
    테이블에서 조건으로 자주 쓰는 컬럼(name/title/rating 등)의 대표 값을 샘플링합니다.
    (TEXT2SQL-FLOW의 V(샘플 값) 컨셉을 간단히 반영)
    """
    if not ddl:
        return {}

    preferred_cols: List[str] = []
    for col in ddl.keys():
        low = col.lower()
        if low in {"name", "title", "rating", "first_name", "last_name"}:
            preferred_cols.append(col)
        elif low.endswith("_name"):
            preferred_cols.append(col)
        elif low in {"amount", "status"}:
            preferred_cols.append(col)

    preferred_cols = list(dict.fromkeys(preferred_cols))[:max_columns]
    if not preferred_cols:
        return {}

    sampled: Dict[str, List[Any]] = {}
    for col in preferred_cols:
        q = f"SELECT DISTINCT {col} AS value FROM {table_name} WHERE {col} IS NOT NULL LIMIT :limit;"
        try:
            rows = run_query(q, {"limit": max_values_per_column}, dvd=True)
            vals: List[Any] = []
            for r in rows:
                v = r.get("value")
                if isinstance(v, str) and len(v) > 60:
                    vals.append(v[:60] + "…")
                else:
                    vals.append(v)
            sampled[col] = vals
        except Exception:
            continue

    return sampled


def _build_schema_and_values_block(table_names: List[str]) -> Tuple[str, str]:
    """
    증강 프롬프트에 넣을 스키마(S)와 샘플값(V) 블록을 구성합니다.
    """
    ddl_by_table: Dict[str, Dict[str, Dict[str, Any]]] = {}
    values_by_table: Dict[str, Dict[str, List[Any]]] = {}

    for t in table_names:
        try:
            ddl = extract_ddl(t)
        except Exception:
            ddl = {}
        ddl_by_table[t] = ddl
        values_by_table[t] = _sample_distinct_values_for_table(t, ddl)

    schema_text = "\n\n".join([f"<TABLE {t}>\n{ddl_by_table[t]}\n</TABLE {t}>" for t in table_names]).strip()
    values_text = "\n\n".join([f"<TABLE {t}>\n{values_by_table[t]}\n</TABLE {t}>" for t in table_names]).strip()
    return schema_text, values_text


def _execution_filter(
    sql: str,
    *,
    statement_timeout_ms: int = 2000,
    max_rows: int = 20,
) -> Tuple[bool, Optional[int]]:
    """
    TEXT2SQL-FLOW의 SQL Execution Filter(실용 버전):
    - SELECT/WITH만 허용
    - statement_timeout 내 실행되는지 확인
    """
    cleaned = _strip_code_fences(sql)
    cleaned = re.sub(r";+\s*$", "", cleaned).strip()

    if not _is_safe_select_sql(cleaned):
        return False, None

    # 실행 검증용으로 LIMIT wrapper를 씌웁니다.
    wrapped = f"SELECT * FROM ({cleaned}) AS __q LIMIT :limit;"

    start = time.perf_counter()
    try:
        _ = run_query_with_statement_timeout(
            wrapped,
            params={"limit": max_rows},
            dvd=True,
            statement_timeout_ms=statement_timeout_ms,
        )
        ms = int((time.perf_counter() - start) * 1000)
        return True, ms
    except Exception:
        return False, None


# -----------------------------
# LLM: SQL 증강 / 질문 생성
# -----------------------------


def augment_sql_once(
    *,
    original_sql: str,
    direction: str,
    schema_text: str,
    values_text: str,
) -> str:
    """
    원본 SQL을 주어진 direction에 맞게 1회 증강합니다.
    """
    llm = get_llm()

    system = (
        "당신은 PostgreSQL 기반 Text-to-SQL 데이터 증강 전문가입니다.\n"
        "입력으로 주어진 스키마(S), 샘플 값(V), 원본 SQL을 바탕으로, 지시된 증강 방향에 맞는 새로운 SQL을 생성하세요.\n"
        "규칙:\n"
        "1) 반드시 SELECT 또는 WITH로 시작하는 쿼리만 생성(INSERT/UPDATE/DELETE/DDL 금지)\n"
        "2) 스키마에 존재하는 테이블/컬럼만 사용\n"
        "3) 의미적으로 타당해야 하며 실행 가능해야 함\n"
        "4) 오직 SQL만 출력(설명/마크다운 금지)\n"
    )

    user = (
        f"[AUGMENTATION DIRECTION]\n{direction}\n\n"
        f"[SCHEMA]\n{schema_text}\n\n"
        f"[SAMPLED VALUES]\n{values_text}\n\n"
        f"[ORIGINAL SQL]\n{original_sql}\n\n"
        "위 원본 SQL을 참고해, 증강 방향에 맞는 '새로운 SQL'을 한 개만 출력하세요."
    )

    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    return _strip_code_fences(getattr(resp, "content", "") or "")


def generate_questions_for_sql(
    *,
    sql: str,
    style_key: str,
) -> str:
    """
    주어진 SQL에 대한 한국어 질문 1개 생성(스타일 지정).
    """
    llm = get_llm()
    style_hint = QUESTION_STYLES.get(style_key, QUESTION_STYLES["formal"])

    system = (
        "당신은 데이터 분석가입니다.\n"
        "주어진 SQL과 의미가 일치하는 한국어 질문을 생성하세요.\n"
        "규칙:\n"
        "1) 질문은 한 문장(또는 짧은 2문장)으로 간결하게\n"
        "2) SQL의 결과를 정확히 묻도록 작성(테이블/컬럼명을 그대로 노출하지 않아도 됨)\n"
        "3) 출력은 질문 텍스트만(번호/불릿/설명 금지)\n"
    )
    user = (
        f"[SQL]\n{sql}\n\n"
        f"[STYLE]\n{style_key}: {style_hint}\n\n"
        "위 SQL과 의미적으로 일치하는 한국어 질문을 1개만 출력하세요."
    )

    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    return (getattr(resp, "content", "") or "").strip()


# -----------------------------
# CSV I/O
# -----------------------------


def load_seeds_from_csv(csv_path: Path) -> List[FlowSeed]:
    """
    시드 CSV 로드 (컬럼: question, sql)
    """
    seeds: List[FlowSeed] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "question" not in reader.fieldnames or "sql" not in reader.fieldnames:
            raise ValueError(f"시드 CSV 형식이 올바르지 않습니다: {csv_path} (columns={reader.fieldnames})")

        for i, row in enumerate(reader, 1):
            q = (row.get("question") or "").strip()
            s = (row.get("sql") or "").strip()
            if not q or not s:
                continue
            seeds.append(FlowSeed(seed_id=str(i), question=q, sql=s))

    return seeds


def write_augmented_rows(
    out_path: Path,
    rows: Iterable[FlowAugmented],
    *,
    append: bool = True,
) -> None:
    """
    증강 결과를 CSV로 저장합니다.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "seed_id",
        "direction",
        "augmented_sql",
        "question_style",
        "augmented_question",
        "exec_ok",
        "exec_ms",
    ]

    write_header = not out_path.exists() or not append
    mode = "a" if append else "w"

    with out_path.open(mode, encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "seed_id": r.seed_id,
                    "direction": r.direction,
                    "augmented_sql": r.augmented_sql,
                    "question_style": r.question_style,
                    "augmented_question": r.augmented_question,
                    "exec_ok": int(bool(r.exec_ok)),
                    "exec_ms": r.exec_ms if r.exec_ms is not None else "",
                }
            )


# -----------------------------
# 메인 파이프라인
# -----------------------------


def run_text2sql_flow_augmentation(
    *,
    seed_csv_path: Path,
    out_csv_path: Path,
    sql_aug_per_seed: int = 2,
    questions_per_sql: int = 2,
    statement_timeout_ms: int = 2000,
    max_rows: int = 20,
    directions: Optional[Sequence[str]] = None,
    style_keys: Optional[Sequence[str]] = None,
) -> None:
    """
    TEXT2SQL-FLOW 스타일 증강 파이프라인 실행(프로젝트용).
    """
    seeds = load_seeds_from_csv(seed_csv_path)
    log_step("TEXT2SQL-FLOW: 시드 로드", {"seed_csv": str(seed_csv_path), "seed_count": len(seeds)})

    if directions is None:
        directions = list(FLOW_DIRECTIONS.keys())
    if style_keys is None:
        style_keys = list(QUESTION_STYLES.keys())

    seen_sql: set[str] = set()

    for seed in seeds:
        base_sql = seed.sql
        table_names = _extract_table_names_from_sql(base_sql)
        if not table_names:
            # dvdrental 규모가 작아 전체 스키마를 넣어도 되지만, 우선은 최소 후보로 진행
            table_names = ["film", "actor", "customer", "rental", "payment", "inventory"]

        schema_text, values_text = _build_schema_and_values_block(table_names)

        log_step(
            "TEXT2SQL-FLOW: 시드 처리 시작",
            {"seed_id": seed.seed_id, "tables": table_names, "question": seed.question},
        )

        # SQL 증강
        for direction_key in directions:
            direction_inst = FLOW_DIRECTIONS.get(direction_key, direction_key)
            for _ in range(sql_aug_per_seed):
                augmented_sql = augment_sql_once(
                    original_sql=base_sql,
                    direction=direction_inst,
                    schema_text=schema_text,
                    values_text=values_text,
                )

                # 정리/중복 제거
                augmented_sql = _strip_code_fences(augmented_sql)
                norm = _normalize_sql_for_dedup(augmented_sql)
                if not norm or norm in seen_sql:
                    continue
                seen_sql.add(norm)

                # 실행 필터
                ok, ms = _execution_filter(
                    augmented_sql,
                    statement_timeout_ms=statement_timeout_ms,
                    max_rows=max_rows,
                )
                if not ok:
                    continue

                # 질문 생성(스타일 여러 개)
                produced: List[FlowAugmented] = []
                for style_key in style_keys[: max(1, questions_per_sql)]:
                    q = generate_questions_for_sql(sql=augmented_sql, style_key=style_key)
                    if not q:
                        continue
                    produced.append(
                        FlowAugmented(
                            seed_id=seed.seed_id,
                            direction=direction_key,
                            augmented_sql=augmented_sql,
                            question_style=style_key,
                            augmented_question=q,
                            exec_ok=ok,
                            exec_ms=ms,
                        )
                    )

                if produced:
                    write_augmented_rows(out_csv_path, produced, append=True)

    log_step("TEXT2SQL-FLOW: 증강 완료", {"out_csv": str(out_csv_path)})

