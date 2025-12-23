# 고급 SQL 문제 평가 실험 (Advanced SQL Test Evaluation)
# 다양한 테이블을 조인하고 복잡한 쿼리를 다루는 능력을 평가합니다.

from chains.text_to_sql_chain import invoke_text_to_sql_chain
from utils import run_query, log_step
import pandas as pd
import os
import traceback

def run():
    """
    고급 SQL 테스트셋으로 모델 성능을 평가합니다.
    
    Returns:
        pd.DataFrame: 결과를 포함한 데이터프레임 (infer_sql, infer 컬럼 추가)
    """

    # 결과 파일이 이미 존재하면 반환
    if os.path.exists("experiments/experiment_2/result.csv"):
        return pd.read_csv("experiments/experiment_2/result.csv")

    # 고급 테스트셋 로드
    testset = pd.read_csv("experiments/dvdrental_testset_advanced.csv")
    question = testset["question"].tolist()

    # 질문 개수와 결과 개수를 항상 1:1로 맞추기 위해 DataFrame에 바로 기록합니다.
    # (중간 단계에서 예외가 발생해도 길이 불일치가 나지 않도록)
    testset["infer_sql"] = ""  # 생성한 SQL
    testset["infer"] = ""  # 실행 결과(헤더 없는 CSV 문자열)
    
    for idx, natural_query in enumerate(question):
        sql = ""
        result_csv = ""
        try:
            log_step(f"🔄 고급 질문 {idx+1}/{len(question)} 처리 중", {"질문": natural_query})
            
            # 모델이 생성한 SQL
            sql = invoke_text_to_sql_chain(natural_query)
            log_step(f"✅ SQL 생성 성공", {"생성된_SQL": sql})
            
            # 생성한 SQL 실행
            rows = run_query(query=sql, dvd=True)
            df = pd.DataFrame(rows)

            # 전체 결과를 CSV 문자열(헤더 없음)로 저장하여 label과 동일 포맷으로 비교
            result_csv = "" if df.empty else df.to_csv(index=False, header=False).strip("\n")
            log_step(f"✅ 쿼리 실행 완료", {"결과": result_csv})
        except Exception as e:
            log_step("❌ 오류 발생", {
                "질문": natural_query,
                "생성된_SQL": sql,
                "에러_타입": type(e).__name__,
                "에러_메시지": str(e),
                "스택_트레이스": traceback.format_exc(),
            })
        finally:
            # 질문 1개당 결과를 정확히 1번만 기록 (길이 불일치 방지)
            row_key = testset.index[idx]
            testset.at[row_key, "infer_sql"] = sql
            testset.at[row_key, "infer"] = result_csv

    testset.to_csv("experiments/experiment_2/result.csv", index=False, encoding="utf-8-sig")
    return testset

