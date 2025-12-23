"""
Phoenix Experiments 러너

1) Phoenix Dataset을 불러오고
2) Text2SQL 태스크를 돌린 뒤
3) 실행결과 동치(Execution-match) evaluator로 점수화하여
   Phoenix의 Datasets & Experiments 화면에서 비교/분석할 수 있게 합니다.

사전 조건:
- Phoenix 서버가 떠 있어야 합니다. (기본: http://localhost:6006)
- 본 프로젝트 DB(dvdrental)가 접근 가능해야 합니다. (도커면 DB_HOST=db, DB_PORT=5432)

사용 예시(로컬):
  python experiments/phoenix_upload_datasets.py --all
  python experiments/phoenix_run_experiment.py --dataset dvdrental-basic --name "baseline-seed-off"

사용 예시(도커):
  docker compose exec web python experiments/phoenix_upload_datasets.py --all
  docker compose exec web python experiments/phoenix_run_experiment.py --dataset dvdrental-basic --name "baseline-seed-off"
"""

from __future__ import annotations

import argparse
import inspect
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


# 루트 경로를 파이썬 모듈 검색 경로에 추가 (어느 위치에서 실행해도 동작하도록)
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))


from chains.text_to_sql_chain import invoke_text_to_sql_chain
from experiments.phoenix_execution_match import execution_match_evaluator
from experiments.phoenix_upload_datasets import DEFAULT_DATASETS, upload_dataset


def _phoenix_base_url() -> str:
    return (
        os.getenv("PHOENIX_BASE_URL")
        or os.getenv("PHOENIX_SERVER_URL")
        or "http://localhost:6006"
    )


def _get_client():
    from phoenix.client import Client

    base_url = _phoenix_base_url()
    # 일부 버전은 Client()가 PHOENIX_BASE_URL을 참조하므로, 안전하게 주입합니다.
    os.environ["PHOENIX_BASE_URL"] = base_url
    # 일부 버전은 host/port 조합을 참조할 수 있어 같이 주입합니다.
    try:
        host_port = base_url.replace("http://", "").replace("https://", "").split("/")[0]
        if ":" in host_port:
            host, port = host_port.split(":", 1)
            os.environ.setdefault("PHOENIX_HOST", host)
            os.environ.setdefault("PHOENIX_PORT", port)
    except Exception:
        pass

    try:
        params = inspect.signature(Client).parameters
    except Exception:
        return Client()

    if "base_url" in params:
        return Client(base_url=base_url)
    if "endpoint" in params:
        return Client(endpoint=base_url)

    return Client()


def _get_dataset(client: Any, dataset_name: str) -> Any:
    """Client에서 dataset을 이름으로 조회(버전별 시그니처 차이 흡수)."""
    def _call_single_arg(fn, value: str, preferred_keys: list[str]) -> Any:
        # 1) 흔한 키워드 우선 시도
        for key in preferred_keys:
            try:
                return fn(**{key: value})
            except TypeError:
                continue

        # 2) signature 기반 자동 추론 (required param 1개를 찾아 넣어봄)
        try:
            params = list(inspect.signature(fn).parameters.values())
        except Exception:
            params = []
        params = [p for p in params if p.name not in ("self",)]
        required = [
            p
            for p in params
            if p.default is inspect._empty
            and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        ]
        for p in required[:1]:
            try:
                return fn(**{p.name: value})
            except TypeError:
                pass

        # 3) positional
        try:
            return fn(value)
        except TypeError:
            return None

    def _try_get(fn):
        return _call_single_arg(fn, dataset_name, ["name", "dataset_name", "datasetName"])

    def _coerce_list(x: Any) -> list[Any]:
        if x is None:
            return []
        if isinstance(x, list):
            return x
        if isinstance(x, tuple):
            return list(x)
        if isinstance(x, dict) and "data" in x:
            try:
                return list(x["data"])
            except Exception:
                return []
        if hasattr(x, "data"):
            try:
                return list(getattr(x, "data"))
            except Exception:
                return []
        try:
            return list(x)
        except Exception:
            return []

    def _call_list(fn):
        try:
            params = inspect.signature(fn).parameters
        except Exception:
            params = {}

        candidates = []
        # name 필터가 가능하면 dataset_name으로 좁혀서 받습니다.
        base_kwargs = {"name": dataset_name} if "name" in params else {}
        if any(k in params for k in ("limit", "page_size", "pageSize", "page_limit", "pageLimit")):
            candidates.append(
                {
                    **base_kwargs,
                    **{k: 2000 for k in ("limit", "page_size", "pageSize", "page_limit", "pageLimit") if k in params},
                }
            )
        candidates.append(base_kwargs)

        for kwargs in candidates:
            try:
                return fn(**kwargs)
            except TypeError:
                continue
        return fn()

    def _list_datasets() -> list[Any]:
        ds_res = getattr(client, "datasets", None)
        if ds_res is None:
            return []

        for meth in ("list", "list_datasets"):
            if not hasattr(ds_res, meth):
                continue
            fn = getattr(ds_res, meth)
            try:
                return _coerce_list(_call_list(fn))
            except Exception:
                continue
        return []

    def _get_name(obj: Any) -> str:
        # list API가 {"dataset": {...}} 형태를 돌려주는 경우 대응
        if isinstance(obj, Mapping) and isinstance(obj.get("dataset"), Mapping):
            obj = obj["dataset"]
        elif hasattr(obj, "dataset") and getattr(obj, "dataset") is not None:
            obj = getattr(obj, "dataset")

        if isinstance(obj, Mapping):
            return str(
                obj.get("name")
                or obj.get("dataset_name")
                or obj.get("datasetName")
                or obj.get("display_name")
                or obj.get("displayName")
                or ""
            )
        return str(
            getattr(obj, "name", "")
            or getattr(obj, "dataset_name", "")
            or getattr(obj, "datasetName", "")
            or getattr(obj, "display_name", "")
            or getattr(obj, "displayName", "")
            or ""
        )

    def _get_id(obj: Any) -> str:
        # list API가 {"dataset": {...}} 형태를 돌려주는 경우 대응
        if isinstance(obj, Mapping) and isinstance(obj.get("dataset"), Mapping):
            obj = obj["dataset"]
        elif hasattr(obj, "dataset") and getattr(obj, "dataset") is not None:
            obj = getattr(obj, "dataset")

        if isinstance(obj, Mapping):
            return str(
                obj.get("id")
                or obj.get("dataset_id")
                or obj.get("datasetId")
                or obj.get("uuid")
                or obj.get("rowid")
                or obj.get("dataset_rowid")
                or ""
            )
        return str(
            getattr(obj, "id", "")
            or getattr(obj, "dataset_id", "")
            or getattr(obj, "datasetId", "")
            or getattr(obj, "uuid", "")
            or getattr(obj, "rowid", "")
            or getattr(obj, "dataset_rowid", "")
            or ""
        )

    def _http_find_dataset_id() -> str:
        """
        Client 목록 API가 버전 차이로 실패하는 경우를 대비해,
        Phoenix 서버 REST API로 dataset id를 직접 조회합니다.
        """
        try:
            import httpx
        except Exception:
            return ""

        base_url = _phoenix_base_url().rstrip("/")
        candidates = [
            f"{base_url}/v1/datasets",
            f"{base_url}/api/v1/datasets",
        ]

        def _pick_id(item: Any) -> str:
            if isinstance(item, Mapping):
                for k in ("id", "dataset_id", "datasetId", "uuid", "rowid", "dataset_rowid"):
                    v = item.get(k)
                    if v is not None and str(v).strip():
                        return str(v)
            return ""

        def _pick_name(item: Any) -> str:
            if isinstance(item, Mapping):
                for k in ("name", "dataset_name", "datasetName", "display_name", "displayName"):
                    v = item.get(k)
                    if v is not None:
                        return str(v).strip()
            return ""

        for url in candidates:
            try:
                # name 필터가 지원되면 가장 확실합니다.
                for params in (
                    {"name": dataset_name, "limit": 50},
                    {"limit": 2000},
                    {},
                ):
                    r = httpx.get(url, params=params, timeout=5.0)
                    if r.status_code != 200:
                        continue
                    try:
                        data = r.json()
                    except Exception:
                        continue
                    break
                else:
                    continue
                if r.status_code != 200:
                    continue
            except Exception:
                continue

            items = (
                data.get("data")
                or data.get("datasets")
                or data.get("items")
                or []
            )
            if isinstance(items, dict) and "data" in items:
                items = items["data"]
            if not isinstance(items, list):
                continue

            for it in items:
                if isinstance(it, Mapping) and isinstance(it.get("dataset"), Mapping):
                    it = it["dataset"]
                name = _pick_name(it)
                if name == dataset_name:
                    return _pick_id(it)

        return ""

    # 후보 함수 수집
    candidates: list[Any] = []
    if hasattr(client, "get_dataset"):
        candidates.append(getattr(client, "get_dataset"))
    if hasattr(client, "datasets") and hasattr(client.datasets, "get_dataset"):
        candidates.append(getattr(client.datasets, "get_dataset"))

    if not candidates:
        raise RuntimeError("Phoenix Client에서 dataset 조회 API를 찾지 못했습니다.")

    # 1차: 이름으로 직접 조회
    for fn in candidates:
        ds = _try_get(fn)
        if ds is not None:
            return ds

    # 2차: list → id 확보 → id로 재조회 (일부 버전은 name 조회가 없을 수 있음)
    for item in _list_datasets():
        if _get_name(item).strip() != dataset_name.strip():
            continue
        ds_id = _get_id(item)
        if not ds_id:
            continue
        for fn in candidates:
            ds = _call_single_arg(fn, ds_id, ["dataset_id", "id", "uuid", "rowid", "dataset_rowid"])
            if ds is not None:
                return ds

    # 3차: REST API로 id를 직접 조회 후 재조회
    ds_id = _http_find_dataset_id()
    if ds_id:
        for fn in candidates:
            ds = _call_single_arg(fn, ds_id, ["dataset_id", "id", "uuid", "rowid", "dataset_rowid"])
            if ds is not None:
                return ds

    raise RuntimeError(f"dataset '{dataset_name}'을(를) 찾지 못했습니다.")


def _extract_question(example: Any) -> str:
    """
    run_experiment에서 example이 dict로 들어오는 경우가 많지만,
    버전/환경에 따라 객체 형태일 수도 있어 방어적으로 처리합니다.
    """
    if isinstance(example, Mapping):
        return str(example.get("question", "") or "")

    # pydantic/dataclass 류
    for attr in ("question",):
        if hasattr(example, attr):
            return str(getattr(example, attr) or "")

    # example.input 같은 구조 대응
    if hasattr(example, "input"):
        inp = getattr(example, "input")
        if isinstance(inp, Mapping):
            return str(inp.get("question", "") or "")

    return ""


def main():
    parser = argparse.ArgumentParser(description="Phoenix Experiments 실행 도구(Text2SQL)")
    parser.add_argument(
        "--dataset",
        default="dvdrental-basic",
        help="Phoenix에 업로드된 dataset 이름 (기본: dvdrental-basic)",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="dataset이 없을 때 업로드에 사용할 CSV 경로(선택)",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Phoenix에 기록될 experiment 이름(미지정 시 자동 생성)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="동시 실행 수 (기본 1, OpenAI 레이트리밋/비용 고려)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Phoenix에 기록하지 않고 샘플 일부만 실행(dry-run)",
    )
    args = parser.parse_args()

    client = _get_client()

    # dataset 확보 (없으면 업로드)
    try:
        dataset = _get_dataset(client, args.dataset)
    except Exception as e:
        # dataset을 못 찾으면(또는 API 시그니처 차이로 조회 실패하면) 기본 CSV 매핑으로 업로드를 시도합니다.
        default_csv_map = {name: path for name, path in DEFAULT_DATASETS}
        csv_path = args.csv or default_csv_map.get(args.dataset)
        if not csv_path:
            raise SystemExit(
                f"dataset '{args.dataset}'을(를) 찾지 못했고, --csv도 지정되지 않았습니다. (원인: {type(e).__name__}: {e})"
            )
        dataset = upload_dataset(name=args.dataset, csv_path=csv_path, suffix=None)

    # 실험 이름/메타데이터 구성
    exp_name = args.name or f"text2sql-{args.dataset}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    exp_meta = {
        "dataset": args.dataset,
        "enable_seed_evidence": os.getenv("ENABLE_SEED_EVIDENCE", "0"),
        "phoenix_base_url": _phoenix_base_url(),
    }

    # task: 질문 -> SQL 생성
    def task(example: Any):
        q = _extract_question(example)
        pred_sql = invoke_text_to_sql_chain(q)
        # output은 evaluator가 쉽게 쓰도록 dict로 반환
        return {"pred_sql": pred_sql}

    # evaluator: 예측SQL vs 정답SQL 실행결과 비교
    def exec_match(input=None, output=None, expected=None, metadata=None):
        return execution_match_evaluator(
            input=input,
            output=output,
            expected=expected,
            metadata=metadata,
        )

    from phoenix.experiments import run_experiment

    run_experiment(
        dataset=dataset,
        task=task,
        evaluators=[exec_match],
        experiment_name=exp_name,
        experiment_description="Text2SQL 실행결과 동치(Execution-match) 평가 실험",
        experiment_metadata=exp_meta,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
    )

    print(f"✅ 실험 실행 완료: {exp_name}")


if __name__ == "__main__":
    main()

