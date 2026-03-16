# 1. 가벼운 Python 3.12 슬림 버전 기반
FROM python:3.12-slim

# 2. 필요한 시스템 패키지 설치 (PostgreSQL 연결용 libpq 등)
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 3. 작업 디렉토리 설정
WORKDIR /app

# 4. 종속성 파일 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 소스 코드 및 템플릿 복사
COPY main.py .
COPY templates/ ./templates/

# 6. 포트 노출
EXPOSE 8000

# 7. 서버 실행 (reload 옵션은 운영 환경에서는 보통 뺍니다)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

