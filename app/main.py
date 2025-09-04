# app/main.py
from fastapi import FastAPI
app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "service": "devops-lab", "version": "v3"} 
