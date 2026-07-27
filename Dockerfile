FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Default remote user for the auto-generated SSH key (shell jobs that ssh
# out use this). Override in .env / docker run -e without rebuilding.
ENV CRONHUB_SSH_USER=ansible

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
RUN apt-get install -y redis-tools openssh-client iputils-ping

# --- Oracle Instant Client 19.25 + SQL*Plus ---
# ENV (not ~/.bashrc!) so it applies to cronhub's job subprocesses too -
# those are spawned non-interactively and never source .bashrc.
ENV ORACLE_HOME=/usr/lib/oracle/19.25/client64
ENV LD_LIBRARY_PATH=${ORACLE_HOME}/lib
ENV PATH=${ORACLE_HOME}/bin:${PATH}

# vendor/db-arsenal/ is the drop zone for any DB client zips/rpms (see its
# README) - files placed there are used directly at build time with no
# network call; anything missing falls back to downloading it below.
COPY vendor/db-arsenal/ /tmp/db-arsenal/

# sqlplus's zip is extracted to BOTH bin/ (for the executable, on PATH) and
# lib/ (for libsqlplus*.so, which is only found via LD_LIBRARY_PATH).
# Debian Trixie's libaio1t64 doesn't ship the libaio.so.1 soname sqlplus
# (built against the pre-time64 ABI) expects, so we symlink whatever
# libaio.so.* actually exists onto that name.
RUN (apt-get install -y libaio1t64 || apt-get install -y libaio1) \
    && apt-get install -y unzip \
    && mkdir -p ${ORACLE_HOME}/lib ${ORACLE_HOME}/bin \
    && fetch() { \
         i=1; \
         while [ $i -le 12 ]; do \
           curl --connect-timeout 10 --max-time 90 -fsSL -o "$2" "$1" && return 0; \
           echo "[oracle-fetch] attempt $i failed for $1, retrying in 4s..."; \
           i=$((i + 1)); \
           sleep 4; \
         done; \
         echo "[oracle-fetch] giving up on $1 after $((i - 1)) attempts"; \
         return 1; \
       } \
    && get() { \
         if [ -f "/tmp/db-arsenal/$1" ]; then \
           echo "[oracle-fetch] using vendored /tmp/db-arsenal/$1"; \
           cp "/tmp/db-arsenal/$1" "/tmp/$1"; \
         else \
           echo "[oracle-fetch] $1 not vendored in db-arsenal, downloading..."; \
           fetch "$2" "/tmp/$1"; \
         fi \
       } \
    && get instantclient-basic-linux.x64-19.25.0.0.0dbru.zip \
         https://download.oracle.com/otn_software/linux/instantclient/1925000/instantclient-basic-linux.x64-19.25.0.0.0dbru.zip \
    && get instantclient-sdk-linux.x64-19.25.0.0.0dbru.zip \
         https://download.oracle.com/otn_software/linux/instantclient/1925000/instantclient-sdk-linux.x64-19.25.0.0.0dbru.zip \
    && get instantclient-sqlplus-linux.x64-19.25.0.0.0dbru.zip \
         https://download.oracle.com/otn_software/linux/instantclient/1925000/instantclient-sqlplus-linux.x64-19.25.0.0.0dbru.zip \
    && unzip -j -o /tmp/instantclient-basic-linux.x64-19.25.0.0.0dbru.zip -d ${ORACLE_HOME}/lib \
    && unzip -j -o /tmp/instantclient-sdk-linux.x64-19.25.0.0.0dbru.zip -d ${ORACLE_HOME}/lib \
    && unzip -j -o /tmp/instantclient-sqlplus-linux.x64-19.25.0.0.0dbru.zip -d ${ORACLE_HOME}/bin \
    && unzip -j -o /tmp/instantclient-sqlplus-linux.x64-19.25.0.0.0dbru.zip -d ${ORACLE_HOME}/lib \
    && rm -rf /tmp/instantclient-*.zip /tmp/db-arsenal \
    && ln -sf ${ORACLE_HOME}/lib/libclntsh.so.19.1 ${ORACLE_HOME}/lib/libclntsh.so \
    && ln -sf "$(find /usr/lib -name 'libaio.so.*' | head -n 1)" /usr/lib/x86_64-linux-gnu/libaio.so.1 \
    && echo "${ORACLE_HOME}/lib" > /etc/ld.so.conf.d/oracle-instantclient.conf \
    && ldconfig

CMD ["uvicorn", "cronhub.main:app", "--host", "0.0.0.0", "--port", "8000"]
