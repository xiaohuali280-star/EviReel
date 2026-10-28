import {useState, type FormEvent} from 'react';
import {Play} from 'lucide-react';
import {errorText, post} from './api';

export interface Me {user_id: string; username: string; workspace: {workspace_id: string; name: string; quota_bytes: number; used_bytes: number; reserved_bytes: number}}

export function Login({onLogin}: {onLogin: (me: Me) => void}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (pending) return;
    setPending(true); setError('');
    try {onLogin(await post<Me>('/api/auth/login', {username, password}))}
    catch (err) {setError(errorText(err))}
    finally {setPending(false)}
  }
  return <div className="login-page"><form className="login-card" onSubmit={submit} aria-label="登录">
    <span className="brand-mark"><Play fill="white" /></span>
    <h1>登录 EviReel</h1>
    <p>视频、对话和上传会话按学习空间隔离。</p>
    <label>用户名<input autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required minLength={3} /></label>
    <label>密码<input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></label>
    {error && <p className="upload-error" role="alert">{error}</p>}
    <button className="primary" disabled={pending}>{pending ? '正在登录…' : '登录'}</button>
  </form></div>;
}
