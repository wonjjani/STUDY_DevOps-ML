import os, io, joblib, boto3, json, numpy as np, random
from sklearn.datasets import load_iris
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from datetime import datetime

# 재현성
np.random.seed(42); random.seed(42)

BUCKET  = os.environ["MODEL_BUCKET"]                 # e.g. devops-lab-<acct>-ml
REPO    = os.environ.get("MODEL_REPO", "devops-lab")
VERSION = os.environ.get("MODEL_VERSION") or datetime.utcnow().strftime("%Y%m%d%H%M%S")
BASE    = f"models/{REPO}/{VERSION}"
LATEST  = f"models/{REPO}/latest"

X, y = load_iris(return_X_y=True)
pipe = Pipeline([("scaler", StandardScaler()), ("clf", LogisticRegression(max_iter=300))])
pipe.fit(X, y)

buf = io.BytesIO()
joblib.dump(pipe, buf); buf.seek(0)

s3 = boto3.client("s3")
key = f"{BASE}/model.pkl"
s3.upload_fileobj(buf, BUCKET, key)
print("uploaded:", f"s3://{BUCKET}/{key}")

# latest 포인터 갱신
s3.copy_object(Bucket=BUCKET, CopySource={"Bucket": BUCKET, "Key": key}, Key=f"{LATEST}/model.pkl")

meta = {"version": VERSION, "algo": "LogReg", "dataset": "iris", "seed": 42}
s3.put_object(Bucket=BUCKET, Key=f"{LATEST}/metadata.json", Body=json.dumps(meta).encode("utf-8"),
              ContentType="application/json")
print("updated latest ->", VERSION)
