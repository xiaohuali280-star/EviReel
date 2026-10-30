import {useCallback, useEffect, useRef, useState} from 'react';
import Markdown from 'react-markdown';
import {ArrowUp, BookOpen, LogOut, Check, ChevronDown, ChevronRight, Copy, Download, FileText, Globe, History, LoaderCircle, LockKeyhole, Maximize, MessageSquarePlus, Moon, PanelLeftClose, PanelLeftOpen, Paperclip, Play, Plus, Presentation, RefreshCw, Search, Sun, Trash2, Video as VideoIcon, X} from 'lucide-react';
import {api, codeText, downloadFile, errorText, isProcessing, post, requestId, seekTime, time, upload} from './api';
import {ChatChannel, remembered} from './chat';
import {actionMessages, ResourceCard} from './quick-actions';
import {useResizablePanels} from './use-resizable-panels';
import {useSidebar} from './use-sidebar';
import {TranscriptReader} from './TranscriptReader';
import {Login, type Me} from './login';
import {ResumeBanner, UploadPanel, uploadFailureText} from './upload-panel';
import {ApiError} from './api';
import {browserTransport, ResumableUpload, UploadError, type UploadSession, type UploadState} from './resumable-upload';
import type {AgentEvent, Chapter, ChatAction, ChatPayload, ChatScope, Conversation, Evidence, Job, Message, Page, Study, Transcript, UIConfig, Video} from './types';

const prompts = ['这段视频的主要内容是什么？', '帮我总结这节课的重点', '这部分的要点有什么好处？', '有哪些值得记录的知识点？'];
const vidPattern = /^v_[0-9a-f]{32}$/;
function selectedFromURL() {const v = new URLSearchParams(location.search).get('video') || ''; return vidPattern.test(v) ? v : ''}
function Robot({large = false}: {large?: boolean}) {
  return <div className={`robot-art ${large ? 'large' : ''}`} aria-hidden="true"><span className="spark s1">✦</span><span className="spark s2">✧</span><span className="bubble b1">━━<br />━</span><span className="bubble b2">━━<br />━</span><span className="robot-head"><span className="robot-face"><i /><i /></span></span><span className="robot-body" /></div>;
}
function Welcome({ready, action, suggest, actions, pending = false, compact = false, hasVideo = false}: {ready: boolean; action: (name: string) => void; suggest: (text: string) => void; actions?: Record<string, boolean>; pending?: boolean; compact?: boolean; hasVideo?: boolean}) {
  const features = [
    {name: '自动生成笔记', text: '智能提取视频核心内容，生成结构化笔记。', icon: <FileText />, color: 'blue', action: 'notes'},
    {name: 'AI 看视频', text: '快速理解视频重点，提炼核心观点。', icon: <Play />, color: 'purple', action: 'video'},
    {name: '生成文稿', text: '自动生成逐字稿，并智能梳理要点。', icon: <FileText />, color: 'green', action: 'transcript'},
    {name: '生成课件', text: '一键生成精美课件，支持导出与下载。', icon: <Presentation />, color: 'orange', action: 'slides'},
  ];
  return <div className={`welcome ${compact ? 'compact' : ''}`}>{!compact && <><Robot /><h2>{ready ? '围绕视频，开启深入学习' : '上传视频后，开始和 AI 助手对话'}</h2><p>我可以帮你总结视频内容、回答相关问题、<br />定位具体时间点，并支持基于视频内容继续深入讨论。</p></>}<h3>我可以帮你：</h3><div className="features">{features.map(f => <button key={f.name} className={`feature ${f.color}`} disabled={pending || !(actions?.[f.action] ?? ready)} onClick={() => action(f.action)}><span className="feature-icon">{f.icon}</span><span><strong>{f.name}</strong><small>{f.text}</small></span></button>)}</div><div className="suggest-title">你可以试着问{!ready && '（内容就绪后可用）'}</div><div className="suggestions">{prompts.map(p => <button key={p} disabled={!ready || pending} onClick={() => suggest(p)}>{p}<ChevronRight size={17} /></button>)}</div></div>;
}
function MarkdownText({children}: {children: string}) {
  return <Markdown components={{a: ({children}) => <span>{children}</span>, img: () => null}}>{children}</Markdown>;
}
function EvidenceCard({e, video, seek}: {e: Evidence; video: Video; seek: (n: number) => void}) {
  if (e.video_id !== video.video_id || !Number.isFinite(e.start_t) || e.start_t < 0 || e.end_t < e.start_t || e.end_t > video.duration + .01) return null;
  const current = e.source === 'transcript' ? (e.version_basis === 'text' ? video.transcript.text_version : video.transcript.search_version) : video.active_generation;
  const stale = e.stale_evidence || e.index_version !== current;
  const image = !stale && e.frame_id && e.source !== 'transcript' ? `/api/videos/${video.video_id}/frames/${encodeURIComponent(e.frame_id)}/image?index_version=${encodeURIComponent(e.index_version)}` : null;
  return <button className={`evidence ${stale ? 'stale' : ''}`} disabled={!!stale || !video.capabilities.playback} onClick={() => seek(e.start_t)}>
    {image && <img src={image} loading="lazy" alt="视频来源画面" onError={event => {event.currentTarget.hidden = true}} />}<span><strong><Play size={12} /> {time(e.start_t)}–{time(e.end_t)}</strong><small>{e.source === 'transcript' ? '语音片段' : '画面证据'}{stale && ' · 旧版来源，请重新提问'}</small><span>{e.text.replace(/^\[\w+\]\s*id=[^|]*\|\s*/, '')}</span></span>
  </button>;
}

export default function App() {
  const panels = useResizablePanels();
  const sidebar = useSidebar();
  const [config, setConfig] = useState<UIConfig | null>(null);
  const [videos, setVideos] = useState<Page<Video>>({items: [], total: 0, offset: 0, limit: 20});
  const [videoId, setVideoId] = useState(selectedFromURL);
  const [video, setVideo] = useState<Video | null>(null);
  const [trash, setTrash] = useState(false);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [dark, setDark] = useState(() => {try {return localStorage.getItem('evireel:theme') === 'dark'} catch {return false}});
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [uploadName, setUploadName] = useState('');
  const [job, setJob] = useState<Job | null>(null);
  const [study, setStudy] = useState<Study | null>(null);
  const [tab, setTab] = useState('notes');
  const [learningOpen, setLearningOpen] = useState(false);
  const [chapterIndex, setChapterIndex] = useState(0);
  const [selectedChapter, setSelectedChapter] = useState<Chapter | null>(null);
  const [unread, setUnread] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [cid, setCid] = useState('');
  const [showHistory, setShowHistory] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState(false);
  const [pendingText, setPendingText] = useState('');
  const [stage, setStage] = useState('');
  const [connection, setConnection] = useState('connecting');
  const [confirm, setConfirm] = useState<Video | null>(null);
  const [copied, setCopied] = useState('');
  const [downloading, setDownloading] = useState(false);
  const [mediaError, setMediaError] = useState('');
  const [playbackTime, setPlaybackTime] = useState(0);
  const [me, setMe] = useState<Me | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [uploadState, setUploadState] = useState<UploadState | null>(null);
  const [resumable, setResumable] = useState<UploadSession[]>([]);
  const uploader = useRef<ResumableUpload | null>(null);
  const resumeTarget = useRef<UploadSession | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const player = useRef<HTMLVideoElement>(null);
  const playerShell = useRef<HTMLDivElement>(null);
  const queuedSeek = useRef<number | null>(null);
  const uploadAbort = useRef<AbortController | null>(null);
  const channel = useRef<ChatChannel | null>(null);
  const queuedQuestion = useRef<ChatPayload | null>(null);
  const sendLock = useRef(false);
  const conversationRevision = useRef(0);
  const chatBody = useRef<HTMLDivElement>(null);
  const followBottom = useRef(true);
  const active = useRef({videoId, cid});
  const manuscript = useRef<Transcript[]>([]);
  const rememberManuscript = useCallback((items: Transcript[]) => {manuscript.current = items}, []);
  active.current = {videoId, cid};
  const ready = !!video && (video.chat?.available ?? (!isProcessing(video.status) && (video.capabilities.vision || video.capabilities.transcript_search)));
  const busy = !!video && isProcessing(video.status);
  const artifact = study?.artifact;
  const chapter = artifact?.content.chapters[chapterIndex];
  const refresh = useCallback(() => setRevision(v => v + 1), []);
  useEffect(() => {setChapterIndex(0); setSelectedChapter(null)}, [artifact?.artifact_id]);
  useEffect(() => {
    const body = chatBody.current;
    if (!body) return;
    if (followBottom.current) {body.scrollTop = body.scrollHeight; setUnread(false)}
    else setUnread(true);
  }, [messages, pending, stage]);

  const chooseVideo = useCallback((id: string) => {
    queuedQuestion.current = null; sendLock.current = false; conversationRevision.current++;
    const url = new URL(location.href); id ? url.searchParams.set('video', id) : url.searchParams.delete('video');
    history.pushState(null, '', url); setVideoId(id); setError('');
  }, []);
  useEffect(() => {const pop = () => setVideoId(selectedFromURL()); addEventListener('popstate', pop); return () => removeEventListener('popstate', pop)}, []);
  useEffect(() => {const timer = setTimeout(() => {setSearch(query); setOffset(0)}, 250); return () => clearTimeout(timer)}, [query]);
  useEffect(() => {
    const abort = new AbortController(); api<UIConfig>('/api/ui-config', {signal: abort.signal}).then(setConfig).catch(e => setError(errorText(e))); return () => abort.abort();
  }, []);
  const authRequired = !!config?.auth_required;
  const signedIn = !authRequired || !!me;
  useEffect(() => {
    if (!config) return;
    if (!config.auth_required) {setAuthChecked(true); return}
    api<Me>('/api/auth/me').then(setMe).catch(() => setMe(null)).finally(() => setAuthChecked(true));
    const expired = () => setMe(null);
    addEventListener('evireel:unauthenticated', expired);
    return () => removeEventListener('evireel:unauthenticated', expired);
  }, [config]);
  const loadResumable = useCallback(async () => {
    if (config?.upload_mode !== 'resumable' || !signedIn) return;
    try {setResumable((await api<{items: UploadSession[]}>('/api/uploads')).items)} catch { /* shown via other requests */ }
  }, [config, signedIn]);
  useEffect(() => {loadResumable()}, [loadResumable, revision]);
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    try {localStorage.setItem('evireel:theme', dark ? 'dark' : 'light')} catch { /* optional preference */ }
  }, [dark]);
  useEffect(() => {
    if (!signedIn) return;
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {const result = await api<Page<Video>>(`/api/videos?offset=${offset}&limit=20&trash=${trash}&q=${encodeURIComponent(search)}`, {signal: abort.signal});
        if (!abort.signal.aborted) {setVideos(result); timer = setTimeout(load, 4000)}
      } catch (e) {if (!abort.signal.aborted) {setError(errorText(e)); timer = setTimeout(load, 6000)}}
    }; load(); return () => {abort.abort(); clearTimeout(timer)};
  }, [offset, search, trash, revision, signedIn]);
  useEffect(() => {
    setDraft(''); sendLock.current = false; queuedQuestion.current = null; setVideo(null); setJob(null); setStudy(null); setCid(''); setMessages([]); setChapterIndex(0); setTab('notes'); setLearningOpen(false); setMediaError(''); queuedSeek.current = null;
    setConversations([]); setPlaybackTime(0); manuscript.current = [];
  }, [videoId, trash]);
  useEffect(() => {
    if (!videoId || trash || !signedIn) return;
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const detail = await api<Video>(`/api/videos/${videoId}`, {signal: abort.signal});
        if (abort.signal.aborted) return;
        if (detail.trashed_at !== null) {chooseVideo(''); refresh(); return}
        setVideo(detail);
        const [newStudy, newJob] = await Promise.all([
          api<Study>(`/api/videos/${videoId}/study`, {signal: abort.signal}),
          detail.latest_job_id ? api<Job>(`/api/jobs/${detail.latest_job_id}`, {signal: abort.signal}) : Promise.resolve(null),
        ]);
        if (abort.signal.aborted) return;
        setStudy(newStudy); setJob(newJob); timer = setTimeout(load, isProcessing(detail.status) || (newStudy.job && isProcessing(newStudy.job.status)) ? 1500 : 5000);
      } catch (e) {if (!abort.signal.aborted) {setError(errorText(e)); timer = setTimeout(load, 5000)}}
    }; load(); return () => {abort.abort(); clearTimeout(timer)};
  }, [videoId, trash, revision, chooseVideo, refresh, signedIn]);
  useEffect(() => {
    if (!videoId || trash || !signedIn) return;
    const abort = new AbortController();
    const selection = conversationRevision.current;
    api<Page<Conversation>>(`/api/conversations?video_id=${videoId}&limit=200`, {signal: abort.signal}).then(result => {
      if (abort.signal.aborted) return;
      setConversations(result.items);
      let saved = ''; try {saved = localStorage.getItem(`evireel:conversation:${videoId}`) || ''} catch { /* optional */ }
      if (selection === conversationRevision.current) setCid(result.items.find(c => c.conversation_id === saved)?.conversation_id || result.items[0]?.conversation_id || '');
    }).catch(e => {if (!abort.signal.aborted) setError(errorText(e))});
    return () => abort.abort();
  }, [videoId, trash, signedIn]);
  const loadMessages = useCallback(async (conversation: string, vid: string, signal?: AbortSignal) => {
    const all: Message[] = []; let after = 0;
    do {
      const page = await api<{items: Message[]; next_after: number | null}>(`/api/conversations/${conversation}/messages?after=${after}&limit=200`, {signal});
      all.push(...page.items); after = page.next_after || 0;
    } while (after && !signal?.aborted);
    if (!signal?.aborted && active.current.cid === conversation && active.current.videoId === vid) setMessages(all);
  }, []);
  useEffect(() => {
    setMessages([]); setPending(false); setPendingText(''); setStage('');
    sendLock.current = false;
    if (!cid || !videoId || trash || !signedIn) return;
    const abort = new AbortController();
    try {localStorage.setItem(`evireel:conversation:${videoId}`, cid)} catch { /* optional */ }
    loadMessages(cid, videoId, abort.signal).catch(e => {if (!abort.signal.aborted) setError(errorText(e))});
    const socket = new ChatChannel(videoId, cid, event => {
      if (active.current.cid !== cid || active.current.videoId !== videoId) return;
      if (event.type === 'final' || event.type === 'error') {
        sendLock.current = false;
        setPending(false); setPendingText(''); setStage('');
        if (event.type === 'error') setError(codeText(event.code));
        loadMessages(cid, videoId, abort.signal).catch(e => setError(errorText(e)));
      } else setStage(event.type === 'accepted' ? '问题已接收' : event.type === 'resolved' ? '正在查找视频内容' : '正在整理回答');
    }, setConnection);
    channel.current = socket;
    const saved = remembered(cid);
    if (saved && saved.video_id === videoId) {sendLock.current = true; setPending(true); setPendingText(saved.text); setStage('正在恢复上次请求'); socket.send(saved)}
    else if (queuedQuestion.current?.conversation_id === cid && queuedQuestion.current.video_id === videoId) {
      const payload = queuedQuestion.current; queuedQuestion.current = null;
      sendLock.current = true; setPending(true); setPendingText(payload.text); setStage('正在发送问题');
      socket.send(payload);
    }
    return () => {abort.abort(); socket.close(); if (channel.current === socket) channel.current = null};
  }, [cid, videoId, trash, loadMessages, signedIn]);
  useEffect(() => () => {uploadAbort.current?.abort(); uploader.current?.pause()}, []);
  useEffect(() => {if (notice) {const timer = setTimeout(() => setNotice(''), 5000); return () => clearTimeout(timer)}}, [notice]);

  async function startResumable(selected: File) {
    const target = resumeTarget.current; resumeTarget.current = null;
    if (target && (selected.size !== target.declared_size || selected.name !== target.original_name)) {
      setError(uploadFailureText('file_mismatch')); return;
    }
    setError(''); setTrash(false); chooseVideo(''); setUploadName(selected.name);
    const json = async <T,>(url: string, init: {method?: string; body?: unknown} = {}) => {
      try {return await api<T>(url, {method: init.method, body: init.body === undefined ? undefined : JSON.stringify(init.body)})}
      catch (e) {throw e instanceof ApiError ? new UploadError(e.code, e.message, e.status) : e}
    };
    const upload = new ResumableUpload(selected, browserTransport(json), setUploadState);
    uploader.current = upload;
    try {
      const session = await upload.start(target || undefined);
      if (session?.state === 'completed') {
        setUploadState(null); uploader.current = null; chooseVideo(session.video_id); refresh();
        if (session?.result?.index_error) setNotice('视频已校验，处理队列已满，可稍后点击“重新处理”。');
      }
    } catch (e) {
      // A wrong reselected file must not hide the resume prompt; other failures stay in the panel.
      if (e instanceof UploadError && e.code === 'file_mismatch') {setUploadState(null); uploader.current = null; setError(uploadFailureText(e.code))}
    }
    finally {if (file.current) file.current.value = ''; loadResumable()}
  }
  async function resumeUpload() {
    const upload = uploader.current; if (!upload) return;
    try {const session = await upload.resume(); if (session?.state === 'completed') {setUploadState(null); uploader.current = null; chooseVideo(session.video_id); refresh()}}
    catch { /* rendered */ }
  }
  async function cancelUpload() {
    const upload = uploader.current;
    if (upload && !['failed', 'completed'].includes(upload.state.phase)) {
      const outcome = await upload.cancel();
      if (outcome === 'unconfirmed') return;  // the panel shows it and offers retry / continue
      if (outcome === 'continuing') {
        setNotice('上传已在服务器端完成合并，无法取消，正在继续校验。');
        try {const session = await upload.wait(); if (session?.state === 'completed') {setUploadState(null); uploader.current = null; chooseVideo(session.video_id); refresh()}}
        catch { /* rendered */ }
        return;
      }
    }
    uploader.current = null; setUploadState(null); loadResumable();
  }
  async function discardSession(s: UploadSession) {
    try {await api(`/api/uploads/${s.upload_id}`, {method: 'DELETE'}); setNotice('已放弃该上传，预留的存储空间已释放。')}
    catch (e) {setError(errorText(e))}
    loadResumable();
  }
  function chooseResume(s: UploadSession) {resumeTarget.current = s; setNotice(`请重新选择文件“${s.original_name}”，系统会校验是否为同一文件。`); file.current?.click()}
  async function logout() {
    try {await post('/api/auth/logout')} catch { /* cookie may already be gone */ }
    setMe(null); setVideos({items: [], total: 0, offset: 0, limit: 20}); chooseVideo('');
  }

  async function handleUpload(selected?: File) {
    if (!selected || uploadProgress !== null || (uploadState && !['failed', 'cancelled'].includes(uploadState.phase))) return;
    if (config?.upload_mode === 'resumable') {
      const extension = '.' + selected.name.split('.').pop()?.toLowerCase();
      if (!config.extensions.includes(extension)) {setError(`支持格式：${config.extensions.join('、')}`); return}
      if (selected.size > config.max_upload_bytes) {setError(`视频超过 ${Math.round(config.max_upload_bytes / 1024 / 1024)} MB 上传上限。`); return}
      return startResumable(selected);
    }
    if (!config) {setError('正在读取上传限制，请稍后重试。'); return}
    const extension = '.' + selected.name.split('.').pop()?.toLowerCase();
    if (!config.extensions.includes(extension)) {setError(`支持格式：${config.extensions.join('、')}`); return}
    if (selected.size > config.max_upload_bytes) {setError(`视频超过 ${Math.round(config.max_upload_bytes / 1024 / 1024)} MB 上传上限。`); return}
    setError(''); setTrash(false); chooseVideo(''); setUploadName(selected.name); setUploadProgress(0);
    const abort = new AbortController(); uploadAbort.current = abort;
    try {const result = await upload(selected, setUploadProgress, abort.signal); chooseVideo(result.video_id); refresh()}
    catch (e) {setError(errorText(e))}
    finally {setUploadProgress(null); if (file.current) file.current.value = ''; uploadAbort.current = null}
  }
  function seek(target: number) {
    if (!player.current) return;
    const value = seekTime(target, player.current.duration);
    if (player.current.readyState < 1 || value === null) queuedSeek.current = target;
    else {player.current.currentTime = value; queuedSeek.current = null}
  }
  async function retry(kind: 'video' | 'transcript' = 'video') {
    if (!videoId) return;
    try {await post(`/api/videos/${videoId}/${kind === 'video' ? 'index' : 'transcripts/index'}`); refresh()}
    catch (e) {setError(errorText(e))}
  }
  async function generate() {
    if (!videoId) return;
    try {const result = await post<Job>(`/api/videos/${videoId}/study`, {request_id: requestId()}); setStudy(s => s ? {...s, job: result} : s); setNotice('已开始生成章节、笔记与课件。'); refresh()}
    catch (e) {setError(errorText(e))}
  }
  async function newConversation() {
    if (!ready) return;
    conversationRevision.current++; setDraft('');
    const vid = videoId;
    try {const c = await post<Conversation>('/api/conversations', {video_id: vid});
      if (active.current.videoId !== vid) return;
      setConversations(prev => [c, ...prev]); setCid(c.conversation_id); setShowHistory(false);
    } catch (e) {setError(errorText(e))}
  }
  async function send(text = draft, options: Pick<ChatPayload, 'action' | 'scope'> = {}, shortcut = false) {
    const permitted = options.action ? video?.chat?.actions[options.action] ?? ready : ready;
    if (!permitted || pending || sendLock.current || !text.trim() || !videoId) return;
    if (text.length > (config?.message_chars || 2000)) {setError('问题超过字数上限，请缩短后重试。'); return}
    sendLock.current = true; conversationRevision.current++; followBottom.current = true;
    setPending(true); setPendingText(text.trim()); setStage('正在发送问题'); setError('');
    const vid = videoId;
    const payload: ChatPayload = {type: 'text', video_id: vid, conversation_id: cid, request_id: requestId(), text: text.trim(), ...options};
    if (!cid) {
      try {const c = await post<Conversation>('/api/conversations', {video_id: vid});
        if (active.current.videoId !== vid) return;
        queuedQuestion.current = {...payload, conversation_id: c.conversation_id};
        setConversations(prev => [c, ...prev]); setCid(c.conversation_id); if (!shortcut) setDraft('');
      } catch (e) {if (active.current.videoId === vid) {sendLock.current = false; setPending(false); setError(errorText(e))}}
      return;
    }
    if (channel.current?.send(payload)) {if (!shortcut) setDraft('')}
    else {sendLock.current = false; setPending(false); setError('对话正在连接，请稍后重试。')}
  }
  async function feature(name: string) {
    if (!(name in actionMessages)) return;
    const action = name as ChatAction;
    await send(actionMessages[action], {action}, true);
  }
  function quickQuestion(text: string) {
    let scope: ChatScope | undefined;
    if (text === prompts[2] && video) {
      if (selectedChapter && artifact && !artifact.stale) {
        scope = {start_t: selectedChapter.start_t, end_t: selectedChapter.end_t};
        text = `${text}（章节：${selectedChapter.title}，${time(scope.start_t)}–${time(scope.end_t)}）`;
      } else if (player.current && player.current.readyState >= 1) {
        const now = player.current.currentTime;
        const current = manuscript.current.find(r => now >= r.start_t && now < r.end_t);
        scope = {start_t: current?.start_t ?? Math.max(0, now - 15), end_t: current?.end_t ?? Math.min(video.duration, now + 15)};
        text = `${text}（播放片段：${time(scope.start_t)}–${time(scope.end_t)}）`;
      }
      if (scope && video.chat?.version) scope.version = video.chat.version;
    }
    send(text, scope ? {scope} : {}, true);
  }
  async function download(url: string) {
    if (downloading) return;
    setDownloading(true);
    try {await downloadFile(url)} catch (e) {setError(errorText(e))} finally {setDownloading(false)}
  }
  async function move(v: Video, restore = false) {
    try {await post(`/api/videos/${v.video_id}/${restore ? 'restore' : 'trash'}`); if (v.video_id === videoId) chooseVideo(''); refresh(); setNotice(restore ? '视频已恢复。' : '视频已移入回收站。')}
    catch (e) {setError(errorText(e))}
  }
  async function purge() {
    if (!confirm) return;
    try {
      // 204: deleted now. 202: new reads are already refused; files are reclaimed once current playback/searches end.
      const result = await api<{status?: string} | undefined>(`/api/videos/${confirm.video_id}`, {method: 'DELETE'});
      if (confirm.video_id === videoId) chooseVideo('');
      setConfirm(null); refresh();
      setNotice(result?.status === 'deleting' ? '视频正在删除：已停止新的访问，正在进行的播放或检索结束后自动清理。' : '视频及关联记录已永久删除。')
    }
    catch (e) {setError(errorText(e))}
  }
  function selectChapter(c: Chapter, index: number) {setChapterIndex(index); setSelectedChapter(c); seek(c.start_t)}
  function selectLearning(next: string) {setLearningOpen(next !== tab || !learningOpen); setTab(next)}
  function showLearning(kind: string) {setTab(kind === 'transcript' ? 'transcript' : 'notes'); setLearningOpen(true); refresh()}
  const sourceLink = (format: string) => `/api/videos/${videoId}/study/artifacts/${artifact?.artifact_id}/export?format=${format}`;

  if (config?.auth_required && authChecked && !me) return <Login onLogin={value => {setMe(value); refresh()}} />;
  if (config?.auth_required && !authChecked) return <div className="login-page"><LoaderCircle className="spin" /></div>;
  return <div className="app">
    <aside className="sidebar" data-collapsed={!sidebar.expanded} data-mobile={sidebar.mobile} aria-label="视频导航">
      <div className="sidebar-header"><a className="brand" href="/" aria-label="EviReel 首页" onClick={e => {e.preventDefault(); setTrash(false); chooseVideo('')}}><span className="brand-mark"><Play fill="white" /></span><span className="sidebar-label">EviReel</span></a>
        <button ref={sidebar.toggleButton} className="sidebar-toggle" aria-label={sidebar.expanded ? '收起侧边栏' : '展开侧边栏'} aria-expanded={sidebar.expanded} aria-controls="sidebar-library" data-tooltip={sidebar.expanded ? '收起侧边栏' : '展开侧边栏'} title={sidebar.expanded ? '收起侧边栏' : '展开侧边栏'} onClick={sidebar.toggle}>{sidebar.expanded ? <PanelLeftClose size={19} /> : <PanelLeftOpen size={19} />}</button></div>
      <button className="primary upload-button" aria-label="上传视频" data-tooltip="上传视频" disabled={uploadProgress !== null || (!!uploadState && !['failed', 'cancelled'].includes(uploadState.phase)) || !config} onClick={() => {resumeTarget.current = null; file.current?.click()}}><Plus size={21} /><span className="sidebar-label">上传视频</span></button>
      <input ref={file} type="file" aria-label="选择视频文件" accept={config?.extensions.join(',') || 'video/*'} hidden onChange={e => handleUpload(e.target.files?.[0])} />
      <nav aria-label="视频分类"><button className={!trash ? 'active' : ''} aria-label="我的视频" aria-current={!trash ? 'page' : undefined} data-tooltip="我的视频" onClick={() => {sidebar.expand(); setTrash(false); setOffset(0)}}><VideoIcon /><span className="sidebar-label">我的视频</span><span className="sidebar-count">{!trash && videos.total}</span></button><button className={trash ? 'active' : ''} aria-label="回收站" aria-current={trash ? 'page' : undefined} data-tooltip="回收站" onClick={() => {sidebar.expand(); setTrash(true); chooseVideo(''); setOffset(0)}}><Trash2 /><span className="sidebar-label">回收站</span></button></nav>
      <div id="sidebar-library" className="sidebar-library" ref={sidebar.library} hidden={!sidebar.expanded}>
      <div className="library-title">{trash ? '已删除的视频' : '视频资料库'}<span>{videos.total}</span></div>
      <div className="video-list" ref={sidebar.list} onScroll={sidebar.rememberScroll}>{videos.items.map(v => <div key={v.video_id} className={`video-item ${videoId === v.video_id ? 'selected' : ''}`}>
        <button className="video-pick" disabled={trash || v.status === 'deleting'} onClick={() => {chooseVideo(v.video_id); sidebar.selected()}}><span className="video-thumb"><Play size={17} /></span><span><strong>{v.original_name}</strong><small>{time(v.duration)} · {v.status === 'deleting' ? '删除中' : isProcessing(v.status) ? '处理中' : v.status === 'failed' ? '处理失败' : trash ? '已移入回收站' : '视频资料'}</small></span></button>
        <div className="video-actions">{trash ? <><button onClick={() => move(v, true)}>恢复</button><button className="danger" onClick={() => setConfirm(v)} aria-label={`永久删除 ${v.original_name}`}><Trash2 size={14} /></button></> : <button onClick={() => move(v)} aria-label={`移入回收站 ${v.original_name}`}><Trash2 size={14} /></button>}</div>
      </div>)}{videos.total === 0 && <p className="library-empty">{search ? '没有匹配的视频' : trash ? '回收站为空' : '上传视频，开始积累知识'}</p>}</div>
      {videos.total > 20 && <div className="pagination"><button disabled={offset === 0} onClick={() => setOffset(n => Math.max(0, n - 20))}>上一页</button><span>{Math.floor(offset / 20) + 1}</span><button disabled={offset + 20 >= videos.total} onClick={() => setOffset(n => n + 20)}>下一页</button></div>}
      </div>
      <div className="workspace-badge" title="我的学习空间"><span>E</span><div className="sidebar-label">我的学习空间<small>内容沉淀，让学习发生</small></div></div>
    </aside>
    <div className="workspace"><header className="topbar"><label className="search"><Search size={20} /><input aria-label="搜索视频文件名" placeholder="搜索视频文件名…" value={query} onChange={e => setQuery(e.target.value)} />{query && <button aria-label="清除搜索" onClick={() => setQuery('')}><X size={16} /></button>}</label><div className="top-actions"><button aria-label={dark ? '切换浅色外观' : '切换深色外观'} onClick={() => setDark(v => !v)}>{dark ? <Moon /> : <Sun />}</button>{me ? <><span className="avatar" title={`${me.username} · ${me.workspace.name}`}>{me.username.slice(0, 1).toUpperCase()}</span><button aria-label="退出登录" title="退出登录" onClick={logout}><LogOut /></button></> : <span className="avatar" title="当前本地学习空间">E</span>}</div></header>
      {(error || notice) && <div className={`toast ${error ? 'error' : ''}`} role={error ? 'alert' : 'status'}><span>{error || notice}</span><button aria-label="关闭提示" onClick={() => {setError(''); setNotice('')}}><X size={16} /></button></div>}
      {!uploadState && !trash && <ResumeBanner sessions={resumable} choose={chooseResume} discard={discardSession} />}
      <div className="columns resizable-columns" ref={panels.container} style={panels.style} data-stacked={panels.stacked} data-dragging={panels.dragging}><main className="main-panel" id="learning-panel">
        {uploadState && !trash ? <UploadPanel name={uploadName} state={uploadState} pause={() => uploader.current?.pause()} resume={resumeUpload} cancel={cancelUpload} /> : trash ? <section className="empty-state"><div className="empty-icon"><Trash2 size={64} /></div><h1>回收站</h1><p>视频、笔记和对话会一起保留，恢复后即可继续学习。</p><p>选择左侧视频恢复，或永久删除。</p></section> : uploadProgress !== null ? <section className="empty-state processing"><div className="empty-icon"><VideoIcon size={70} /></div><span className="eyebrow">正在上传</span><h1>把知识带进你的学习空间</h1><p className="filename">{uploadName}</p><div className="progress"><span style={{width: `${uploadProgress}%`}} /></div><strong>{uploadProgress}%</strong><p>{uploadProgress === 100 ? '上传完成，正在检查视频…' : '正在传输视频文件…'}</p><button className="secondary" onClick={() => uploadAbort.current?.abort()}>取消上传</button></section> : busy ? <section className="empty-state processing"><div className="processing-orbit"><VideoIcon size={58} /><span /><LoaderCircle className="spin" size={98} /></div><span className="eyebrow">让内容转化为知识</span><h1>视频正在处理中</h1><p className="filename">{video?.original_name}</p><p>我们正在整理视频内容，完成后即可查看笔记并开始提问。</p><div className="indeterminate"><span /></div><small>可以离开此页面，处理会在后台继续。</small></section> : video ? <>
          <div className="video-heading"><div><h1>{video.original_name}</h1><p>{new Date(video.created_at * 1000).toLocaleDateString('zh-CN')} <span>{time(video.duration)}</span> <span>{artifact ? '已生成笔记' : '视频资料'}{video.capabilities.transcript && '、文稿'}</span></p></div><button className="icon-button" title="重新处理视频" aria-label="重新处理视频" onClick={() => retry()}><RefreshCw size={18} /></button></div>
          {['failed', 'interrupted', 'partial'].includes(video.status) && <div className="status-banner"><span>{video.status === 'partial' ? '部分能力未就绪，已完成的内容仍可使用。' : '上次视频处理未完成。'} {job?.error_code && `（${job.error_code}）`}</span><button onClick={() => retry()}>重新处理</button></div>}
          <div className="player-shell" ref={playerShell}>{video.capabilities.playback ? <video ref={player} key={video.video_id} src={video.content_url || undefined} controls playsInline preload="metadata" aria-label="视频播放器" onTimeUpdate={e => {
              const now = e.currentTarget.currentTime; setPlaybackTime(now);
              if (artifact && !artifact.stale) {const chapters = artifact.content.chapters; let index = 0; for (let i = 0; i < chapters.length; i++) {if (chapters[i].start_t <= now) index = i} setChapterIndex(index)}
            }} onLoadedMetadata={() => {if (queuedSeek.current !== null) seek(queuedSeek.current)}} onError={() => setMediaError('视频暂时无法播放，请重试；若文件损坏可重新处理。')} /> : <div className="player-placeholder"><VideoIcon size={48} /><p>播放文件尚未就绪</p><button onClick={() => retry()}>重新处理视频</button></div>}</div>
          <div className="play-tools"><span>{mediaError || '点击章节、文稿或回答来源，跳转到对应画面。'}</span><label>倍速 <select aria-label="播放倍速" defaultValue="1" onChange={e => {if (player.current) player.current.playbackRate = Number(e.target.value)}}>{[.5, 1, 1.25, 1.5, 2].map(n => <option key={n} value={n}>{n}×</option>)}</select></label><button title="全屏播放" aria-label="全屏播放" onClick={() => playerShell.current?.requestFullscreen().catch(() => setNotice('当前浏览器不支持全屏播放。'))}><Maximize size={16} /></button></div>
          <div className="content-tabs" role="group" aria-label="笔记和文稿"><button id="learning-notes-button" className={learningOpen && tab === 'notes' ? 'selected' : ''} aria-controls="learning-content" aria-expanded={learningOpen && tab === 'notes'} onClick={() => selectLearning('notes')}><BookOpen size={17} />章节与笔记</button><button id="learning-transcript-button" className={learningOpen && tab === 'transcript' ? 'selected' : ''} aria-controls="learning-content" aria-expanded={learningOpen && tab === 'transcript'} onClick={() => selectLearning('transcript')}><FileText size={17} />文稿</button><span />{learningOpen && <button title="刷新学习内容" aria-label="刷新学习内容" onClick={refresh}><RefreshCw size={15} /></button>}<button className={`learning-toggle ${learningOpen ? 'open' : ''}`} title={learningOpen ? '收起笔记和文稿' : '展开笔记和文稿'} aria-label={learningOpen ? '收起笔记和文稿' : '展开笔记和文稿'} aria-controls="learning-content" aria-expanded={learningOpen} onClick={() => setLearningOpen(value => !value)}><ChevronDown size={18} /></button></div>
          <section className="learning-content" id="learning-content" aria-labelledby={tab === 'notes' ? 'learning-notes-button' : 'learning-transcript-button'} hidden={!learningOpen}>
          {tab === 'notes' ? <>
            {study?.job && isProcessing(study.job.status) && <div className="status-banner"><LoaderCircle className="spin" size={17} />正在生成完整笔记… {study.job.total > 0 && `已完成 ${study.job.current}/${study.job.total} 批`}</div>}
            {study?.job && ['failed', 'interrupted'].includes(study.job.status) && <div className="status-banner warning"><span>{codeText(study.job.error_code)}</span><button onClick={generate}>重试生成</button></div>}
            {artifact && chapter ? <><div className="artifact-tools"><span>{artifact.stale ? '旧版笔记 · 原视频来源已更新' : '完整笔记已就绪'}</span><a href={sourceLink('md')} aria-disabled={downloading} onClick={e => {e.preventDefault(); download(sourceLink('md'))}}><Download size={14} />笔记</a><a href={sourceLink('pptx')} aria-disabled={downloading} onClick={e => {e.preventDefault(); download(sourceLink('pptx'))}}>PPTX</a><a href={sourceLink('pdf')} aria-disabled={downloading} onClick={e => {e.preventDefault(); download(sourceLink('pdf'))}}>PDF</a><button disabled={!!study?.job && isProcessing(study.job.status)} onClick={generate}>重新生成</button></div><div className="notes-layout"><nav className="chapters" aria-label="章节目录"><h3>章节目录</h3>{artifact.content.chapters.map((c, i) => <button key={i} className={chapterIndex === i ? 'selected' : ''} onClick={() => selectChapter(c, i)}><Play size={15} fill="currentColor" /><time>{time(c.start_t)}</time><span>{c.title}</span></button>)}</nav><article className="notes"><h3>笔记内容</h3><h2>{time(chapter.start_t)} {chapter.title}</h2><ul>{chapter.points.map((point, i) => <li key={i}>{point}</li>)}</ul><h4>本章来源</h4>{chapter.source_ids.map(id => {const e = artifact.content.evidence.find(r => r.evidence_id === id); return e && <button key={id} className="time-chip" disabled={artifact.stale || !video.capabilities.playback} onClick={() => seek(e.start_t)}><Play size={11} />{time(e.start_t)}–{time(e.end_t)}</button>})}<p className="fine-print">{artifact.content.warnings.join(' ')}</p></article></div></> : <div className="content-empty"><BookOpen size={33} /><h3>将视频整理成章节与笔记</h3><p>{study?.model_configured ? '根据视频真实内容生成完整笔记，并提供来源定位。' : '请先在服务器配置模型服务，再生成章节与笔记。'}</p><button className="primary" disabled={!!study?.job && isProcessing(study.job.status)} onClick={generate}>生成章节与笔记</button></div>}
          </> : null}
          <TranscriptReader key={video.video_id} video={video} visible={learningOpen && tab === 'transcript'} playbackTime={playbackTime} downloading={downloading} download={download} retry={() => retry('transcript')} seek={seek} onSegments={rememberManuscript} />
          </section>
        </> : videoId ? <div className="content-empty"><LoaderCircle className="spin" /><p>正在读取视频资料…</p><button onClick={refresh}>重试</button></div> : <section className="empty-state" onDragOver={e => e.preventDefault()} onDrop={e => {e.preventDefault(); handleUpload(e.dataTransfer.files[0])}}><div className="video-illustration" aria-hidden="true"><span className="float-note">━━<br />━━<br />━</span><div className="illustration-back" /><div className="illustration-screen"><Play size={72} fill="currentColor" /><span /></div><span className="float-chat">━━<br />━</span><i>✦</i></div><h1>还没有上传视频</h1><p>上传课程、会议、讲座或任何学习视频，<br />EviReel 将为你自动生成结构化笔记，并提供 AI 助手深度解读。</p><button className="primary first-upload" disabled={!config} onClick={() => file.current?.click()}><Plus />上传第一个视频</button><small>支持 {config?.extensions.map(e => e.slice(1).toUpperCase()).join('、') || '常见视频'} 格式 · 支持拖拽上传</small></section>}
      </main>
      <div className="panel-separator" {...panels.separator}><span aria-hidden="true" /></div>
      <aside className="assistant" id="assistant-panel"><header className="assistant-heading"><span className="assistant-symbol"><LockKeyhole size={23} /></span><h2>AI 助手</h2><button className="icon-button" title="历史对话" aria-label="历史对话" disabled={!videoId || busy} onClick={() => setShowHistory(v => !v)}><History size={17} /></button><button className="new-chat" disabled={!ready || pending} onClick={newConversation}><MessageSquarePlus size={16} />新对话</button></header>
        {showHistory && <div className="history-panel"><h3>历史对话</h3>{conversations.length ? conversations.map(c => <button className={cid === c.conversation_id ? 'selected' : ''} key={c.conversation_id} onClick={() => {setCid(c.conversation_id); setShowHistory(false)}}>{c.title || '新对话'}<small>{new Date(c.updated_at * 1000).toLocaleString('zh-CN')}</small></button>) : <p>还没有对话</p>}</div>}
        {messages.length > 0 && <details className="quick-menu" key={cid}><summary>快捷功能</summary><Welcome compact ready={ready} actions={video?.chat?.actions} pending={pending} hasVideo={!!video} action={feature} suggest={quickQuestion} /></details>}
        {video?.chat?.available && <div className="chat-basis">{video.chat.mode === 'text' ? '基于文稿/笔记问答 · 画面分析尚未就绪' : '基于当前视频的可用索引'}</div>}
        <div className="assistant-body" ref={chatBody} onScroll={() => {const b = chatBody.current; if (b) followBottom.current = b.scrollHeight - b.scrollTop - b.clientHeight < 80}}>{(messages.length === 0 && !pending) ? <Welcome ready={ready} actions={video?.chat?.actions} pending={pending} hasVideo={!!video} action={feature} suggest={quickQuestion} /> : <div className="messages">{messages.map(m => <div className={`message ${m.role}`} key={m.message_id}>{m.role === 'assistant' && <span className="answer-icon">✦</span>}<div><div className="message-text"><MarkdownText>{m.role === 'assistant' ? m.content.replace(/\[ev_[0-9a-f]{32}\]/g, '') : m.content}</MarkdownText></div>{m.role === 'assistant' && video && <><div className="answer-tools"><button aria-label="复制回答" onClick={() => navigator.clipboard.writeText(m.content).then(() => {setCopied(m.request_id); setTimeout(() => setCopied(''), 2000)}).catch(() => setNotice('当前浏览器无法复制，请手动选择文本。'))}>{copied === m.request_id ? <Check size={15} /> : <Copy size={15} />}</button><span>{m.result?.result?.material_mode === 'transcript' ? '基于语音文稿 · 未分析画面' : m.result?.result?.material_mode === 'events' ? '基于画面摘要' : '基于当前视频'}</span></div>{m.result?.result?.warnings?.length ? <p className="fine-print">内容可能存在识别误差或证据不足，请结合来源核对。</p> : null}<div className="evidence-list">{(m.result?.result?.evidence || []).filter(e => (m.result?.result?.citations || []).some(c => c.evidence_id === e.evidence_id)).map(e => <EvidenceCard key={e.evidence_id} e={e} video={video} seek={seek} />)}</div></>}{m.role === 'assistant' && m.result?.result?.resources?.map((resource, i) => <ResourceCard key={i} resource={resource} videoId={videoId} download={download} view={showLearning} seek={seek} refreshed={refresh} />)}{m.role === 'user' && ['failed', 'interrupted'].includes(m.status) && <div className="request-error">{codeText(m.error_code)}<button disabled={pending} onClick={() => send(m.content, {action: m.action, scope: m.scope}, true)}>重新提问</button></div>}</div></div>)}{pending && <><>{!messages.some(m => m.role === 'user' && m.content === pendingText && ['accepted', 'running'].includes(m.status)) && <div className="message user"><div className="message-text">{pendingText}</div></div>}</><div className="pending" role="status"><LoaderCircle size={17} className="spin" />{stage}</div></>}</div>}</div>
        {unread && <button className="latest-message" onClick={() => {followBottom.current = true; if (chatBody.current) chatBody.current.scrollTop = chatBody.current.scrollHeight; setUnread(false)}}>回到最新</button>}
        <div className="composer"><textarea id="question" aria-label="向 AI 助手提问" rows={2} disabled={!ready || pending} maxLength={config?.message_chars || 2000} placeholder={!video ? '请先上传视频，上传后即可开始提问…' : !ready ? codeText(video.chat?.reason) : pending ? 'AI 正在回答，可以稍后继续追问…' : '基于当前视频提问，获得更准确的答案…'} value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={e => {if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {e.preventDefault(); send()}}} /><div className="composer-tools"><button aria-label="附件支持范围" onClick={() => setNotice('请通过左侧上传视频。当前对话仅基于所选视频，不支持额外附件。')}><Paperclip size={21} /></button><span><Globe size={17} />基于当前视频<ChevronDown size={14} /></span><button className="send" aria-label="发送问题" disabled={!ready || pending || !draft.trim()} onClick={() => send()}><ArrowUp size={23} /></button></div></div>
        {ready && cid && <div className={`connection ${connection}`}>{connection === 'online' ? '对话已连接' : '连接恢复中，请求将在后台保留'}</div>}
      </aside></div>
    </div>
    {confirm && <div className="modal-backdrop" role="presentation"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="delete-title"><h2 id="delete-title">永久删除视频？</h2><p>{confirm.original_name}</p><p>视频文件、索引、文稿、笔记和关联对话将全部删除，无法恢复。</p><div><button className="secondary" onClick={() => setConfirm(null)} autoFocus>取消</button><button className="danger primary" onClick={purge}>永久删除</button></div></section></div>}
  </div>;
}
