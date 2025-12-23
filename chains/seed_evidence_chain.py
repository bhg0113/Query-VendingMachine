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
from functools import lru_cache
from typing import Any, Dict, List, Optional

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

from config.llm_config import get_llm
from retrievers.seed_fewshot_retriever import get_seed_fewshot_examples
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


# SEED 비활성화 안내 로그가 과도하게 반복되지 않도록 1회만 출력합니다.
_LOGGED_SEED_DISABLED_ONCE = False


@dataclass(frozen=True)
class ProbeCandidate:
    """Sample SQL Execution을 위한 컬럼/값 후보"""

    table: str
    column: str
    value: str
    operator: str  # "equals" | "contains"


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


def _env_int(name: str, default: int) -> int:
    """환경변수 정수 파서"""
    try:
        return int((os.getenv(name, str(default)) or "").strip())
    except Exception:
        return default


def _env_str(name: str, default: str) -> str:
    """환경변수 문자열 파서"""
    v = os.getenv(name)
    return default if v is None else v


def _is_safe_identifier(s: str) -> bool:
    """
    동적 SQL에서 식별자(테이블/컬럼)로 사용해도 되는지 간단 검증.
    - information_schema에서 온 이름만 사용하도록 방어
    """
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s or ""))


@lru_cache(maxsize=1)
def _get_all_public_tables() -> List[str]:
    """dvdrental(public) 스키마의 모든 테이블 목록을 캐시해서 반환"""
    sql = """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
        ORDER BY table_name;
    """
    rows = run_query(sql, dvd=True)
    return [r["table_name"] for r in rows]


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


def _extract_value_candidates_from_question(question: str) -> List[str]:
    """
    질문에서 "실제 데이터 값" 후보를 추출합니다.

    목적:
    - sample sql execution은 '값'을 DB에서 확인하는 단계이므로,
      결과 컬럼명('staff_name', 'hour_of_day' 등) 같은 "라벨"은 제외하고
      실제 값(예: 'Horror', 'PG', 'Bergman' 등)만 대상으로 하는 것이 노이즈가 적습니다.

    휴리스틱:
    - 따옴표로 둘러싸인 문자열(ASCII/유니코드 따옴표)을 우선 추출
    - snake_case(언더스코어 포함)는 대개 결과 컬럼 라벨이므로 제외
    """
    if not question:
        return []

    patterns = [
        r"'([^']+)'",  # ASCII single quotes
        r"‘([^’]+)’",  # curly single quotes
        r'"([^"]+)"',  # ASCII double quotes
        r"“([^”]+)”",  # curly double quotes
    ]

    raw: List[str] = []
    for pat in patterns:
        raw.extend(re.findall(pat, question))

    cleaned: List[str] = []
    for v in raw:
        vv = str(v).strip()
        if not vv:
            continue
        # 결과 컬럼 라벨(대부분 snake_case)을 노이즈로 간주하고 제거
        if "_" in vv:
            continue
        # 너무 긴 값은 토큰 낭비라 제외
        if len(vv) > 80:
            continue
        cleaned.append(vv)

    # 중복 제거(대소문자 무시) + 입력 순서 유지
    seen: set[str] = set()
    out: List[str] = []
    for v in cleaned:
        k = v.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(v)

    return out


def _format_fewshot_examples(examples: List[Dict[str, Any]], max_examples: int = 5) -> str:
    """
    few-shot 예시를 evidence 생성 프롬프트에 넣기 위한 텍스트로 변환합니다.

    논문 SEED는 question 유사도(임베딩)로 예시를 선택해 프롬프트에 포함합니다.
    이 프로젝트는 question→SQL 페어를 제공해 SQL 패턴/조인 경로를 간접적으로 유도합니다.
    """
    if not examples:
        return "(none)"

    lines: List[str] = []
    for i, ex in enumerate(examples[:max_examples], 1):
        q = str(ex.get("question", "")).strip()
        s = str(ex.get("sql", "")).strip()
        lines.append(f"Example {i}")
        lines.append(f"- Question: {q}")
        lines.append(f"- SQL: {s}")
        if "distance" in ex:
            try:
                lines.append(f"- distance(cosine): {float(ex['distance']):.4f}")
            except Exception:
                pass
        lines.append("")

    return "\n".join(lines).strip()


def _sql_mentions_any_table(sql: str, tables: List[str]) -> bool:
    """
    SQL 문자열이 후보 테이블 중 하나라도 언급하는지(단순 휴리스틱) 검사합니다.
    - 너무 과한 예시(다른 도메인 패턴)가 섞이는 노이즈를 줄이기 위한 필터입니다.
    """
    if not sql or not tables:
        return False
    s = sql.lower()
    for t in tables:
        tt = (t or "").strip().lower()
        if not tt:
            continue
        # 단어 경계를 대략적으로 보장(알리아스/스키마 표기 등의 변형은 완벽히 커버하지 못함)
        if re.search(rf"\\b{re.escape(tt)}\\b", s):
            return True
    return False


def _select_fewshot_examples(
    examples: List[Dict[str, Any]],
    *,
    candidate_tables: List[str],
    min_k: int = 2,
    max_k: int = 8,
    distance_margin: float = 0.12,
) -> List[Dict[str, Any]]:
    """
    고정 K 대신, "질문에 필요한 것만" 남기도록 few-shot을 자동 선택합니다.

    선택 규칙(휴리스틱):
    1) 우선 top-N 검색 결과(examples)는 이미 distance 오름차순이라고 가정
    2) 가장 가까운 예시의 distance(d0)를 기준으로 (d0 + margin) 이하만 유지
    3) SQL이 후보 테이블을 전혀 언급하지 않으면(완전 무관) 제외
    4) 결과가 너무 적으면(min_k) 상위 min_k로 폴백
    5) 최대 max_k까지
    """
    if not examples:
        return []

    # distance가 없는 경우를 대비해 안전 처리
    try:
        d0 = float(examples[0].get("distance"))
    except Exception:
        d0 = None  # type: ignore[assignment]

    selected: List[Dict[str, Any]] = []
    for ex in examples:
        # 2) distance threshold
        if d0 is not None:
            try:
                d = float(ex.get("distance"))
                if d > d0 + distance_margin:
                    continue
            except Exception:
                # distance가 없으면 보수적으로 제외
                continue

        # 3) 후보 테이블 언급 여부(노이즈 감소)
        sql = str(ex.get("sql", "") or "")
        if candidate_tables and not _sql_mentions_any_table(sql, candidate_tables):
            continue

        selected.append(ex)
        if len(selected) >= max_k:
            break

    # 4) 너무 적으면 폴백(상위 min_k는 항상 포함)
    if len(selected) < min_k:
        return examples[: min(min_k, len(examples))]

    return selected


def _llm_extract_probe_candidates(
    question: str,
    ddl_by_table: Dict[str, Dict[str, Dict[str, Any]]],
    candidate_tables: List[str],
    *,
    max_candidates: int = 6,
    allowed_values: Optional[List[str]] = None,
) -> List[ProbeCandidate]:
    """
    질문에서 "컬럼-값 후보"를 뽑아 Sample SQL Execution에 사용할 후보를 만듭니다.

    논문 SEED는 질문에서 컬럼/값 키워드를 추출해 샘플 SQL을 실행합니다.
    이 프로젝트에서는 간단히 LLM으로 후보를 추출합니다.
    """
    llm = get_llm()

    # 스키마를 아주 컴팩트하게 요약(테이블+컬럼명만)
    schema_lines: List[str] = []
    for t in candidate_tables:
        cols = list(ddl_by_table.get(t, {}).keys())
        schema_lines.append(f"- {t}: {', '.join(cols[:40])}{'...' if len(cols) > 40 else ''}")

    system_prompt = (
        "당신은 PostgreSQL 데이터베이스 스키마를 보고, 질문에서 샘플 SQL로 확인할 만한 "
        "'컬럼-값 후보'를 추출하는 도우미입니다.\n"
        "가능한 경우에만 후보를 제시하고, 추정이 강한 후보는 제외하세요.\n"
        "반드시 JSON 오브젝트만 출력하세요."
    )

    allowed_block = ""
    if allowed_values:
        allowed_block = (
            "[ALLOWED VALUES]\n"
            + _safe_json_dumps(allowed_values)
            + "\n\n"
            "제약: value는 반드시 ALLOWED VALUES 중 하나를 그대로 사용하세요(새 값 생성 금지).\n\n"
        )

    user_prompt = (
        f"[QUESTION]\n{question}\n\n"
        f"{allowed_block}"
        f"[CANDIDATE TABLES]\n{', '.join(candidate_tables)}\n\n"
        f"[SCHEMA COLUMNS]\n{chr(10).join(schema_lines)}\n\n"
        "다음 JSON 형식으로 답하세요:\n"
        "{\n"
        '  "candidates": [\n'
        '    {"table": "table_name", "column": "column_name", "value": "value_string", "operator": "equals|contains"}\n'
        "  ]\n"
        "}\n"
        f"제약:\n- candidates는 최대 {max_candidates}개\n- operator는 equals 또는 contains 중 하나\n"
    )

    resp = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
    parsed = _extract_first_json_object(getattr(resp, "content", "") or "")
    if not parsed:
        return []

    raw = parsed.get("candidates") or []
    if not isinstance(raw, list):
        return []

    allowed_lower = {v.lower() for v in (allowed_values or []) if v}
    q_lower = question.lower()

    out: List[ProbeCandidate] = []
    for item in raw[:max_candidates]:
        if not isinstance(item, dict):
            continue
        t = str(item.get("table", "")).strip()
        c = str(item.get("column", "")).strip()
        v = str(item.get("value", "")).strip()
        op = str(item.get("operator", "")).strip().lower()
        if not t or not c or not v:
            continue
        if op not in {"equals", "contains"}:
            continue

        # 값 후보는 "질문에 실제로 등장한 값"으로 제한(노이즈 최소화)
        if allowed_values:
            if v.lower() not in allowed_lower:
                continue
        else:
            # allowed_values가 없을 때는 질문 문자열 안에 등장한 값만 허용
            if v.lower() not in q_lower:
                continue

        if t not in ddl_by_table:
            continue
        if c not in ddl_by_table.get(t, {}):
            continue
        if not _is_safe_identifier(t) or not _is_safe_identifier(c):
            continue
        out.append(ProbeCandidate(table=t, column=c, value=v, operator=op))

    return out


def _run_sample_sql_execution(
    candidates: List[ProbeCandidate],
    *,
    limit_per_candidate: int = 8,
) -> List[Dict[str, Any]]:
    """
    Sample SQL Execution 단계(간단 버전):
    - 후보 (table, column, value)에 대해 실제 DB에서 매칭되는 값을 조회하여 evidence 생성에 활용합니다.
    """
    results: List[Dict[str, Any]] = []
    for cand in candidates:
        # 식별자 안전 체크(방어)
        if not _is_safe_identifier(cand.table) or not _is_safe_identifier(cand.column):
            continue

        # "필요한 값들만" 가져오도록, 질문에서 추출한 candidate_value를 중심으로 조회합니다.
        # - equals: 텍스트 캐스팅 + 대소문자 무시 비교로 1차 확인 → 없으면 ILIKE로 유사값 확인
        # - contains: ILIKE로 부분 매칭
        values: List[Any] = []

        if cand.operator == "equals":
            q_eq = f"""
                SELECT DISTINCT {cand.column} AS value
                FROM {cand.table}
                WHERE LOWER(CAST({cand.column} AS TEXT)) = LOWER(:val)
                LIMIT :limit;
            """
            try:
                rows = run_query(q_eq, {"val": cand.value, "limit": limit_per_candidate}, dvd=True)
                values = [r["value"] for r in rows]
            except Exception:
                values = []

            # exact match가 없으면 유사값(부분 매칭)만 추가로 탐색
            if not values:
                q_like = f"""
                    SELECT DISTINCT {cand.column} AS value
                    FROM {cand.table}
                    WHERE CAST({cand.column} AS TEXT) ILIKE :pat
                    LIMIT :limit;
                """
                try:
                    rows = run_query(q_like, {"pat": f"%{cand.value}%", "limit": limit_per_candidate}, dvd=True)
                    values = [r["value"] for r in rows]
                except Exception:
                    values = []
        else:
            q_like = f"""
                SELECT DISTINCT {cand.column} AS value
                FROM {cand.table}
                WHERE CAST({cand.column} AS TEXT) ILIKE :pat
                LIMIT :limit;
            """
            try:
                rows = run_query(q_like, {"pat": f"%{cand.value}%", "limit": limit_per_candidate}, dvd=True)
                values = [r["value"] for r in rows]
            except Exception:
                values = []

        # 문자열 길이 제한(토큰 절약)
        clipped: List[Any] = []
        for v in values:
            if isinstance(v, str) and len(v) > 80:
                clipped.append(v[:80] + "…")
            else:
                clipped.append(v)

        # 매칭된 값이 없으면 evidence 노이즈이므로 제외
        if not clipped:
            continue

        results.append(
            {
                "table": cand.table,
                "column": cand.column,
                "candidate_value": cand.value,
                "operator": cand.operator,
                "matched_values": clipped,
            }
        )

    return results


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
    schema_tables: List[str],
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
    for t in schema_tables:
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
    max_tables: int = 10,
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
        # 사용자가 "왜 evidence가 비어있지?" 혼동하지 않도록 1회만 안내 로그를 남깁니다.
        global _LOGGED_SEED_DISABLED_ONCE
        if not _LOGGED_SEED_DISABLED_ONCE:
            _LOGGED_SEED_DISABLED_ONCE = True
            log_step(
                "SEED: evidence 비활성화 상태(기본값)",
                {
                    "ENABLE_SEED_EVIDENCE": os.getenv("ENABLE_SEED_EVIDENCE", ""),
                    "안내": "evidence를 생성하려면 ENABLE_SEED_EVIDENCE=1 로 설정하세요.",
                },
            )
        return SeedEvidenceResult(
            evidence="",
            clarified_question=question,
            candidate_tables=candidate_tables,
            join_hints=[],
            sampled_values={},
        )

    # 모드 정리:
    # - lite: 초기 SEED-lite(리트리버 기반 테이블 + FK 힌트 + 대표값 샘플 + evidence 생성)
    # - full: 논문에 최대한 근접(가능한 전체 스키마 + few-shot + sample sql execution + evidence 생성)
    seed_mode = (_env_str("SEED_MODE", "lite") or "lite").strip().lower()
    if seed_mode not in {"lite", "full"}:
        seed_mode = "lite"

    # full 모드에서만 활성화되는 기능들(논문 구성요소에 근접)
    enable_fewshot = seed_mode == "full"
    enable_sample_exec = seed_mode == "full"

    # 스키마 입력 크기
    schema_mode = "full" if seed_mode == "full" else "retriever"
    schema_tables = candidate_tables
    if seed_mode == "full":
        try:
            schema_tables = _get_all_public_tables()
        except Exception as e:
            # DB 상태/권한 등에 따라 정보 스키마 조회가 실패할 수 있어 폴백합니다.
            log_step(
                "SEED(full): 전체 스키마 로드 실패(lite 방식으로 폴백)",
                {"에러": f"{type(e).__name__}: {e}"},
            )
            seed_mode = "lite"
            enable_fewshot = False
            enable_sample_exec = False
            schema_mode = "retriever"
            schema_tables = candidate_tables

    log_step(
        "SEED: evidence 생성 시작",
        {
            "테이블_후보(retriever)": candidate_tables,
            "SEED_MODE": seed_mode,
            "스키마_모드": schema_mode,
            "스키마_테이블_수": len(schema_tables),
            "fewshot": enable_fewshot,
            "sample_exec": enable_sample_exec,
            "질문": question,
        },
    )

    # 1) 스키마(DDL) 수집
    ddl_by_table: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for t in schema_tables:
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
        schema_tables=schema_tables,
        join_hints=join_hints,
        ddl_by_table=ddl_by_table,
        sampled_values=sampled_values,
    )

    # 3-1) "description files" 대체: table_docs(Document.page_content)를 함께 제공 (논문 입력 구성요소에 근접)
    # - lite 모드에서는 초기 구현과 동일하게 제외합니다.
    if seed_mode == "full" and docs:
        table_docs_text = "\n\n".join(
            [
                f"<TableDoc {d.metadata.get('table_name','')}>\\n{d.page_content}\\n</TableDoc>"
                for d in docs
            ]
        )
        seed_context += "\n\n[DESCRIPTION DOCS (table_docs)]\n" + table_docs_text

    # 3-2) few-shot 예시 선택(임베딩 유사도 기반)
    if enable_fewshot:
        # 고정 K 대신, top-N을 넓게 가져온 뒤(노이즈 줄이는 필터로) 자동 선택합니다.
        pool_k = 20
        try:
            fewshot_pool = get_seed_fewshot_examples(question, limit=pool_k)
        except Exception as e:
            log_step("SEED few-shot: 예시 검색 실패(무시)", {"에러": f"{type(e).__name__}: {e}"})
            fewshot_pool = []

        fewshot = _select_fewshot_examples(
            fewshot_pool,
            candidate_tables=candidate_tables,
            min_k=2,
            max_k=8,
            distance_margin=0.12,
        )
        log_step(
            "SEED few-shot: 예시 선택",
            {
                "pool_size": len(fewshot_pool),
                "selected_size": len(fewshot),
                "선택_기준": "d0+0.12 이내 + 후보테이블 언급 + 최대 8개(최소 2개 폴백)",
            },
        )
        seed_context += "\n\n[FEW-SHOT EXAMPLES (similar questions)]\n" + _format_fewshot_examples(fewshot, max_examples=len(fewshot) or 0)

    # 3-3) Sample SQL Execution (질문 기반 컬럼/값 후보 → 실제 DB 조회)
    probe_results: List[Dict[str, Any]] = []
    # - SEEDgpt(full) 쪽으로 갈수록 "전체 스키마"에서 후보를 찾는 편이 자연스럽습니다.
    # - SEEDdeepseek(lite) 쪽은 컨텍스트 제약 때문에 관련 테이블(리트리버 결과)로 범위를 좁힙니다.
    probe_table_scope = schema_tables if seed_mode == "full" else candidate_tables
    if enable_sample_exec and probe_table_scope:
        # "개수 제한(max_probe)" 대신, 질문에서 추출된 실제 값만 대상으로 후보를 만들고,
        # DB에서 매칭 결과가 있는 항목만 evidence에 포함해 노이즈를 줄입니다.
        allowed_values = _extract_value_candidates_from_question(question)
        if not allowed_values:
            log_step(
                "SEED sample-exec: 값 후보 없음(생략)",
                {"안내": "질문에 실제 값(예: 'Horror', 'PG')이 명시되지 않아 sample exec를 건너뜁니다."},
            )
            probes = []
        else:
            max_probe = max(1, min(30, len(allowed_values) * 5))
            log_step(
                "SEED sample-exec: 값 후보 추출",
                {"values": allowed_values, "max_probe_candidates": max_probe},
            )
        try:
            probes = _llm_extract_probe_candidates(
                question=question,
                ddl_by_table=ddl_by_table,
                candidate_tables=probe_table_scope,
                max_candidates=max_probe,
                allowed_values=allowed_values,
            )
        except Exception as e:
            log_step("SEED sample-exec: 후보 추출 실패(무시)", {"에러": f"{type(e).__name__}: {e}"})
            probes = []
        if probes:
            log_step("SEED sample-exec: 후보 추출 완료", {"후보_개수": len(probes), "후보": [p.__dict__ for p in probes]})
            try:
                probe_results = _run_sample_sql_execution(probes, limit_per_candidate=8)
            except Exception as e:
                log_step("SEED sample-exec: 샘플 SQL 실행 실패(무시)", {"에러": f"{type(e).__name__}: {e}"})
                probe_results = []
        else:
            log_step("SEED sample-exec: 후보 추출 결과 없음", {"안내": "질문에서 값/컬럼 후보를 확신하기 어려운 경우일 수 있습니다."})

    if probe_results:
        seed_context += "\n\n[SAMPLE SQL EXECUTION RESULTS]\n" + _safe_json_dumps(probe_results)

    # 4) LLM으로 evidence 생성 (JSON 출력 강제)
    llm = get_llm()
    system_prompt = (
        "당신은 Text-to-SQL을 돕는 '증거(evidence)'를 작성하는 전문가입니다.\n"
        "주어진 질문과 스키마/조인 힌트/샘플값을 바탕으로 SQL 생성에 필요한 핵심 정보만 정리하세요.\n"
        "추가로 few-shot 예시와 sample sql 실행 결과가 제공되면, 이를 근거로 값/컬럼 매핑을 더 정확히 하세요.\n"
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


