from typing import Optional
from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment/.env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", allow_inf_nan=False)

    app_name: str = "EviReel"
    data_root: str = "data/evireel"
    max_upload_bytes: int = Field(1024 * 1024 * 1024, gt=0)
    max_video_duration_s: float = Field(3 * 60 * 60, gt=0)
    index_concurrency: int = Field(2, ge=1, le=8)
    max_queued_jobs: int = Field(20, ge=1, le=1000)
    probe_timeout_s: float = Field(30.0, gt=0)
    media_timeout_s: float = Field(600.0, gt=0)
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    enable_event_finalize: bool = True
    enable_legacy_http_api: bool = False
    asr_enabled: bool = True
    asr_model: str = "tiny"
    asr_model_revision: str = ""
    asr_model_cache: str = "data/asr-models"
    asr_local_files_only: bool = True
    asr_language: str = ""
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    asr_cpu_threads: int = Field(2, ge=1, le=32)
    asr_beam_size: int = Field(5, ge=1, le=10)
    asr_vad_filter: bool = True
    asr_timeout_s: float = Field(900, gt=0, le=7200)
    asr_max_segments: int = Field(20000, ge=1, le=100000)
    asr_max_text_chars: int = Field(2000000, ge=1, le=10000000)
    transcript_chunk_max_chars: int = Field(500, ge=16, le=4000)
    transcript_chunk_max_duration_s: float = Field(30, gt=0, le=300)
    transcript_chunk_pause_s: float = Field(1.5, ge=0, le=30)
    retrieval_fusion: str = Field('rrf', pattern='^(rrf|legacy)$')
    retrieval_rrf_k: int = Field(60, ge=1, le=1000)
    retrieval_candidates_per_source: int = Field(20, ge=1, le=100)
    retrieval_min_similarity: float = Field(0.05, ge=-1, le=1)
    retrieval_max_evidence: int = Field(24, ge=4, le=50)
    retrieval_neighbor_s: float = Field(2, ge=0, le=30)
    retrieval_inspect_frames: int = Field(6, ge=1, le=16)
    retrieval_max_turns: int = Field(4, ge=1, le=20)
    retrieval_answer_text_chars: int = Field(800, ge=100, le=4000)
    rerank_enabled: bool = False
    rerank_api_key: str = ''
    rerank_model: str = 'rerank-v4.0-pro'
    rerank_timeout_s: float = Field(10, gt=0, le=60)
    rerank_candidates: int = Field(20, ge=1, le=100)
    rerank_text_chars: int = Field(2000, ge=100, le=8000)
    embedding_api_key_override: str = ""
    embedding_base_url_override: str = ""
    embedding_model_override: str = ""
    app_version: str = "0.1.0"
    websocket_path: str = "/ws/agent"
    
    # Core
    openrouter_api_key: str = ""
    siliconflow_api_key: str = ""
    dashscope_api_key: str = ""
    llm_model: str = "Qwen/Qwen3-VL-8B-Instruct"
    embedding_model: str = "Qwen/Qwen3-VL-Embedding-8B"
    
    frame_store_path: str = "data/frame_store.sqlite3"

    # Overrides
    openrouter_model_id: str = ""
    openrouter_model_id_main: str = ""
    openrouter_model_id_light: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_embedding_model: str = ""
    openrouter_embedding_dimensions: int = 0
    openrouter_model_id_vlm: str = ""
    siliconflow_model_id: str = ""
    siliconflow_model_id_main: str = "Qwen/Qwen3-VL-8B-Instruct"
    siliconflow_model_id_light: str = "Qwen/Qwen3.5-9B"
    siliconflow_model_id_vlm: str = "Qwen/Qwen3-VL-8B-Instruct"
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_embedding_model: str = "Qwen/Qwen3-VL-Embedding-8B"
    siliconflow_embedding_dimensions: int = 0
    dashscope_model_id: str = "qwen3-vl-plus"
    dashscope_embedding_model: str = "qwen3-vl-embedding"
    dashscope_embedding_dimensions: int = 0
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    
    enable_retrieval: bool = True

    # Video Processing & Filtering Presets
    video_fps: float = Field(0.5, gt=0, le=60)
    video_max_frames: Optional[int] = Field(None, ge=1)
    video_laplacian_min: float = 20.0
    video_diff_threshold: float = 20.0
    video_ssim_threshold: float = 0.92
    video_hist_threshold: float = 0.0
    video_similarity_threshold: float = 0.88  # For Embedding deduplication
    video_event_threshold: float = 0.80       # Trigger VLM event analysis if similarity < this
    video_event_max_duration_s: float = 300.0

    # Layered memory retention
    memory_recent_window_s: float = 60 * 60.0
    memory_mid_window_s: float = 24 * 60 * 60.0
    memory_recent_min_gap_s: float = 1.0
    memory_mid_min_gap_s: float = 20.0
    memory_long_min_gap_s: float = 120.0
    memory_mid_max_side: int = 768
    memory_long_max_side: int = 512
    memory_mid_jpeg_quality: int = 70
    memory_long_jpeg_quality: int = 45

    # LLM Multi-modal limits
    llm_max_image_size_mb: float = 5.0
    llm_max_image_pixels: int = 1280 * 1280
    # DashScope Base64 video limit: encoded string must be smaller than 10 MB.
    llm_max_video_base64_mb: float = 10.0

    # Query ablation: attach the latest finalized segment as video context.
    query_include_latest_segment_video: bool = True
    query_latest_segment_video_path: str = ""

    @property
    def provider_name(self) -> str:
        if self.dashscope_api_key:
            return "dashscope"
        if self.siliconflow_api_key:
            return "siliconflow"
        if self.openrouter_api_key:
            return "openrouter"
        return "siliconflow"

    @property
    def api_key(self) -> str:
        if self.provider_name == "dashscope":
            return self.dashscope_api_key
        if self.provider_name == "openrouter":
            return self.openrouter_api_key
        return self.siliconflow_api_key or self.openrouter_api_key

    @property
    def chat_base_url(self) -> str:
        if self.provider_name == "dashscope":
            return self.dashscope_base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        if self.provider_name == "openrouter":
            return self.openrouter_base_url or "https://openrouter.ai/api/v1"
        return self.siliconflow_base_url or "https://api.siliconflow.cn/v1"

    @property
    def model_id(self) -> str:
        if self.provider_name == "dashscope":
            return self.dashscope_model_id or self.llm_model
        if self.provider_name == "openrouter":
            return (
                self.openrouter_model_id
                or self.openrouter_model_id_main
                or self.openrouter_model_id_light
                or self.openrouter_model_id_vlm
                or self.llm_model
            )
        return (
            self.siliconflow_model_id
            or self.siliconflow_model_id_main
            or self.siliconflow_model_id_light
            or self.siliconflow_model_id_vlm
            or self.llm_model
        )

    @property
    def model_id_main(self) -> str:
        return self.model_id

    @property
    def model_id_light(self) -> str:
        return self.model_id

    @property
    def model_id_vlm(self) -> str:
        return self.model_id

    @property
    def embedding_model_id(self) -> str:
        if self.embedding_model_override:
            return self.embedding_model_override
        if self.provider_name == "dashscope":
            return self.dashscope_embedding_model or self.embedding_model
        if self.provider_name == "openrouter":
            return self.openrouter_embedding_model or self.embedding_model
        return self.siliconflow_embedding_model or self.embedding_model

    @property
    def embedding_dimensions(self) -> int:
        if self.provider_name == "dashscope":
            return int(self.dashscope_embedding_dimensions or 0)
        if self.provider_name == "openrouter":
            return int(self.openrouter_embedding_dimensions or 0)
        return int(self.siliconflow_embedding_dimensions or 0)

_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
