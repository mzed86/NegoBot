# 1. Pick a base image with Python 3.11 (slim to keep it small)
FROM python:3.11
USER root
# Ensure necessary directories exist and have the right permissions
RUN apt-get update && \
apt-get install -y --no-install-recommends \
build-essential \
unixodbc-dev \
curl \
gnupg2 && \
apt-get clean

RUN curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - && \
curl https://packages.microsoft.com/config/debian/10/prod.list > /etc/apt/sources.list.d/mssql-release.list && \
apt-get update && \
ACCEPT_EULA=Y apt-get install -y msodbcsql17 && \
apt-get clean

pip install -r requirements.txt

# 3. Copy your code in and install Python deps
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt

# 4. Your start command
CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8000"]
