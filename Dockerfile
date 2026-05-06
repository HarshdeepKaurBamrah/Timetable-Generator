FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["sh", "-c", "python run.py --prod --port ${PORT:-5000} --mongo ${MONGO_URI:-mongodb://mongo:27017} --db ${MONGO_DB:-timetable_db}"]
