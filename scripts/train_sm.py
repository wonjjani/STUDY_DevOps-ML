# scripts/train_sm.py
# SageMaker Script Mode: /opt/ml/model 에 결과 저장하면 자동으로 model.tar.gz 업로드
import os, io, joblib
import numpy as np
from sklearn.datasets import load_iris
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

def main():
    X, y = load_iris(return_X_y=True)
    pipe = Pipeline([("scaler", StandardScaler()), ("clf", LogisticRegression(max_iter=300))])
    pipe.fit(X, y)

    model_dir = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")
    os.makedirs(model_dir, exist_ok=True)
    out = os.path.join(model_dir, "model.pkl")
    with open(out, "wb") as f:
        joblib.dump(pipe, f)
    print(f"saved: {out}")

if __name__ == "__main__":
    main()
