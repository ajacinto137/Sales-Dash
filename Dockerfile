FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ------------------------------------------------------------------
# System dependencies + Microsoft ODBC Driver 18 for SQL Server.
# The base image is Debian 12 (bookworm), which is what the
# packages.microsoft.com/config/debian/12 repo below targets.
# ACCEPT_EULA=Y accepts the Microsoft driver license non-interactively
# so `docker compose up --build` never blocks on a prompt.
# ------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gnupg2 \
        apt-transport-https \
        ca-certificates \
        unixodbc \
        unixodbc-dev \
    && curl -sSL -o /tmp/packages-microsoft-prod.deb \
        https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && rm /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 3005

CMD ["python", "app.py"]
