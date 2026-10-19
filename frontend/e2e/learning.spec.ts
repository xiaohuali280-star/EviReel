import {test, expect} from '@playwright/test';

test('real upload, processing, notes, media seek, recoverable chat, exports and recycle bin', async ({page, request}) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  await expect(page.getByRole('heading', {name: '还没有上传视频'})).toBeVisible();
  await expect(page.getByRole('heading', {name: '上传视频后，开始和 AI 助手对话'})).toBeVisible();
  await page.screenshot({path: 'test-results/P5_EMPTY.png', fullPage: true});
  const media = await request.get('/__test__/media');
  await page.getByLabel('选择视频文件').setInputFiles({name: '009 图的应用.mp4', mimeType: 'video/mp4', buffer: await media.body()});
  await expect(page.getByRole('heading', {name: '视频正在处理中'})).toBeVisible();
  await expect(page.getByRole('heading', {name: '上传视频后，开始和 AI 助手对话'})).toBeVisible();
  await expect(page.getByLabel('向 AI 助手提问')).toBeDisabled();
  await page.screenshot({path: 'test-results/P5_PROCESSING.png', fullPage: true});
  await expect(page.getByRole('heading', {name: '笔记内容', exact: true})).toBeVisible({timeout: 30000});
  const vid = new URL(page.url()).searchParams.get('video')!;
  const detail = await (await request.get(`/api/videos/${vid}`)).json();
  const range = await request.get(`/api/videos/${vid}/content`, {headers: {Range: 'bytes=0-99'}});
  expect(range.status()).toBe(206);
  await page.locator('.chapters button').nth(1).click();
  await expect.poll(() => page.locator('video').evaluate((v: HTMLVideoElement) => v.currentTime)).toBeGreaterThan(3);
  await page.getByLabel('播放倍速').selectOption('1.5');
  expect(await page.locator('video').evaluate((v: HTMLVideoElement) => v.playbackRate)).toBe(1.5);
  // Native HTML5 controls are real, including audio and playback.
  await page.locator('video').evaluate((v: HTMLVideoElement) => {v.volume = .4; return v.play()});
  await expect.poll(() => page.locator('video').evaluate((v: HTMLVideoElement) => v.paused)).toBe(false);
  await page.locator('video').evaluate((v: HTMLVideoElement) => v.pause());
  await page.getByRole('button', {name: '文稿', exact: true}).click();
  await expect(page.locator('.transcript-line')).toHaveCount(2);
  const downloadWait = page.waitForEvent('download'); await page.getByRole('link', {name: 'SRT', exact: true}).click();
  expect((await downloadWait).suggestedFilename()).toMatch(/\.srt$/);
  await page.getByRole('button', {name: '章节与笔记', exact: true}).click();
  const pdfWait = page.waitForEvent('download'); await page.getByRole('link', {name: 'PDF', exact: true}).click();
  expect((await pdfWait).suggestedFilename()).toMatch(/\.pdf$/);
  const pptxWait = page.waitForEvent('download'); await page.getByRole('link', {name: 'PPTX', exact: true}).click();
  expect((await pptxWait).suggestedFilename()).toMatch(/\.pptx$/);
  const beforeDownloadError = page.url();
  await page.route('**/study/artifacts/*/export?format=pdf', route => route.fulfill({status: 422, contentType: 'application/json', body: JSON.stringify({error: {code: 'export_budget_exceeded', message: 'test page budget'}})}));
  await page.getByRole('link', {name: 'PDF', exact: true}).click();
  await expect(page.getByRole('alert')).toContainText('页数上限');
  expect(page.url()).toBe(beforeDownloadError);
  await page.getByLabel('关闭提示').click();
  await page.unroute('**/study/artifacts/*/export?format=pdf');
  await page.getByLabel('向 AI 助手提问').fill('生成树的性质是什么？');
  await page.getByRole('button', {name: '发送问题', exact: true}).click();
  await expect(page.locator('.pending')).toBeVisible();
  // Reload while the server owns an accepted request. The same request ID is
  // recovered; the model execution is not restarted by the browser.
  await page.reload();
  await expect(page.locator('.message.assistant')).toHaveCount(1, {timeout: 15000});
  await expect(page.locator('.evidence').first()).toBeVisible();
  const cid = await page.evaluate(vid => localStorage.getItem(`evireel:conversation:${vid}`), vid);
  const history = await (await request.get(`/api/conversations/${cid}/messages`)).json();
  expect(history.items).toHaveLength(2);
  await page.getByLabel('向 AI 助手提问').fill('这个概念有什么性质？');
  await page.getByRole('button', {name: '发送问题', exact: true}).click();
  await expect(page.locator('.message.assistant')).toHaveCount(2, {timeout: 15000});
  await page.screenshot({path: 'test-results/P5_LEARNING.png', fullPage: true});
  await page.getByLabel('向 AI 助手提问').fill('测试失败');
  await page.getByRole('button', {name: '发送问题', exact: true}).click();
  await expect(page.locator('.request-error')).toBeVisible({timeout: 10000});
  await page.getByLabel('关闭提示').click();
  await page.getByRole('button', {name: '新对话', exact: true}).click();
  await expect(page.locator('.message')).toHaveCount(0);
  await page.getByLabel('历史对话').click();
  await expect(page.locator('.history-panel button')).toHaveCount(2);
  await page.getByLabel('历史对话').click();
  for (const width of [390, 820, 1440]) {
    await page.setViewportSize({width, height: 900});
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({width: 1440, height: 1000});
  await page.getByLabel(`移入回收站 ${detail.original_name}`).click();
  await expect(page.getByRole('heading', {name: '还没有上传视频'})).toBeVisible();
  await page.getByRole('button', {name: '回收站', exact: true}).click();
  await page.getByRole('button', {name: '恢复', exact: true}).click();
  await page.getByRole('button', {name: /我的视频/}).click();
  await page.getByRole('button', {name: /009 图的应用.mp4/}).first().click();
  await expect(page.getByRole('heading', {name: '笔记内容', exact: true})).toBeVisible();
  await page.getByLabel(`移入回收站 ${detail.original_name}`).click();
  await page.getByRole('button', {name: '回收站', exact: true}).click();
  await page.getByLabel(`永久删除 ${detail.original_name}`).click();
  await page.getByRole('button', {name: '永久删除', exact: true}).click();
  await expect(page.getByText('回收站为空', {exact: true})).toBeVisible();
  expect(errors).toEqual([]);
});

test('video switches isolate late answers and offline recovery merges the same request', async ({page, request, context}) => {
  const media = await (await request.get('/__test__/media')).body();
  async function ingest(name: string) {
    const result = await (await request.post('/api/videos', {multipart: {video: {name, mimeType: 'video/mp4', buffer: media}}})).json();
    await expect.poll(async () => (await (await request.get(`/api/jobs/${result.job_id}`)).json()).status, {timeout: 30000}).toMatch(/succeeded|partial/);
    return result.video_id as string;
  }
  const a = await ingest('隔离 A.mp4'); const b = await ingest('隔离 B.mp4');
  await page.goto(`/?video=${a}`);
  await expect(page.getByLabel('向 AI 助手提问')).toBeEnabled();
  await page.getByLabel('向 AI 助手提问').fill('生成树有哪些性质？');
  await page.getByRole('button', {name: '发送问题', exact: true}).click();
  await expect(page.locator('.pending')).toBeVisible();
  const saved = await page.evaluate(() => {
    const key = Object.keys(localStorage).find(k => k.startsWith('evireel:pending:'))!;
    return JSON.parse(localStorage.getItem(key)!);
  });
  await expect.poll(async () => (await request.get(`/api/conversations/${saved.conversation_id}/requests/${saved.request_id}`)).status()).toBe(200);
  await page.goto(`/?video=${b}`);
  await expect(page.getByRole('heading', {name: '隔离 B.mp4', exact: true})).toBeVisible();
  await expect.poll(async () => (await (await request.get(`/api/conversations/${saved.conversation_id}/requests/${saved.request_id}`)).json()).status).toBe('completed');
  await expect(page.locator('.message.assistant')).toHaveCount(0);
  await page.goto(`/?video=${a}`);
  await expect(page.locator('.message.assistant')).toHaveCount(1);
  await page.getByLabel('向 AI 助手提问').fill('生成树的定义是什么？');
  await page.getByRole('button', {name: '发送问题', exact: true}).click();
  await expect(page.locator('.pending')).toBeVisible();
  const second = await page.evaluate(cid => JSON.parse(localStorage.getItem(`evireel:pending:${cid}`)!), saved.conversation_id);
  await expect.poll(async () => (await request.get(`/api/conversations/${saved.conversation_id}/requests/${second.request_id}`)).status()).toBe(200);
  await context.setOffline(true);
  await expect.poll(async () => (await (await request.get(`/api/conversations/${saved.conversation_id}/requests/${second.request_id}`)).json()).status).toBe('completed');
  await context.setOffline(false);
  await expect(page.locator('.message.assistant')).toHaveCount(2, {timeout: 15000});
  const messages = await (await request.get(`/api/conversations/${saved.conversation_id}/messages`)).json();
  expect(messages.items).toHaveLength(4);
  expect(new Set(messages.items.map((m: {request_id: string}) => m.request_id)).size).toBe(2);
  const rebuild = await (await request.post(`/api/videos/${a}/index`)).json();
  await expect.poll(async () => (await (await request.get(`/api/jobs/${rebuild.job_id}`)).json()).status, {timeout: 30000}).toMatch(/succeeded|partial/);
  await page.reload();
  await expect(page.locator('.evidence.stale').first()).toBeDisabled();
  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: 'test-results/P5_MOBILE.png', fullPage: true});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('unsupported uploads fail visibly without registering a video', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('button', {name: '上传视频', exact: true})).toBeEnabled();
  await page.getByLabel('选择视频文件').setInputFiles({name: 'bad.exe', mimeType: 'application/octet-stream', buffer: Buffer.from('bad')});
  await expect(page.getByRole('alert')).toContainText('支持格式');
  await expect(page.getByRole('heading', {name: '还没有上传视频', exact: true})).toBeVisible();
});
