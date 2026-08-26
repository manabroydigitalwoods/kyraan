"""One-time setup for local face recognition: downloads the two OpenCV
model files into data/models/ (gitignored). Run once per machine:

    .venv/bin/python scripts/setup_faces.py

Everything at runtime is then fully offline — see src/kyraan/agents/faces.py.
"""
import sys
import urllib.request
from pathlib import Path

MODELS = Path(__file__).resolve().parents[1] / "data" / "models"
FILES = {
    "face_detection_yunet_2023mar.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/"
        "face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/"
        "face_recognition_sface/face_recognition_sface_2021dec.onnx",
}


def main() -> int:
    MODELS.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        target = MODELS / name
        if target.exists() and target.stat().st_size > 10000:
            print(f"already present: {name}")
            continue
        print(f"downloading {name} ...")
        urllib.request.urlretrieve(url, target)
        print(f"  -> {target.stat().st_size:,} bytes")
    from kyraan.agents import faces
    print("faces.available():", faces.available())
    return 0


if __name__ == "__main__":
    sys.exit(main())
