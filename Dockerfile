# 1. Pick a base image with Python 3.11 (slim to keep it small)
FROM python:3.11

# Ensure necessary directories exist and have the right permissions
RUN mkdir -p /var/lib/apt/lists/partial && chmod 755 /var/lib/apt/lists/partial

# 2. Install the ODBC driver and build tools
RUN apt-get update \
 && apt-get install -y curl gnupg unixodbc-dev gcc g++ \
 # bring in MS’s repo
 && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \
 && curl -sSL \
    https://packages.microsoft.com/config/debian/10/prod.list \
    > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y msodbcsql18

# 3. Copy your code in and install Python deps
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt

# 4. Your start command
CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8000"]
