FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install pandas sqlalchemy psycopg2-binary requests pillow
COPY . .
RUN mkdir -p data
RUN apt update
RUN apt-get install -y postgresql-client fonts-liberation curl jq wget
RUN apt-get install -y fontconfig fonts-dejavu-core fonts-dejavu-extra fc-cache -f -v
RUN apt-get install -y fonts-dejavu-core fontconfig
RUN apt-get install -y redis-tools openssh-client

# --- Oracle Instant Client (sqlplus) ---
ENV ORACLE_HOME=/opt/oracle/instantclient_21_13
ENV LD_LIBRARY_PATH=${ORACLE_HOME}
ENV PATH=${ORACLE_HOME}:${PATH}
RUN (apt-get install -y libaio1t64 || apt-get install -y libaio1) \
    && apt-get install -y unzip \
    && mkdir -p /opt/oracle \
    && curl -fsSL -o /tmp/instantclient-basiclite.zip \
        https://download.oracle.com/otn_software/linux/instantclient/2113000/instantclient-basiclite-linux.x64-21.13.0.0.0dbru.zip \
    && curl -fsSL -o /tmp/instantclient-sqlplus.zip \
        https://download.oracle.com/otn_software/linux/instantclient/2113000/instantclient-sqlplus-linux.x64-21.13.0.0.0dbru.zip \
    && unzip -q /tmp/instantclient-basiclite.zip -d /opt/oracle \
    && unzip -q /tmp/instantclient-sqlplus.zip -d /opt/oracle \
    && rm -f /tmp/instantclient-basiclite.zip /tmp/instantclient-sqlplus.zip \
    && ln -sf ${ORACLE_HOME}/libclntsh.so.21.1 ${ORACLE_HOME}/libclntsh.so \
    && echo "${ORACLE_HOME}" > /etc/ld.so.conf.d/oracle-instantclient.conf \
    && ldconfig

CMD ["uvicorn", "cronhub.main:app", "--host", "0.0.0.0", "--port", "8000"]
