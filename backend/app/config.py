"""Central settings. Dev-mode secrets via .env — documented non-goal: no Vault/KMS in this build."""
from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    jwt_secret: str = "sentinel-vision-dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 480

    db_path: Path = BASE_DIR / "sentinel.db"
    uploads_dir: Path = BASE_DIR / "uploads"
    evidence_dir: Path = BASE_DIR / "evidence_store"

    # Detection pipeline
    model_name: str = "yolov8n.pt"
    model_version: str = "yolov8n-coco-1.0"
    rule_version: str = "rules-1.0"
    detect_every_n_frames: int = 3  # throttle inference for CPU
    confidence_threshold: float = 0.4

    class Config:
        env_file = ".env"


settings = Settings()
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
settings.evidence_dir.mkdir(parents=True, exist_ok=True)
