"""
Phoenix 기반 관측 설정 유틸

LangChain 체인 실행을 Phoenix로 전송해 디버깅/모니터링을 돕습니다.
환경변수로 비활성화할 수 있으며, 패키지가 없으면 조용히 건너뜁니다.
"""
import os
from functools import lru_cache

# Docker/로컬 모두에서 기본값으로 동작하도록 로컬 주소를 기본값으로 둡니다.
DEFAULT_PHOENIX_SERVER = "http://localhost:6006"


def _normalize_base_url(url: str) -> str:
    """스킴이 없는 주소가 들어오면 http://를 붙여 정규화합니다."""
    if "://" not in url:
        return f"http://{url}"
    return url


@lru_cache(maxsize=1)
def setup_phoenix_observability() -> bool:
    """
    Phoenix 서버로 트레이스 전송을 초기화합니다.

    Returns:
        bool: 설정 성공 여부 (False면 계측 생략)
    """
    # 간단한 플래그로 계측 ON/OFF (기본 ON)
    enable_flag = os.getenv("ENABLE_PHOENIX", "1").lower()
    if enable_flag not in ("1", "true", "yes", "on"):
        print("[Phoenix] ENABLE_PHOENIX=0 으로 계측을 건너뜁니다.")
        return False

    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from openinference.instrumentation.openai import OpenAIInstrumentor
        from opentelemetry import trace as trace_api
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError as e:
        # 패키지가 없거나(또는 서브모듈이 빠졌거나) 버전이 다른 경우 계측을 생략합니다.
        print(f"[Phoenix] 계측 모듈 불러오기 실패로 건너뜀: {type(e).__name__} {e}")
        return False

    server_url = _normalize_base_url(os.getenv("PHOENIX_SERVER_URL") or DEFAULT_PHOENIX_SERVER).rstrip("/")

    # Phoenix는 OTLP/HTTP 엔드포인트를 제공합니다.
    # (Phoenix 로그에도 HTTP: http://<host>:6006/v1/traces 형태로 출력됩니다.)
    traces_endpoint = f"{server_url}/v1/traces"

    # OpenTelemetry TracerProvider + Exporter를 명시적으로 구성해야 스팬이 실제로 전송됩니다.
    # (환경변수만으로는 SDK가 자동 구성되지 않아 스팬이 안 나갈 수 있음)
    service_name = os.getenv("OTEL_SERVICE_NAME") or "text2sql"
    tracer_provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    try:
        trace_api.set_tracer_provider(tracer_provider)
    except Exception as e:
        # 이미 TracerProvider가 설정된 경우가 있어, 그때는 기존 Provider를 재사용합니다.
        existing = trace_api.get_tracer_provider()
        if hasattr(existing, "add_span_processor"):
            tracer_provider = existing  # type: ignore[assignment]
            print(f"[Phoenix] 기존 TracerProvider 재사용 (사유: {type(e).__name__} {e})")
        else:
            # 기존 provider에 span processor를 붙일 수 없으면 계측을 포기합니다.
            print(f"[Phoenix] TracerProvider 설정 실패로 계측을 건너뜁니다: {type(e).__name__} {e}")
            return False

    tracer_provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint)))

    # LangChain & OpenAI 호출 자동 계측 (openinference 기반)
    LangChainInstrumentor().instrument()
    OpenAIInstrumentor().instrument()

    print(f"[Phoenix] 계측 활성화 완료 → {traces_endpoint}")
    return True
