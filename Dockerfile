FROM python:3.12-slim 
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    libcap-dev \
    libsystemd-dev \
    libseccomp-dev \
    pkg-config \
    pypy3 \
    uidmap \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system isolate && \
    echo "isolate:100000:65536" >> /etc/subuid && \
    echo "isolate:100000:65536" >> /etc/subgid

RUN git clone https://github.com/ioi/isolate.git
RUN sed -i 's/SYS_quotactl_fd/SYS_quotactl/' isolate/rules.c

RUN make --directory=isolate isolate
RUN make --directory=isolate install

COPY . .

CMD ["fastapi", "run", "src/main.py"]