"""Video-bound shortcuts and durable resource references, without model routing."""
from __future__ import annotations

from vam.video_registry import DomainError


class ChatActions:
    def __init__(self, indexing, study, *, model=None):
        self.indexing, self.study, self.model = indexing, study, model
        self.settings, self.registry = indexing.settings, indexing.registry

    def artifact(self, video_id):
        artifact = self.study.state(video_id)['artifact']
        return artifact if artifact and not artifact['stale'] else None

    def capabilities(self, video_id):
        row = self.registry.get(video_id)
        if row['trashed_at'] is not None or row['status'] in ('deleting', 'delete_failed'):
            return {'available': False, 'mode': 'unavailable', 'reason': 'video_trashed', 'version': None,
                    'actions': dict.fromkeys(('video', 'notes', 'slides', 'transcript'), False)}
        public = self.registry.public(row)
        artifact = self.artifact(video_id)
        model = bool(self.settings.api_key or self.model or self.study.model)
        text = public['capabilities']['transcript'] or bool(artifact)
        retrieval = public['capabilities']['vision'] or public['capabilities']['transcript_search']
        available = bool(model and (text or retrieval))
        reason = '' if available else ('model_not_configured' if not model else 'chat_source_not_ready')
        return {'available': available, 'mode': 'retrieval' if retrieval else 'text' if text else 'unavailable',
                'reason': reason, 'version': public['transcript']['text_version'] or row['active_generation'],
                'actions': {'video': available, 'notes': bool(artifact or model and (text or retrieval)),
                            'slides': bool(artifact or model and (text or retrieval)),
                            'transcript': bool(public['capabilities']['transcript'] or self.settings.asr_enabled)}}

    def validate_scope(self, video_id, scope):
        if scope is None:
            return
        row = self.indexing._usable_video(video_id)
        if scope.end_t > row['duration'] + .001 or scope.start_t >= row['duration']:
            raise DomainError(422, 'invalid_time_range', 'Context must be inside this video')
        current = self.registry.transcript_state(row)['text_version'] or row['active_generation']
        if scope.version and scope.version != current:
            raise DomainError(409, 'chat_context_stale', 'Selected context changed; select it again')

    def _active(self, video_id, table):
        with self.registry.db.connect() as conn:
            row = conn.execute(f"SELECT job_id FROM {table} WHERE video_id=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1" if table == 'study_jobs' else
                               "SELECT job_id FROM index_jobs WHERE video_id=? AND status IN ('queued','running') LIMIT 1", (video_id,)).fetchone()
        return row[0] if row else None

    def study_resource(self, video_id, request_id, label='学习笔记'):
        artifact = self.artifact(video_id)
        if artifact:
            return {'kind': 'study', 'video_id': video_id, 'artifact_id': artifact['artifact_id'], 'label': label}
        jid = self._active(video_id, 'study_jobs')
        if jid is None:
            jid = self.study.submit(video_id, request_id)['job_id']
        return {'kind': 'study', 'video_id': video_id, 'job_id': jid, 'label': label}

    def execute(self, request):
        vid = request.video_id
        self.indexing._usable_video(vid)
        if request.action in ('notes', 'slides'):
            item = self.study_resource(vid, request.request_id, '学习课件' if request.action == 'slides' else '学习笔记')
        elif request.action == 'transcript':
            row = self.registry.get(vid)
            state = self.registry.transcript_state(row)
            if state['readable']:
                item = {'kind': 'transcript', 'video_id': vid, 'version': state['text_version'], 'label': '语音文稿'}
            else:
                if not self.settings.asr_enabled:
                    raise DomainError(409, 'asr_disabled', 'Enable speech transcription first')
                # This shortcut owns a deletion lease but no open source read.
                # Existing per-video write locks still coordinate transcription.
                jid = self._active(vid, 'index_jobs') or self.indexing.schedule(vid, kind='transcript', allow_retained=True)
                item = {'kind': 'transcript', 'video_id': vid, 'job_id': jid, 'label': '语音文稿'}
        else:
            raise DomainError(422, 'invalid_chat_action', 'Unsupported shortcut')
        text = '已准备好学习资料，请在下方查看或下载。' if item.get('artifact_id') or item.get('version') else '学习资料正在后台准备中，可在下方查看进度；完成后即可查看或下载。'
        return {'type': 'final', 'text': text, 'result': {'answer': text, 'resources': [item], 'evidence': [], 'citations': [], 'warnings': []}}

    def resource(self, video_id, *, kind, artifact_id=None, job_id=None, version=None, offset=0):
        self.indexing._usable_video(video_id)
        base = f'/api/videos/{video_id}'
        if kind == 'study':
            if job_id:
                job = self.study.job(video_id, job_id)
                if job['status'] != 'succeeded':
                    return {'status': job['status'], 'error_code': job['error_code'], 'current': job['current'], 'total': job['total'], 'links': []}
                artifact_id = job['artifact_id']
            if not artifact_id:
                raise DomainError(422, 'resource_not_found', 'Missing study resource identity')
            artifact = self.study.artifact(video_id, artifact_id)
            chapters = artifact['content']['chapters']
            return {'status': 'ready', 'stale': artifact['stale'], 'artifact_id': artifact_id, 'total': len(chapters),
                    'preview': [{'text': c['title'] + '：' + '；'.join(c['points']), 'start_t': c['start_t'], 'end_t': c['end_t']} for c in chapters[offset:offset+5]],
                    'links': [{'label': label, 'url': f'{base}/study/artifacts/{artifact_id}/export?format={fmt}'}
                              for fmt, label in [('md', 'Markdown'), ('pptx', 'PPTX'), ('pdf', 'PDF')]]}
        if job_id:
            job = self.registry.job(job_id)
            if job['video_id'] != video_id:
                raise DomainError(404, 'resource_not_found', 'Resource does not belong to this video')
            if job['status'] in ('queued', 'running'):
                return {'status': job['status'], 'phase': job['phase'], 'links': []}
            published = (job.get('result') or {}).get('stages', {}).get('transcript', {}).get('text_version')
            if not published and job['status'] in ('failed', 'interrupted'):
                return {'status': job['status'], 'error_code': job['error_code'], 'links': []}
            version = version or published
        row = self.registry.get(video_id)
        state = self.registry.transcript_state(row)
        if not state['readable']:
            return {'status': 'empty' if state['text_status'] == 'empty' else 'failed', 'error_code': 'transcript_empty' if state['text_status'] == 'empty' else 'transcript_not_ready', 'links': []}
        if version and version != state['text_version']:
            raise DomainError(409, 'chat_context_stale', 'Transcript changed; request the current manuscript')
        version = state['text_version']
        page = self.indexing._transcript_store(video_id).list(kind='segments', offset=offset, limit=10)
        return {'status': 'ready', 'version': version, 'total': page['total'], 'preview': page['items'],
                'links': [{'label': fmt.upper(), 'url': f'{base}/transcripts/export?format={fmt}&version={version}'} for fmt in ('txt', 'srt')]}
