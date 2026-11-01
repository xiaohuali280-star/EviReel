import {useRef, useState, type FormEvent} from 'react';
import {Play} from 'lucide-react';
import {errorText, post} from './api';

export interface Me {user_id: string; username: string; workspace: {workspace_id: string; name: string; quota_bytes: number; used_bytes: number; reserved_bytes: number}}

/** Platform-mode sign-in and self-service sign-up (when the server allows it: ui-config `registration`).
 * A new account owns a new learning space; the server signs it in at once. */
export function Login({onLogin, registration = false}: {onLogin: (me: Me) => void; registration?: boolean}) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const usernameField = useRef<HTMLInputElement>(null);
  const signingUp = mode === 'register';
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (pending) return;
    setPending(true); setError('');
    try {onLogin(await post<Me>(signingUp ? '/api/auth/register' : '/api/auth/login', {username, password}))}
    catch (err) {setError(errorText(err))}
    finally {setPending(false)}
  }
  function switchTo(next: 'login' | 'register') {setMode(next); setError(''); setPassword('')}
  return <div className="login-page">
    <header className="auth-top"><div className="brand"><span className="brand-mark"><Play fill="white" /></span>EviReel</div></header>
    <section className="auth-hero">
      <h1>AI 课程视频学习助手</h1>
      <p className="auth-lead">一键生成学习资料，AI 解答可溯源、可核对</p>
      <p>上传课程视频，自动整理文稿、章节笔记与课件；<br />每个回答都附带可跳转的视频证据。</p>
      <button type="button" className="auth-cta" onClick={() => usernameField.current?.focus()}>开始使用</button>
    </section>
    <form className="login-card" onSubmit={submit} aria-label={signingUp ? '注册' : '登录'}>
      <h1>{signingUp ? '欢迎注册' : '登录 EviReel'}</h1>
      <label>用户名<input ref={usernameField} autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required
        minLength={3} maxLength={64} pattern={signingUp ? '[A-Za-z0-9_.\\-]{3,64}' : undefined}
        title={signingUp ? '3–64 位字母、数字或 _ . -' : undefined} /></label>
      <label>密码<input type="password" autoComplete={signingUp ? 'new-password' : 'current-password'} value={password}
        onChange={e => setPassword(e.target.value)} required minLength={signingUp ? 10 : undefined} maxLength={256} /></label>
      {signingUp && <p className="auth-hint">3–64 位字母、数字或 _ . -；密码至少 10 位</p>}
      {error && <p className="upload-error" role="alert">{error}</p>}
      <button className="primary" disabled={pending}>{pending ? (signingUp ? '正在注册…' : '正在登录…') : (signingUp ? '注册' : '登录')}</button>
      {registration && <p className="auth-switch">{signingUp
        ? <>已有账号？<button type="button" onClick={() => switchTo('login')}>去登录</button></>
        : <>还没有账号？<button type="button" onClick={() => switchTo('register')}>立即注册</button></>}</p>}
    </form>
  </div>;
}
