from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import redis
import psycopg2
import requests
import json
from datetime import datetime

# Microsoft Agent Framework 임포트 예시 (가이드에 따름)
# from agent_framework import Agent, Tool
# from agent_framework_azurefunctions import AzureFunctionHost

app = FastAPI(title="Stock AI Agent API & Monitoring")

# 템플릿 설정
templates = Jinja2Templates(directory="templates")

# ==========================================
# 1. 외부 연결 설정 정보
# ==========================================

REDIS_HOST = "YOUR-REDIS-DOMAIN.koreacentral.redis.azure.net"
REDIS_PORT = 10000
REDIS_PW = "YOUR-REDIS-AUTHENTICATION-KEY"

PG_HOST = "YOUR-POSTGRES-DOMAIN.postgres.database.azure.com"
PG_PORT = 5432
PG_DB = "YOUR-POSTGRES-DATABASE-NAME"
PG_USER = "YOUR-USERNAME"
PG_PW = "YOUR-POSTGRE-PASSWORD"  # 실제 비밀번호

MCP_BASE_URL = "https://mcp-function-app-777-gddjhrd4ceh5a0f5.koreacentral-01.azurewebsites.net/api/stock/{}"
APIM_URL = "https://apim-777.azure-api.net/gpt-4/openai/deployments/gpt-4.1/chat/completions?api-version=2025-03-01-preview"

# Redis 클라이언트 초기화 (SSL 및 Keep-alive 옵션 추가 반영)
redis_client = redis.Redis(
    host=REDIS_HOST, 
    port=REDIS_PORT, 
    password=REDIS_PW, 
    decode_responses=True,
    ssl=True,                  # Azure Redis 필수: SSL/TLS 암호화 활성화
    socket_keepalive=True,     # 네트워크 단절 방지: Keep-alive 활성화
    health_check_interval=30   # 30초마다 Health Check(Ping)를 보내 연결 유지
)

class AnalyzeRequest(BaseModel):
    userId: str
    stockTicker: str

# ==========================================
# 2. 핵심 비즈니스 로직 (공통 함수)
# ==========================================
def process_stock_analysis(user_id: str, ticker: str):
    """API와 웹 UI 양쪽에서 호출되는 핵심 End-to-End 로직"""
    ticker = ticker.upper()
    redis_key = f"UserId:{user_id},StockTicker:{ticker}"
    debug_log = [] # 웹 UI 모니터링을 위한 디버그 기록

    # ---------------------------------------------------------
    # (Step 2) 단기 Memory (Redis) 조회
    # ---------------------------------------------------------
    try:
        cached_data_str = redis_client.execute_command('JSON.GET', redis_key)
        if cached_data_str:
            cached_data = json.loads(cached_data_str)
            return {
                "stockTicker": ticker,
                "score": cached_data.get("score"),
                "LlmReply": cached_data.get("LlmReply"),
                "source": "redis_cache"
            }, "Redis에서 캐시된 데이터를 가져왔습니다."
    except Exception as e:
        debug_log.append(f"Redis 조회 오류(무시됨): {e}")

    # (Step 2-B) 캐시 Miss

    # ---------------------------------------------------------
    # (Step 3) 외부 Vector DB (PostgreSQL) 조회
    # ---------------------------------------------------------
    vector_data = None
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER, password=PG_PW
        )
        cur = conn.cursor()
        cur.execute("SELECT VectorAnalysis FROM VectorDBTable WHERE StockTicker = %s LIMIT 1", (ticker,))
        row = cur.fetchone()
        if row:
            vector_data = row[0]
            debug_log.append("PostgreSQL Vector 데이터 로드 성공.")
        cur.close()
        conn.close()
    except Exception as e:
        debug_log.append(f"PostgreSQL 조회 오류: {e}")

    # ---------------------------------------------------------
    # (Step 4) 외부 MCP Server 호출
    # ---------------------------------------------------------
    mcp_data = {}
    try:
        mcp_response = requests.get(MCP_BASE_URL.format(ticker), timeout=15)
        if mcp_response.status_code == 200:
            mcp_data = mcp_response.json()
            debug_log.append("MCP 서버 데이터 로드 성공.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MCP 서버 호출 실패: {e}")

    # ---------------------------------------------------------
    # (Step 5) Prompt 구성 및 Azure APIM 호출
    # ---------------------------------------------------------
    system_prompt = "You are a highly capable AI financial analyst. Based on the provided data, evaluate the stock."
    
    user_prompt = f"""
    Please analyze the following stock and provide a recommendation.
    
    [Target Stock]: {ticker}
    [Vector RAG Data]: {vector_data if vector_data else 'No historical RAG context available.'}
    [Real-time MCP Data]: {json.dumps(mcp_data, ensure_ascii=False)}
    
    Respond STRICTLY in the following JSON format without any markdown wrappers or extra text:
    {{
        "score": <integer from 1 to 5>,
        "LlmReply": "<your detailed analysis string ending with a recommendation wrapped in curly braces, such as {{강력 매수}}, {{매수}}, {{중립}}, {{매도}}, or {{강력 매도}}>"
    }}
    """

    apim_headers = {
        "Host": "apim-777.azure-api.net",
        "Content-Type": "application/json"
    }
    
    apim_payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 1000
    }

    # ---------------------------------------------------------
    # (Step 6 & 7) APIM 응답 수신 및 Redis 저장
    # ---------------------------------------------------------
    try:
        debug_log.append("APIM (GPT-4.1) 호출 중...")
        # APIM 응답 대기 시간을 120초로 넉넉하게 설정
        apim_response = requests.post(APIM_URL, headers=apim_headers, json=apim_payload, timeout=120)
        apim_response.raise_for_status()
        
        result_json = apim_response.json()
        llm_content = result_json["choices"][0]["message"]["content"]
        
        try:
            parsed_llm_response = json.loads(llm_content)
            final_score = parsed_llm_response.get("score", 3)
            final_reply = parsed_llm_response.get("LlmReply", "{중립}")
            debug_log.append("APIM 응답 파싱 성공.")
        except json.JSONDecodeError:
            raise Exception(f"LLM이 올바른 JSON을 반환하지 않았습니다. 원본: {llm_content}")

    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="[에러 1] APIM (LLM) 응답 시간이 초과되었습니다 (Timeout).")
    except Exception as e:
        error_msg = f"[에러 2] APIM 호출 중 에러 발생: {str(e)}"
        if 'apim_response' in locals():
            error_msg += f" | 상태 코드: {apim_response.status_code} | 응답 본문: {apim_response.text}"
        raise HTTPException(status_code=500, detail=error_msg)

    # Redis에 데이터 저장 (Step 7)
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        redis_save_data = {
            "DateTime": now_str,
            "score": final_score,
            "LlmReply": final_reply
        }
        
        redis_client.execute_command(
            'JSON.SET', 
            redis_key, 
            '$', 
            json.dumps(redis_save_data, ensure_ascii=False)
        )
        debug_log.append("Redis 캐시 저장 완료.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"[에러 3] Redis 저장 중 오류 발생: {e}")

    # ---------------------------------------------------------
    # (Step 8) 최종 응답 반환
    # ---------------------------------------------------------
    return {
        "stockTicker": ticker,
        "score": final_score,
        "LlmReply": final_reply,
        "source": "llm_analysis"
    }, " | ".join(debug_log)


# ==========================================
# 3. 라우팅 (API Endpoint & Web UI)
# ==========================================

# 1) 기존 백엔드 WAS (Spring Boot) 용 REST API 유지
@app.post("/analyze")
def analyze_stock_api(request: AnalyzeRequest):
    result, _ = process_stock_analysis(request.userId, request.stockTicker)
    return result


# 2) 모니터링 테스트용 Web UI (GET: 화면 렌더링)
@app.get("/test", response_class=HTMLResponse)
def test_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# 3) 모니터링 테스트용 Web UI (POST: 폼 전송 처리)
@app.post("/test-agent", response_class=HTMLResponse)
def test_agent(request: Request, userId: str = Form(...), stockTicker: str = Form(...)):
    try:
        # 공통 로직 호출
        result, debug_info = process_stock_analysis(userId, stockTicker)
        return templates.TemplateResponse("index.html", {
            "request": request, 
            "result": result,
            "debug_info": debug_info
        })
    except HTTPException as e:
        return templates.TemplateResponse("index.html", {"request": request, "error": f"{e.status_code}: {e.detail}"})
    except Exception as e:
        return templates.TemplateResponse("index.html", {
            "request": request, 
            "error": str(e)
        })
