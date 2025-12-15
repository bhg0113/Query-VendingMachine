"""
SQL 생성 기본 체인 모듈

LCEL(LangChain Expression Language)을 사용하여
프롬프트 + LLM을 조합한 SQL 생성 체인을 정의합니다.
"""
from langchain_core.runnables import RunnablePassthrough
from config.llm_config import get_llm
from prompts.sql_generation_prompt import get_sql_generation_prompt


def create_sql_generation_chain():
    """
    SQL 생성 체인 생성

    Returns:
        Runnable: LangChain 체인 (프롬프트 | LLM 조합)
    
    체인 구조:
        1. 입력: {"context": str, "question": str, "primary_table": str, "evidence": str, "clarified_question": str}
        2. 프롬프트 템플릿에 변수 주입
        3. LLM을 통해 SQL 생성
        4. 출력: LLM의 응답 (AIMessage 객체)
    """
    prompt = get_sql_generation_prompt()
    llm = get_llm()
    
    # SEED 변수(evidence/clarified_question)가 없더라도 동작하도록 기본값을 주입
    chain = (
        RunnablePassthrough.assign(
            evidence=lambda x: x.get("evidence", ""),
            clarified_question=lambda x: x.get("clarified_question", x.get("question", "")),
        )
        | prompt
        | llm
    )
    
    return chain


# 모듈 로드 시 체인 인스턴스 생성
sql_generation_chain = create_sql_generation_chain()
