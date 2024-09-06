FROM python:3-alpine 
WORKDIR /app

COPY requirements.txt ./
RUN apk update && apk add gcc g++ libc-dev linux-headers
RUN pip install --no-cache-dir -r requirements.txt

RUN apk add git make libcap-dev elogind-dev
RUN git clone https://github.com/ioi/isolate.git

RUN make --directory=isolate isolate
RUN make --directory=isolate install

COPY . .

CMD ["fastapi", "run", "src/main.py"]