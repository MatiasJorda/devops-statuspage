# Servicio caotico: solo biblioteca estandar, asi que no hay nada que instalar.
FROM python:3.12-slim

WORKDIR /usr/local/app
ENV PYTHONUNBUFFERED=1

COPY main.py ./

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8080
CMD ["python", "main.py"]
