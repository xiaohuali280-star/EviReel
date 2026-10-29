from typing import Literal, Optional
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
    conversation_recent_turns: int = Field(6, ge=1, le=30)
    conversation_context_chars: int = Field(16000, ge=1000, le=64000)
    conversation_context_tokens: int = Field(8000, ge=500, le=32000)
    conversation_message_chars: int = Field(2000, ge=100, le=4000)
    conversation_summary_chars: int = Field(2000, ge=100, le=8000)
    conversation_summary_batch_turns: int = Field(12, ge=1, le=50)
    conversation_model_timeout_s: float = Field(30, gt=0, le=120)
    conversation_request_timeout_s: float = Field(180, gt=0, le=900)
    conversation_max_running: int = Field(4, ge=1, le=32)
    chat_text_chars: int = Field(12000, ge=1000, le=30000)
    frontend_dist: str = ''
    study_auto_generate: bool = True
    study_max_running: int = Field(2, ge=1, le=8)
    study_max_batches: int = Field(80, ge=1, le=300)
    study_batch_chars: int = Field(10000, ge=1000, le=30000)
    study_timeout_s: float = Field(600, gt=0, le=3600)
    study_model_timeout_s: float = Field(180, gt=0, le=180)
    study_stream: bool = True
    study_model_attempts: int = Field(2, ge=1, le=3)
    study_max_export_pages: int = Field(500, ge=1, le=2000)
    embedding_api_key_override: str = ""
    embedding_base_url_override: str = ""
    embedding_model_override: str = ""
    # E1 platform mode: enabled when DATABASE_URL is set. PostgreSQL holds identities,
    # ownership, quota and upload sessions; S3-compatible storage holds originals.
    database_url: str = ""
    s3_endpoint: str = ""
    s3_public_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "evireel"
    s3_region: str = "us-east-1"
    s3_key_prefix: str = ""
    upload_part_size: int = Field(8 * 1024 * 1024, ge=5 * 1024 * 1024, le=512 * 1024 * 1024)
    upload_session_ttl_s: int = Field(24 * 3600, ge=60, le=7 * 24 * 3600)
    upload_url_ttl_s: int = Field(900, ge=1, le=3600)
    upload_lease_s: int = Field(300, ge=5, le=3600)
    upload_max_active_sessions: int = Field(5, ge=1, le=100)
    upload_verify_attempts: int = Field(3, ge=1, le=20)
    upload_verify_part_etag: bool = True
    upload_reconcile_interval_s: float = Field(60, ge=1, le=3600)
    default_workspace_quota_bytes: int = Field(20 * 1024 ** 3, ge=0)
    auth_session_ttl_s: int = Field(7 * 24 * 3600, ge=300, le=90 * 24 * 3600)
    auth_cookie_secure: bool = False
    # E2 durable pipeline (platform mode only). "inline" keeps the in-process indexer; "worker"
    # records jobs in PostgreSQL and runs stages in Celery workers fed by the outbox dispatcher.
    pipeline_mode: Literal["inline", "worker"] = "inline"
    broker_url: str = ""                       # amqp://user:pass@host:5672/vhost (RabbitMQ)
    pipeline_lease_s: int = Field(120, ge=5, le=3600)          # stage lease; heartbeat every lease/3
    pipeline_stage_max_attempts: int = Field(4, ge=1, le=20)
    pipeline_retry_base_s: float = Field(5, ge=0.1, le=600)
    pipeline_retry_cap_s: float = Field(300, ge=1, le=3600)
    pipeline_dispatch_interval_s: float = Field(1, ge=0.05, le=60)
    pipeline_redispatch_after_s: float = Field(120, ge=1, le=3600)  # pending but never claimed
    pipeline_task_time_limit_s: int = Field(3 * 3600, ge=30, le=24 * 3600)
    pipeline_embed_batch: int = Field(32, ge=1, le=256)
    pipeline_scratch: str = ""                 # worker scratch dir; default DATA_ROOT/.pipeline
    # Test-only injection point ("module:function" returning (backend, transcriber)); never set in production.
    pipeline_factory: str = ""
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
    def platform_enabled(self) -> bool:
        return bool(self.database_url)

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
