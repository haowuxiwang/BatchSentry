"""Configuration management — loads from .env, exposes typed config."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class SiliconFlowConfig:
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class PaddleOCRConfig:
    api_url: str
    token: str
    model: str


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    database_path: str
    output_dir: str
    llm_provider: str


def load_config():
    return {
        "deepseek": DeepSeekConfig(
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        ),
        "siliconflow": SiliconFlowConfig(
            api_key=os.getenv("SILICONFLOW_API_KEY", ""),
            base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
            model=os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V4-Pro"),
        ),
        "paddle_ocr": PaddleOCRConfig(
            api_url=os.getenv("PADDLE_OCR_API_URL", ""),
            token=os.getenv("PADDLE_OCR_TOKEN", ""),
            model=os.getenv("PADDLE_OCR_MODEL", "PaddleOCR-VL-1.6"),
        ),
        "app": AppConfig(
            host=os.getenv("APP_HOST", "127.0.0.1"),
            port=int(os.getenv("APP_PORT", "8000")),
            database_path=os.getenv("DATABASE_PATH", "data/pharma.db"),
            output_dir=os.getenv("OUTPUT_DIR", "output"),
            llm_provider=os.getenv("LLM_PROVIDER", "deepseek"),
        ),
    }


config = load_config()
