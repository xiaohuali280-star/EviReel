import {test, expect} from '@playwright/test';

test('all eight shortcuts auto-send, keep drafts, expose real resources and work without embeddings', async ({page, request}) => {
  const media = await (await request.get('/__test__/media')).body();
  const upload = await (await request.post('/api/videos', {multipart: {video: {name: 'chat-actions.mp4', mimeType: 'video/mp4', buffer: media}}})).json();
  const vid = upload.video_id;
  await expect.poll(async () => (await (await request.get(`/api/videos/${vid}/study`)).json()).artifact?.artifact_id, {timeout: 30000}).toBeTruthy();
  await request.post(`/__test__/text-mode/${vid}`);
  await page.goto(`/?video=${vid}`);
  await expect(page.getByLabel('向 AI 助手提问')).toBeEnabled();
  await expect(page.getByText('基于文稿/笔记问答 · 画面分析尚未就绪')).toBeVisible();
  await page.getByLabel('向 AI 助手提问').fill('保留这条草稿');
  await page.getByRole('button', {name: '自动生成笔记', exact: false}).click();
  await expect(page.locator('.message.user')).toHaveCount(1);
  await expect(page.locator('.resource-card')).toHaveCount(1);
  await expect(page.getByLabel('向 AI 助手提问')).toHaveValue('保留这条草稿');
  await expect(page.locator('.resource-card').getByRole('button', {name: 'Markdown', exact: true})).toBeVisible();
  await page.screenshot({path: 'test-results/CHAT_NOTES_CARD.png', fullPage: true});
  const features = ['AI 看视频', '生成文稿', '生成课件'];
  for (const [index, name] of features.entries()) {
    await page.locator('.quick-menu summary').click();
    await page.locator('.quick-menu').getByRole('button', {name, exact: false}).click();
    await expect(page.locator('.message.user')).toHaveCount(index + 2);
    await expect(page.locator('.pending')).toHaveCount(0);
    // The menu stays open; collapse it before the next loop.
    await page.locator('.quick-menu summary').click();
  }
  await expect(page.getByRole('button', {name: 'TXT', exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'SRT', exact: true})).toBeVisible();
  const prompts = ['这段视频的主要内容是什么？', '帮我总结这节课的重点', '这部分的要点有什么好处？', '有哪些值得记录的知识点？'];
  for (const [index, text] of prompts.entries()) {
    await page.locator('.quick-menu summary').click();
    await page.locator('.quick-menu').getByRole('button', {name: text, exact: false}).click();
    await expect(page.locator('.message.user')).toHaveCount(index + 5);
    await expect(page.locator('.pending')).toHaveCount(0);
    await page.locator('.quick-menu summary').click();
  }
  await expect(page.getByLabel('向 AI 助手提问')).toHaveValue('保留这条草稿');
  const cid = (await (await request.get(`/api/conversations?video_id=${vid}`)).json()).items[0].conversation_id;
  const messages = (await (await request.get(`/api/conversations/${cid}/messages`)).json()).items;
  expect(messages.filter((m: {role: string}) => m.role === 'user')).toHaveLength(8);
  expect(new Set(messages.filter((m: {role: string}) => m.role === 'user').map((m: {request_id: string}) => m.request_id)).size).toBe(8);
  await page.reload();
  await expect(page.locator('.message.user')).toHaveCount(8);
  await expect(page.locator('.resource-card')).toHaveCount(3);
  await page.screenshot({path: 'test-results/CHAT_ACTIONS_READY.png', fullPage: true});
  await page.setViewportSize({width: 390, height: 900});
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
