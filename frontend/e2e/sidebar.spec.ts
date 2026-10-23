import {expect, test} from '@playwright/test';

const key = 'evireel:sidebar-collapsed';

test('sidebar remembers the icon rail and keeps navigation, focus and panel preferences usable', async ({page}) => {
  await page.goto('/');
  const sidebar = page.getByRole('complementary', {name: '视频导航'});
  const divider = page.getByRole('separator');
  await divider.focus(); await page.keyboard.press('ArrowLeft');
  const ratio = await divider.getAttribute('aria-valuenow');
  const search = page.getByLabel('搜索视频文件名'); await search.fill('保留搜索');
  await page.getByRole('button', {name: '收起侧边栏', exact: true}).click();
  await expect.poll(() => sidebar.evaluate(node => node.getBoundingClientRect().width)).toBe(72);
  await expect(page.locator('#sidebar-library')).toBeHidden();
  await expect(page.getByRole('button', {name: '上传视频', exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: '我的视频', exact: true})).toHaveAttribute('aria-current', 'page');
  await expect(search).toHaveValue('保留搜索');
  await expect(divider).toHaveAttribute('aria-valuenow', ratio!);
  const expand = page.getByRole('button', {name: '展开侧边栏', exact: true});
  await expand.focus();
  await page.keyboard.press('Tab'); await page.keyboard.press('Shift+Tab');
  await expect(expand).toHaveAttribute('aria-expanded', 'false');
  expect(await expand.evaluate(node => getComputedStyle(node, '::after').content)).toContain('展开侧边栏');
  const chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', {name: '上传视频', exact: true}).click();
  expect((await chooser).isMultiple()).toBe(false);
  await page.reload();
  await expect(page.getByRole('button', {name: '展开侧边栏', exact: true})).toBeVisible();
  await expect(divider).toHaveAttribute('aria-valuenow', ratio!);
  await page.getByRole('button', {name: '我的视频', exact: true}).click();
  await expect(page.locator('#sidebar-library')).toBeVisible();
  await page.getByRole('button', {name: '收起侧边栏', exact: true}).focus();
  await page.keyboard.press('Space');
  await page.getByRole('button', {name: '回收站', exact: true}).click();
  await expect(page.locator('#sidebar-library')).toBeVisible();
  await expect(page.getByRole('heading', {name: '回收站', exact: true})).toBeVisible();
  await page.getByRole('button', {name: '收起侧边栏', exact: true}).click();
  for (const width of [1920, 1024, 820]) {
    await page.setViewportSize({width, height: 1000});
    await expect.poll(() => sidebar.evaluate(node => node.getBoundingClientRect().width)).toBe(72);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({width: 1440, height: 1000});
  await page.getByRole('button', {name: '切换深色外观'}).click();
  await page.screenshot({path: 'test-results/SIDEBAR_DARK_RAIL.png', fullPage: true});
});

test('collapse during processing and chat keeps video, draft, request and library position', async ({page, request}) => {
  const media = await (await request.get('/__test__/media')).body();
  const upload = await (await request.post('/api/videos', {multipart: {video: {name: '侧栏验收.mp4', mimeType: 'video/mp4', buffer: media}}})).json();
  await page.goto(`/?video=${upload.video_id}`);
  await expect(page.getByRole('heading', {name: '视频正在处理中'})).toBeVisible();
  await page.getByRole('button', {name: '收起侧边栏', exact: true}).click();
  await expect(page.getByRole('heading', {name: '笔记内容', exact: true})).toBeVisible({timeout: 30000});
  expect(new URL(page.url()).searchParams.get('video')).toBe(upload.video_id);
  const player = page.getByLabel('视频播放器');
  await player.evaluate(async (node: HTMLVideoElement) => {node.dataset.identity = 'same'; node.muted = true; node.playbackRate = .5; await node.play(); node.currentTime = 1});
  await page.getByLabel('向 AI 助手提问').fill('侧栏变化后保留的草稿');
  const sent: string[] = [];
  // The first chat creates its connection after the shortcut is selected.
  page.on('websocket', ws => ws.on('framesent', event => {try {const f = JSON.parse(String(event.payload)); if (f.type === 'text') sent.push(f.request_id)} catch { /* ping */ }}));
  await page.getByRole('button', {name: 'AI 看视频', exact: false}).click();
  await expect(page.locator('.pending')).toBeVisible();
  await page.getByRole('button', {name: '展开侧边栏', exact: true}).click();
  await expect(player).toHaveAttribute('data-identity', 'same');
  expect(await player.evaluate((node: HTMLVideoElement) => node.currentTime)).toBeGreaterThanOrEqual(1);
  expect(await player.evaluate((node: HTMLVideoElement) => node.paused)).toBe(false);
  await expect(page.getByLabel('向 AI 助手提问')).toHaveValue('侧栏变化后保留的草稿');
  await expect(page.locator('.message.assistant')).toHaveCount(1);
  expect(new Set(sent).size).toBe(1);
  const list = page.locator('.video-list');
  // Stretch the existing list to exercise its actual scroll container, without
  // registering unrelated videos or sending private material to a model.
  await list.evaluate(node => {node.style.maxHeight = '30px'; node.scrollTop = node.scrollHeight});
  const before = await list.evaluate(node => node.scrollTop);
  expect(before).toBeGreaterThan(0);
  await page.getByRole('button', {name: '收起侧边栏', exact: true}).click();
  await page.getByRole('button', {name: '展开侧边栏', exact: true}).click();
  await expect.poll(() => page.locator('.sidebar').evaluate(node => node.getBoundingClientRect().width)).toBe(224);
  await expect.poll(() => list.evaluate(node => node.scrollTop)).toBe(before);
  await page.screenshot({path: 'test-results/SIDEBAR_LEARNING.png', fullPage: true});
});

test('mobile toolbar has independent state, restores focus and exposes video selection', async ({page, request}) => {
  const media = await (await request.get('/__test__/media')).body();
  const upload = await (await request.post('/api/videos', {multipart: {video: {name: '手机侧栏.mp4', mimeType: 'video/mp4', buffer: media}}})).json();
  await page.goto('/');
  await page.getByRole('button', {name: '收起侧边栏', exact: true}).click();
  await page.setViewportSize({width: 390, height: 900});
  await expect(page.locator('#sidebar-library')).toBeHidden();
  await expect.poll(() => page.locator('.sidebar').evaluate(node => node.getBoundingClientRect().height)).toBeLessThanOrEqual(80);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', {name: '我的视频', exact: true}).click();
  await expect(page.locator('#sidebar-library')).toBeVisible();
  await page.getByRole('button', {name: /手机侧栏.mp4/}).first().click();
  await expect(page.locator('#sidebar-library')).toBeHidden();
  expect(new URL(page.url()).searchParams.get('video')).toBe(upload.video_id);
  await expect(page.getByRole('button', {name: '展开侧边栏', exact: true})).toBeFocused();
  await page.getByRole('button', {name: '展开侧边栏', exact: true}).click();
  // Moving to a desktop rail must not leave focus in the newly hidden list.
  await page.getByRole('button', {name: /手机侧栏.mp4/}).first().focus();
  await page.setViewportSize({width: 1440, height: 1000});
  await expect(page.locator('#sidebar-library')).toBeHidden();
  await expect(page.getByRole('button', {name: '展开侧边栏', exact: true})).toBeFocused();
  expect(await page.evaluate(k => localStorage.getItem(k), key)).toBe('true');
  await page.setViewportSize({width: 390, height: 900});
  await page.getByRole('button', {name: '收起侧边栏', exact: true}).click();
  await expect.poll(() => page.locator('.sidebar').evaluate(node => node.getBoundingClientRect().height)).toBeLessThanOrEqual(80);
  await page.screenshot({path: 'test-results/SIDEBAR_MOBILE.png', fullPage: true});
});

test('invalid or inaccessible sidebar preferences keep controls usable', async ({page}) => {
  await page.goto('/'); await page.evaluate(k => localStorage.setItem(k, 'invalid'), key); await page.reload();
  await expect(page.getByRole('button', {name: '收起侧边栏', exact: true})).toBeVisible();
  await page.addInitScript(k => {
    const get = Storage.prototype.getItem, set = Storage.prototype.setItem;
    Storage.prototype.getItem = function(name) {if (name === k) throw new DOMException('Test denied', 'SecurityError'); return get.call(this, name)};
    Storage.prototype.setItem = function(name, value) {if (name === k) throw new DOMException('Test denied', 'QuotaExceededError'); set.call(this, name, value)};
  }, key);
  await page.reload();
  await page.getByRole('button', {name: '收起侧边栏', exact: true}).click();
  await expect(page.getByRole('button', {name: '展开侧边栏', exact: true})).toBeVisible();
  await page.getByRole('button', {name: '展开侧边栏', exact: true}).click();
  await expect(page.locator('#sidebar-library')).toBeVisible();
});
