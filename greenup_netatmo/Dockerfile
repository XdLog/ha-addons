FROM python:3.12-alpine

RUN pip install --no-cache-dir requests==2.32.3 paho-mqtt==2.1.0

COPY greenup.py /greenup.py

CMD ["python3", "-u", "/greenup.py"]
