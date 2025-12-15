"""
SEED(Automatic Evidence Generation) 스타일 evidence 생성 모듈

이 프로젝트에서는 논문 SEED의 전체 파이프라인을 그대로 복제하기보다,
현재 RAG(Text2SQL) 체인에 쉽게 얹을 수 있는 "SEED-lite" 형태로 구현합니다.

핵심 아이디어:
- 질문에 대해 관련 테이블 후보를 선정(retriever 결과 활용)
- 해당 테이블들의 스키마(DDL), 외래키(조인 힌트), 대표적인 값(샘플/유니크 값)을 수집
- LLM이 위 정보를 바탕으로 SQL 생성을 돕는 evidence와 clarified question을 JSON으로 생성
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

from config.llm_config import get_llm
from utils.db_utils import run_query, extract_ddl
from utils.logging_utils import log_step


@dataclass(frozen=True)
class SeedEvidenceResult:
    """SEED evidence 생성 결과"""

    evidence: str
    clarified_question: str
    candidate_tables: List[str]
    join_hints: List[str]
    sampled_values: Dict[str, Dict[str, List[Any]]]


def _safe_json_dumps(obj: Any) -> str:
    """
    Decimal/날짜 등 JSON 직렬화가 어려운 타입이 섞여 있어도
    프롬프트 컨텍스트 생성이 실패하지 않도록 안전하게 덤프합니다.
    """
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _env_flag(name: str, default: str = "0") -> bool:
    """환경변수 플래그(ON/OFF) 파서"""
    v = (os.getenv(name, default) or "").strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def _strip_code_fences(text: str) -> str:
    """```json ... ``` 같은 코드펜스 제거"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _extract_first_json_object(text: str) -> Optional[dict]:
    """
    LLM 출력에서 첫 번째 JSON object를 최대한 복구해서 파싱합니다.
    - 코드펜스 제거
    - 앞/뒤 불필요 텍스트가 섞여 있어도 { ... } 블록을 추출 시도
    """
    cleaned = _strip_code_fences(text)

    # 1) 전체가 JSON일 가능성
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # 2) 가장 바깥 { ... } 범위를 찾기 (단순 휴리스틱)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    candidate = cleaned[start : end + 1]
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None

    return None


def _get_foreign_key_hints(table_names: List[str]) -> List[str]:
    """
    후보 테이블들 사이의 외래키 관계를 조회해서 조인 힌트 문자열로 반환합니다.

    Returns:
        List[str]: 예) "film_actor.film_id -> film.film_id"
    """
    if not table_names:
        return []

    # dvdrental(public 스키마)에서 FK 전체를 가져온 뒤, 후보 테이블만 필터링합니다.
    sql = """
        SELECT
            tc.constraint_name,
            tc.table_name AS source_table,
            kcu.column_name AS source_column,
            ccu.table_name AS target_table,
            ccu.column_name AS target_column
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage AS ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public';
    """
    rows = run_query(sql, dvd=True)
    table_set = set(table_names)

    hints: List[str] = []
    for r in rows:
        src_t = r["source_table"]
        tgt_t = r["target_table"]
        if src_t in table_set and tgt_t in table_set:
            hints.append(f"{src_t}.{r['source_column']} -> {tgt_t}.{r['target_column']}")

    # 중복 제거 + 안정적 정렬
    return sorted(list(dict.fromkeys(hints)))


def _sample_distinct_values_for_table(
    table_name: str,
    ddl: Dict[str, Dict[str, Any]],
    *,
    max_columns: int = 6,
    max_values_per_column: int = 12,
) -> Dict[str, List[Any]]:
    """
    테이블에서 "사람이 자주 조건으로 쓰는 컬럼"에 대해 대표 값(유니크 값)을 샘플링합니다.

    SEED 논문은 질문에서 값 후보를 뽑아 샘플 SQL을 실행하지만,
    여기서는 비용/복잡도를 줄이기 위해 간단한 휴리스틱을 사용합니다.
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

    # 후보가 너무 많으면 앞부분만
    preferred_cols = list(dict.fromkeys(preferred_cols))[:max_columns]
    if not preferred_cols:
        return {}

    sampled: Dict[str, List[Any]] = {}
    for col in preferred_cols:
        # DISTINCT는 비용이 있을 수 있으니 max_values_per_column을 작게 유지
        q = f"SELECT DISTINCT {col} AS value FROM {table_name} WHERE {col} IS NOT NULL LIMIT {max_values_per_column};"
        try:
            rows = run_query(q, dvd=True)
            values = [r["value"] for r in rows]
            # 너무 긴 문자열은 잘라서 토큰 낭비 방지
            clipped: List[Any] = []
            for v in values:
                if isinstance(v, str) and len(v) > 60:
                    clipped.append(v[:60] + "…")
                else:
                    clipped.append(v)
            sampled[col] = clipped
        except Exception:
            # 샘플링 실패는 무시 (e.g., 권한/타입 이슈)
            continue

    return sampled


def _build_seed_context(
    question: str,
    candidate_tables: List[str],
    join_hints: List[str],
    ddl_by_table: Dict[str, Dict[str, Dict[str, Any]]],
    sampled_values: Dict[str, Dict[str, List[Any]]],
) -> str:
    """
    LLM에 넣을 SEED 컨텍스트 텍스트 구성
    (스키마 + 조인 힌트 + 샘플값)
    """
    parts: List[str] = []
    parts.append(f"[QUESTION]\n{question}\n")

    parts.append("[CANDIDATE TABLES]")
    parts.append(", ".join(candidate_tables) if candidate_tables else "(none)")
    parts.append("")

    parts.append("[JOIN HINTS (FOREIGN KEYS)]")
    if join_hints:
        parts.extend([f"- {h}" for h in join_hints])
    else:
        parts.append("(none)")
    parts.append("")

    parts.append("[SCHEMA (DDL)]")
    for t in candidate_tables:
        ddl = ddl_by_table.get(t, {})
        parts.append(f"<TABLE {t}>")
        parts.append(_safe_json_dumps(ddl))
        parts.append(f"</TABLE {t}>")
        parts.append("")

    parts.append("[SAMPLED VALUES]")
    for t in candidate_tables:
        vals = sampled_values.get(t, {})
        parts.append(f"<TABLE {t}>")
        parts.append(_safe_json_dumps(vals))
        parts.append(f"</TABLE {t}>")
        parts.append("")

    return "\n".join(parts).strip()


def generate_seed_evidence(
    question: str,
    docs: List[Document],
    *,
    enable: Optional[bool] = None,
    max_tables: int = 6,
) -> SeedEvidenceResult:
    """
    SEED-lite evidence 생성 메인 함수

    Args:
        question: 사용자 자연어 질문
        docs: retriever 결과 Document 리스트 (metadata['table_name'] 포함)
        enable: evidence 생성 활성화 여부 (None이면 ENV로 제어)
        max_tables: evidence 생성에 포함할 최대 테이블 수

    Returns:
        SeedEvidenceResult: evidence/clarified_question + 디버그용 부가정보
    """
    if enable is None:
        enable = _env_flag("ENABLE_SEED_EVIDENCE", "0")

    candidate_tables = [d.metadata.get("table_name", "") for d in docs if d.metadata.get("table_name")]
    candidate_tables = [t for t in candidate_tables if t]  # 빈 값 제거
    candidate_tables = list(dict.fromkeys(candidate_tables))[:max_tables]

    if not enable:
        return SeedEvidenceResult(
            evidence="",
            clarified_question=question,
            candidate_tables=candidate_tables,
            join_hints=[],
            sampled_values={},
        )

    log_step("SEED: evidence 생성 시작", {"테이블_후보": candidate_tables, "질문": question})

    # 1) 스키마(DDL) 수집
    ddl_by_table: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for t in candidate_tables:
        try:
            ddl_by_table[t] = extract_ddl(t)
        except Exception:
            ddl_by_table[t] = {}

    # 2) 조인 힌트(외래키) 수집
    join_hints = _get_foreign_key_hints(candidate_tables)

    # 3) 샘플 값 수집(휴리스틱)
    sampled_values: Dict[str, Dict[str, List[Any]]] = {}
    for t in candidate_tables:
        sampled_values[t] = _sample_distinct_values_for_table(t, ddl_by_table.get(t, {}))

    seed_context = _build_seed_context(
        question=question,
        candidate_tables=candidate_tables,
        join_hints=join_hints,
        ddl_by_table=ddl_by_table,
        sampled_values=sampled_values,
    )

    # 4) LLM으로 evidence 생성 (JSON 출력 강제)
    llm = get_llm()
    system_prompt = (
        "당신은 Text-to-SQL을 돕는 '증거(evidence)'를 작성하는 전문가입니다.\n"
        "주어진 질문과 스키마/조인 힌트/샘플값을 바탕으로 SQL 생성에 필요한 핵심 정보만 정리하세요.\n"
        "불필요한 장황한 설명은 제외하고, 틀린 추정은 하지 마세요.\n"
        "반드시 JSON 오브젝트만 출력하세요."
    )
    user_prompt = (
        f"{seed_context}\n\n"
        "아래 형식의 JSON으로만 답변하세요:\n"
        "{\n"
        '  "clarified_question": "SQL 생성을 위해 더 명확하게 재작성한 질문(한 줄)",\n'
        '  "evidence": "SQL 작성에 도움되는 근거/매핑/조인/필터 힌트를 한국어로 간결하게(여러 줄 가능)"\n'
        "}\n"
    )

    resp = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
    parsed = _extract_first_json_object(getattr(resp, "content", "") or "")

    if not parsed:
        # 실패 시 최소한의 fallback
        log_step("SEED: evidence JSON 파싱 실패(폴백 사용)", {"원본_응답_앞부분": (resp.content or "")[:500]})
        return SeedEvidenceResult(
            evidence="(evidence 생성 실패: 스키마/샘플값은 있으나 JSON 파싱에 실패했습니다)",
            clarified_question=question,
            candidate_tables=candidate_tables,
            join_hints=join_hints,
            sampled_values=sampled_values,
        )

    evidence = str(parsed.get("evidence", "") or "").strip()
    clarified = str(parsed.get("clarified_question", "") or "").strip() or question

    # 로깅(너무 길면 자르기)
    log_step(
        "SEED: evidence 생성 완료",
        {
            "clarified_question": clarified,
            "evidence_미리보기": evidence[:500] + ("…" if len(evidence) > 500 else ""),
            "조인_힌트_개수": len(join_hints),
        },
    )

    return SeedEvidenceResult(
        evidence=evidence,
        clarified_question=clarified,
        candidate_tables=candidate_tables,
        join_hints=join_hints,
        sampled_values=sampled_values,
    )


