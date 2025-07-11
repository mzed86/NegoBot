# 1. Pick a base image with Python 3.13
FROM python:3.12-slim
USER root
# Ensure necessary directories exist and have the right permissions
RUN mkdir -p /var/lib/apt/lists/partial && chmod 755 /var/lib/apt/lists/partial

# 2. Install the ODBC driver and build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg && \
    curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - && \
    curl https://packages.microsoft.com/config/debian/10/prod.list \
         > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# 3. Switch to non‑root user
USER 1001

# 3. Copy your code in and install Python deps
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
# 4. Your start command
CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8000"]
