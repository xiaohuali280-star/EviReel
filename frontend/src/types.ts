export interface Video {
  video_id: string; original_name: string; duration: number; size_bytes: number;
  status: string; created_at: number; trashed_at: number | null; latest_job_id: string | null;
  active_generation: string | null; last_error: string | null; content_url: string | null;
  capabilities: {playback: boolean; vision: boolean; transcript: boolean; transcript_search: boolean};
  transcript: {status: string; error: string | null; text_status: string; text_version: string | null; search_version: string | null};
  chat?: {available: boolean; mode: 'retrieval' | 'text' | 'unavailable'; reason: string; version: string | null; actions: Record<ChatAction, boolean>};
}
export interface Page<T> {items: T[]; total: number; offset: number; limit: number}
export interface Evidence {
  evidence_id: string; video_id: string; source: string; index_version: string;
  start_t: number; end_t: number; text: string; frame_id?: string; image_url?: string;
  stale_evidence?: boolean; historical_snapshot?: boolean;
  version_basis?: 'text' | 'vision';
}
export interface AgentEvent {
  type: string; video_id?: string; conversation_id?: string; request_id?: string;
  code?: string; message?: string; text?: string; status?: string;
  result?: {answer?: string; evidence?: Evidence[]; citations?: {evidence_id: string}[]; warnings?: unknown[]; resources?: ChatResource[]; material_mode?: string};
}
export interface Message {message_id: number; request_id: string; role: string; content: string; status: string; error_code?: string; result?: AgentEvent; action?: ChatAction; scope?: ChatScope}
export interface Conversation {conversation_id: string; video_id: string; title: string; updated_at: number}
export interface Transcript {doc_id?: string; segment_id?: string; start_t: number; end_t: number; text: string; transcript_version: string}
export interface Chapter {title: string; start_t: number; end_t: number; points: string[]; source_ids: string[]}
export interface Artifact {artifact_id: string; stale: boolean; created_at: number; content: {title: string; chapters: Chapter[]; evidence: Evidence[]; warnings: string[]}}
export interface Job {job_id: string; status: string; phase: string; current: number; total: number; error_code?: string; error?: string}
export interface Study {job: Job | null; artifact: Artifact | null; model_configured: boolean; auto_enabled: boolean}
export interface UIConfig {upload_mode?: 'single' | 'resumable'; auth_required?: boolean; max_upload_bytes: number; max_video_duration_s: number; extensions: string[]; message_chars: number; model_configured: boolean}
export type ChatAction = 'notes' | 'video' | 'transcript' | 'slides';
export interface ChatScope {start_t: number; end_t: number; version?: string}
export interface ChatPayload {type: 'text'; video_id: string; conversation_id: string; request_id: string; text: string; action?: ChatAction; scope?: ChatScope}
export interface ChatResource {kind: 'study' | 'transcript'; video_id: string; label?: string; artifact_id?: string; job_id?: string; version?: string}
export interface ResourceState {status: string; stale?: boolean; error_code?: string; total?: number; current?: number; preview?: {text: string; start_t: number; end_t: number}[]; links: {label: string; url: string}[]}
