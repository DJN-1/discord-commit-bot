# keep_alive.py

from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "I'm alive"

def run():
  # Render의 웹 서비스는 보통 10000번 포트를 사용합니다.
  app.run(host='0.0.0.0', port=10000)

def keep_alive():
    t = Thread(target=run)
    t.start()