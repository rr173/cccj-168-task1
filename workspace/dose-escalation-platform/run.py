"""服务入口: python3 run.py  ->  http://127.0.0.1:8000 (安全工作台)"""
import uvicorn

from app.api import create_app

app = create_app("platform.db")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
