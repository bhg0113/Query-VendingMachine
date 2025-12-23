"""
Phoenix Dataset 업로드 스크립트

로컬에 있는 테스트셋 CSV(기본/고급)를 Phoenix의 Dataset으로 업로드합니다.
- 목적: Phoenix의 Datasets & Experiments 기능으로 실험/평가를 체계화하기 위함

사용 예시(로컬):
  python experiments/phoenix_upload_datasets.py --all

사용 예시(도커 web 컨테이너 내부):
  docker compose exec web python experiments/phoenix_upload_datasets.py --all
"""

from __future__ import annotations

import argparse
import inspect
import os
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import pandas as pd


# 루트 경로를 파이썬 모듈 검색 경로에 추가 (어느 위치에서 실행해도 동작하도록)
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))


DEFAULT_DATASETS: List[Tuple[str, str]] = [
    ("dvdrental-basic", "experiments/dvdrental_testset.csv"),
    ("dvdrental-advanced", "experiments/dvdrental_testset_advanced.csv"),
]


def _phoenix_base_url() -> str:
    """
    Phoenix 서버 주소를 환경변수에서 읽습니다.
    - PHOENIX_BASE_URL 우선
    - 없으면 PHOENIX_SERVER_URL 사용 (본 프로젝트에서 사용 중)
    - 둘 다 없으면 로컬 기본값 사용
    """

    return (
        os.getenv("PHOENIX_BASE_URL")
        or os.getenv("PHOENIX_SERVER_URL")
        or "http://localhost:6006"
    )


def _get_client():
    """phoenix.client.Client를 생성합니다(버전별 시그니처 차이를 흡수)."""
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

    # 버전별로 Client 초기화 시그니처가 다를 수 있어 signature를 보고 맞춰 호출합니다.
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
        """
        버전마다 키워드 인자 이름이 달라질 수 있어,
        1) 흔한 키워드 시도 → 2) signature 기반 자동 추론 → 3) positional 시도 순으로 호출합니다.
        """
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

        # bound method라도 self가 남아 있을 수 있어 제거
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

        # 3) positional (키워드 전용이면 TypeError)
        try:
            return fn(value)
        except TypeError:
            return None

    def _try_get_by_name(fn):
        return _call_single_arg(fn, dataset_name, ["name", "dataset_name", "datasetName"])

    def _try_get_by_id(fn, ds_id: str):
        return _call_single_arg(fn, ds_id, ["dataset_id", "id", "uuid", "rowid", "dataset_rowid"])

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

    def _get_name(obj: Any) -> str:
        # list API가 {"dataset": {...}} 형태를 돌려주는 경우 대응
        if isinstance(obj, dict) and isinstance(obj.get("dataset"), dict):
            obj = obj["dataset"]
        elif hasattr(obj, "dataset") and getattr(obj, "dataset") is not None:
            obj = getattr(obj, "dataset")

        if isinstance(obj, dict):
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
        if isinstance(obj, dict) and isinstance(obj.get("dataset"), dict):
            obj = obj["dataset"]
        elif hasattr(obj, "dataset") and getattr(obj, "dataset") is not None:
            obj = getattr(obj, "dataset")

        if isinstance(obj, dict):
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

    def _list_datasets() -> list[Any]:
        ds_res = getattr(client, "datasets", None)
        if ds_res is None:
            return []

        def _call_list(fn):
            # 버전별로 limit/page_size 등의 파라미터가 다를 수 있어 signature를 보고 시도합니다.
            try:
                params = inspect.signature(fn).parameters
            except Exception:
                params = {}

            candidates = []
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

        for meth in ("list", "list_datasets"):
            if not hasattr(ds_res, meth):
                continue
            fn = getattr(ds_res, meth)
            try:
                return _coerce_list(_call_list(fn))
            except Exception:
                continue

        return []

    def _http_find_dataset_id() -> str:
        """Phoenix 서버 REST API로 dataset id를 직접 조회합니다(최후의 폴백)."""
        try:
            import httpx
        except Exception:
            return ""

        base_url = _phoenix_base_url().rstrip("/")
        candidates = [
            f"{base_url}/v1/datasets",
            f"{base_url}/api/v1/datasets",
        ]

        def _pick_name(item: Any) -> str:
            if isinstance(item, dict):
                for k in ("name", "dataset_name", "datasetName", "display_name", "displayName"):
                    v = item.get(k)
                    if v is not None:
                        return str(v).strip()
            return ""

        def _pick_id(item: Any) -> str:
            if isinstance(item, dict):
                for k in ("id", "dataset_id", "datasetId", "uuid", "rowid", "dataset_rowid"):
                    v = item.get(k)
                    if v is not None and str(v).strip():
                        return str(v)
            return ""

        for url in candidates:
            try:
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
            except Exception:
                continue

            items = data.get("data") or data.get("datasets") or data.get("items") or []
            if isinstance(items, dict) and "data" in items:
                items = items["data"]
            if not isinstance(items, list):
                continue

            for it in items:
                if isinstance(it, dict) and isinstance(it.get("dataset"), dict):
                    it = it["dataset"]
                if _pick_name(it) == dataset_name.strip():
                    return _pick_id(it)
        return ""

    candidates: list[Any] = []
    if hasattr(client, "get_dataset"):
        candidates.append(getattr(client, "get_dataset"))
    if hasattr(client, "datasets") and hasattr(client.datasets, "get_dataset"):
        candidates.append(getattr(client.datasets, "get_dataset"))

    if not candidates:
        raise RuntimeError("Phoenix Client에서 dataset 조회 API를 찾지 못했습니다.")

    # 1) 이름으로 직접 조회
    for fn in candidates:
        ds = _try_get_by_name(fn)
        if ds is not None:
            return ds

    # 2) list로 id를 찾아 id 기반 조회
    for item in _list_datasets():
        if _get_name(item).strip() != dataset_name.strip():
            continue
        ds_id = _get_id(item)
        if not ds_id:
            continue
        for fn in candidates:
            ds = _try_get_by_id(fn, ds_id)
            if ds is not None:
                return ds

    # 3) REST로 id를 찾아 id 기반 조회
    ds_id = _http_find_dataset_id()
    if ds_id:
        for fn in candidates:
            ds = _try_get_by_id(fn, ds_id)
            if ds is not None:
                return ds

    raise RuntimeError(f"dataset '{dataset_name}'을(를) 찾지 못했습니다.")


def _safe_str(v: Any) -> str:
    """None/숫자/Decimal 등 어떤 타입이든 문자열로 정규화."""
    if v is None:
        return ""
    return str(v)


def _load_testset(csv_path: str) -> pd.DataFrame:
    """CSV를 읽어 Phoenix Dataset에 올릴 형태로 정규화."""
    df = pd.read_csv(csv_path)
    # Phoenix에 올릴 때 타입 혼선(숫자/NaN 등)으로 인한 문제를 줄이기 위해 문자열화
    for col in ("question", "sql", "label"):
        if col in df.columns:
            df[col] = df[col].map(_safe_str)
    return df


def _dataset_name_with_suffix(name: str, suffix: Optional[str]) -> str:
    if not suffix:
        return name
    return f"{name}-{suffix}"


def upload_dataset(
    *,
    name: str,
    csv_path: str,
    suffix: Optional[str] = None,
    input_keys: Sequence[str] = ("question",),
    output_keys: Sequence[str] = ("sql",),
    metadata_keys: Sequence[str] = ("label",),
) -> Any:
    """
    Phoenix에 Dataset 업로드(또는 이미 있으면 반환)
    - 기본: name으로 기존 dataset이 있으면 재사용
    - suffix가 있으면 name-suffix로 새 dataset 생성
    """
    client = _get_client()
    dataset_name = _dataset_name_with_suffix(name, suffix)

    if suffix is None:
        # suffix를 안 쓰는 경우엔 기존 dataset이 있으면 그대로 재사용
        try:
            return _get_dataset(client, dataset_name)
        except Exception:
            pass

    df = _load_testset(csv_path)

    # 버전별 API 차이를 흡수: upload_dataset / datasets.create_dataset
    if hasattr(client, "upload_dataset"):
        try:
            return client.upload_dataset(
                dataset_name=dataset_name,
                dataframe=df,
                input_keys=list(input_keys),
                output_keys=list(output_keys),
                metadata_keys=list(metadata_keys),
            )
        except TypeError:
            # 일부 버전은 name 키를 쓸 수 있음
            return client.upload_dataset(
                name=dataset_name,
                dataframe=df,
                input_keys=list(input_keys),
                output_keys=list(output_keys),
                metadata_keys=list(metadata_keys),
            )
        except Exception as e:
            # 이미 동일 이름이 있으면 기존 dataset을 반환 (409 Conflict 등)
            if "already exists" in str(e).lower():
                return _get_dataset(client, dataset_name)
            raise

    # fallback
    if hasattr(client, "datasets") and hasattr(client.datasets, "create_dataset"):
        try:
            return client.datasets.create_dataset(
                name=dataset_name,
                dataframe=df,
                input_keys=list(input_keys),
                output_keys=list(output_keys),
                metadata_keys=list(metadata_keys),
            )
        except Exception as e:
            # 409 Conflict 등: 이미 존재하면 기존 dataset 반환
            if "already exists" in str(e).lower() or "409" in str(e):
                return _get_dataset(client, dataset_name)
            raise

    raise RuntimeError(
        "Phoenix Client에서 dataset 업로드 API를 찾지 못했습니다. arize-phoenix 버전을 확인하세요."
    )


def main():
    parser = argparse.ArgumentParser(description="Phoenix Dataset 업로드 도구")
    parser.add_argument(
        "--all",
        action="store_true",
        help="기본/고급 dvdrental 테스트셋을 모두 업로드",
    )
    parser.add_argument("--name", help="업로드할 dataset 이름(단일 업로드)")
    parser.add_argument("--csv", help="업로드할 CSV 경로(단일 업로드)")
    parser.add_argument(
        "--suffix",
        default=None,
        help="dataset 이름 뒤에 붙일 suffix (예: 20251214). 지정하면 새 dataset으로 업로드합니다.",
    )
    args = parser.parse_args()

    if args.all:
        for name, csv_path in DEFAULT_DATASETS:
            ds = upload_dataset(name=name, csv_path=csv_path, suffix=args.suffix)
            print(f"✅ 업로드 완료: {name} ({csv_path}) → {getattr(ds, 'name', name)}")
        return

    if not args.name or not args.csv:
        raise SystemExit("단일 업로드는 --name 과 --csv 를 함께 지정해야 합니다. (또는 --all)")

    ds = upload_dataset(name=args.name, csv_path=args.csv, suffix=args.suffix)
    print(f"✅ 업로드 완료: {args.name} ({args.csv}) → {getattr(ds, 'name', args.name)}")


if __name__ == "__main__":
    main()

