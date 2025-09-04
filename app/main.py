from fastapi import FastAPI
from pydantic import BaseModel
import os, time, tempfile, joblib, boto3
from botocore.config import Config
from starlette.middleware.base import BaseHTTPMiddleware
from aws_embedded_metrics import metric_scope

SERVICE_NAME = "devops-lab"
app = FastAPI(title="devops-lab-ml")
_model = None
_model_version = None

class IrisInput(BaseModel):
    sepal_length: float
    sepal_width: float
    petal_length: float
    petal_width: float

def _download_from_s3(s3_uri: str) -> str:
    assert s3_uri.startswith("s3://"), "MODEL_S3_URI must start with s3://"
    _, _, rest = s3_uri.partition("s3://")
    bucket, _, key = rest.partition("/")
    s3 = boto3.client("s3", config=Config(retries={"max_attempts": 3}))
    fd, path = tempfile.mkstemp(suffix=".pkl")
    os.close(fd)
    s3.download_file(bucket, key, path)
    return path

def load_model():
    global _model, _model_version
    s3_uri = os.getenv("MODEL_S3_URI")  # e.g. s3://<bucket>/models/devops-lab/latest/model.pkl
    if s3_uri:
        p = _download_from_s3(s3_uri)
        _model = joblib.load(p)
        _model_version = os.getenv("MODEL_VERSION", "latest")
    else:
        # dev fallback (로컬에서만)
        local = os.getenv("MODEL_LOCAL_PATH", "model/model.pkl")
        _model = joblib.load(local)
        _model_version = "local"

@app.on_event("startup")
def _startup():
    load_model()

@metric_scope
def _publish_metrics(metrics, success: bool, latency_ms: float, path: str, method: str, status_code: int):
    metrics.set_namespace("DevOpsLab")
    metrics.put_dimensions({"Service": SERVICE_NAME, "Route": path, "Method": method})
    metrics.put_metric("request_count", 1, "Count")
    metrics.put_metric("success_count", 1 if success else 0, "Count")
    metrics.put_metric("latency_ms", latency_ms, "Milliseconds")
    metrics.set_property("status_code", status_code)

class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = getattr(response, "status_code", 200)
            return response
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            success = 200 <= status < 500
            try:
                _publish_metrics(success, latency_ms, request.url.path, request.method, status)
            except Exception:
                # 메트릭 전송 실패는 서비스 흐름에 영향 주지 않음
                pass

app.add_middleware(MetricsMiddleware)

@app.get("/")
def root():
    return {"ok": True, "service": SERVICE_NAME, "version": "ml-api", "model_version": _model_version}

@app.post("/predict")
def predict(x: IrisInput):
    if _model is None:
        return {"ok": False, "error": "model not loaded"}
    import numpy as np
    X = np.array([[x.sepal_length, x.sepal_width, x.petal_length, x.petal_width]])
    y = _model.predict(X).tolist()
    return {"ok": True, "pred": y[0], "model_version": _model_version}