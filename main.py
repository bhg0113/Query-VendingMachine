import streamlit as st
import pandas as pd
import os

# LangChain 모듈 임포트
from chains.text_to_sql_chain import invoke_text_to_sql_chain
from experiments.experiment_1.run import run as ex1_run
from experiments.experiment_2.run import run as ex2_run
from utils import run_query, log_step


def main():
    st.title("📝 Text2SQL Demo with LangChain")

    tabs = st.tabs(["Text2SQL", "실험결과 1 (기본)", "실험결과 2 (고급)"])

    with tabs[0]:
        st.subheader("🔍 자연어 → SQL 변환")
        
        natural_query = st.text_input(
            "질문을 입력하세요:",
            "배우는 총 몇 명인가요?",
        )

        if st.button("🚀 실행", key="run_text2sql"):
            try:
                log_step("🎯 사용자 요청 시작")
                sql = invoke_text_to_sql_chain(natural_query)
                log_step("Step 6: SQL 정리 완료", {"정리된_SQL": sql})
                
                # 생성된 SQL 표시
                st.markdown("#### 📝 생성된 SQL")
                st.code(sql, language="sql")

                try:
                    log_step("Step 7: SQL 쿼리 실행 중...", {"SQL": sql})
                    rows = run_query(query=sql, dvd=True)
                    df = pd.DataFrame(rows)
                    log_step("Step 8: 쿼리 실행 완료", {
                        "반환된_행_수": len(rows),
                        "컬럼_수": len(df.columns),
                        "컬럼명": list(df.columns),
                    })
                    
                    # 결과 표시
                    st.markdown("#### ✅ 실행 결과")
                    st.dataframe(df, use_container_width=True)
                    
                    log_step("✅ 전체 파이프라인 완료", {
                        "최종_결과_행수": len(rows),
                        "처리_상태": "성공",
                    })
                    st.session_state["experiment_result"] = df
                except Exception as e:
                    log_step("❌ 쿼리 실행 오류 발생", {
                        "에러_타입": type(e).__name__,
                        "에러_메시지": str(e),
                    })
                    st.error(f"❌ 쿼리 실행 오류: {e}")
            except Exception as e:
                log_step("❌ SQL 생성 오류 발생", {
                    "에러_타입": type(e).__name__,
                    "에러_메시지": str(e),
                })
                st.error(f"❌ SQL 생성 오류: {e}")

    with tabs[1]:
        st.header("📊 실험결과 1 - 기본 SQL 문제")
        st.write("**파이프라인:** 기본 스키마 + 테이블 요약 → RAG 유사 문서 검색 → 프롬프트 생성 → SQL 생성 → 실행")
        st.write("**난이도:** 🟢 기본 (COUNT, 단순 JOIN, 기본 집계함수)")
        csv_path = "experiments/experiment_1/result.csv"

        if os.path.exists(csv_path):
            df = pd.read_csv(csv_path)
            # session_state에 저장 (처음 한 번만)
            if "experiment_1" not in st.session_state:
                st.session_state["experiment_1"] = df
            
            # 정확도 계산
            if "infer" in df.columns and "label" in df.columns:
                correct = 0
                for i in range(len(df)):
                    label = str(df.iloc[i]["label"]).strip()
                    infer = str(df.iloc[i]["infer"]).strip()
                    if label == infer and infer != "":
                        correct += 1
                accuracy = (correct / len(df) * 100) if len(df) > 0 else 0
                
                st.metric("정확도", f"{accuracy:.1f}%", f"{correct}/{len(df)}")
            
            # 테이블 표시 (SQL 비교 가능하도록)
            st.markdown("#### 📋 전체 결과")
            st.dataframe(st.session_state["experiment_1"], use_container_width=True)
            
            # SQL 비교 섹션
            st.markdown("#### 🔍 SQL 상세 비교")
            col1, col2 = st.columns([1, 3])
            with col1:
                selected_row = st.selectbox(
                    "질문 선택:",
                    options=range(len(df)),
                    format_func=lambda i: f"{i+1}. {df.iloc[i]['question'][:30]}..."
                )
            
            if selected_row is not None:
                row = df.iloc[selected_row]
                st.markdown(f"**Q: {row['question']}**")
                
                col_sql, col_infer = st.columns(2)
                with col_sql:
                    st.markdown("**✅ 정답 SQL:**")
                    st.code(row['sql'], language="sql")
                    st.markdown(f"**결과:** `{row['label']}`")
                
                with col_infer:
                    st.markdown("**🤖 생성 SQL:**")
                    if pd.notna(row.get('infer_sql')):
                        st.code(row['infer_sql'], language="sql")
                    else:
                        st.warning("생성 SQL이 없습니다.")
                    st.markdown(f"**결과:** `{row['infer']}`")
                
                # 일치 여부
                label_str = str(row['label']).strip()
                infer_str = str(row['infer']).strip()
                if label_str == infer_str and infer_str != "":
                    st.success("✅ 결과가 일치합니다!")
                else:
                    st.error(f"❌ 결과 불일치 (정답: {label_str}, 생성: {infer_str})")
        else:
            st.info("아직 실험 결과가 없습니다.")
            if st.button("🚀 실험 실행하기", key="run_ex1"):
                with st.spinner("실험 중... 잠시 기다려주세요"):
                    df = ex1_run()
                    st.session_state["experiment_1"] = df
                    st.rerun()
    
    with tabs[2]:
        st.header("📊 실험결과 2 - 고급 SQL 문제")
        st.write("**파이프라인:** 기본 스키마 + 테이블 요약 → RAG 유사 문서 검색 → 프롬프트 생성 → SQL 생성 → 실행")
        st.write("**난이도:** 🟠 고급 (복잡한 JOIN, 윈도우 함수, 서브쿼리, 코딩테스트 수준)")
        csv_path = "experiments/experiment_2/result.csv"

        if os.path.exists(csv_path):
            df = pd.read_csv(csv_path)
            # session_state에 저장 (처음 한 번만)
            if "experiment_2" not in st.session_state:
                st.session_state["experiment_2"] = df
            
            # 정확도 계산
            if "infer" in df.columns and "label" in df.columns:
                correct = 0
                for i in range(len(df)):
                    label = str(df.iloc[i]["label"]).strip()
                    infer = str(df.iloc[i]["infer"]).strip()
                    if label == infer and infer != "":
                        correct += 1
                accuracy = (correct / len(df) * 100) if len(df) > 0 else 0
                
                st.metric("정확도", f"{accuracy:.1f}%", f"{correct}/{len(df)}")
            
            # 테이블 표시 (SQL 비교 가능하도록)
            st.markdown("#### 📋 전체 결과")
            st.dataframe(st.session_state["experiment_2"], use_container_width=True)
            
            # SQL 비교 섹션
            st.markdown("#### 🔍 SQL 상세 비교")
            col1, col2 = st.columns([1, 3])
            with col1:
                selected_row = st.selectbox(
                    "질문 선택:",
                    options=range(len(df)),
                    format_func=lambda i: f"{i+1}. {df.iloc[i]['question'][:30]}...",
                    key="ex2_select"
                )
            
            if selected_row is not None:
                row = df.iloc[selected_row]
                st.markdown(f"**Q: {row['question']}**")
                
                col_sql, col_infer = st.columns(2)
                with col_sql:
                    st.markdown("**✅ 정답 SQL:**")
                    st.code(row['sql'], language="sql")
                    st.markdown(f"**결과:** `{row['label']}`")
                
                with col_infer:
                    st.markdown("**🤖 생성 SQL:**")
                    if pd.notna(row.get('infer_sql')):
                        st.code(row['infer_sql'], language="sql")
                    else:
                        st.warning("생성 SQL이 없습니다.")
                    st.markdown(f"**결과:** `{row['infer']}`")
                
                # 일치 여부
                label_str = str(row['label']).strip()
                infer_str = str(row['infer']).strip()
                if label_str == infer_str and infer_str != "":
                    st.success("✅ 결과가 일치합니다!")
                else:
                    st.error(f"❌ 결과 불일치 (정답: {label_str}, 생성: {infer_str})")
        else:
            st.info("아직 실험 결과가 없습니다.")
            if st.button("🚀 실험 실행하기", key="run_ex2"):
                with st.spinner("실험 중... 잠시 기다려주세요"):
                    df = ex2_run()
                    st.session_state["experiment_2"] = df
                    st.rerun()


if __name__ == "__main__":
    main()