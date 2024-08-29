FROM python:3-alpine 
WORKDIR /app

COPY requirements.txt ./
RUN apk update && apk add gcc g++ libc-dev linux-headers
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["fastapi", "run", "src/main.py"]