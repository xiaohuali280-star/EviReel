import {expect, test} from '@playwright/test';
import type {Page} from '@playwright/test';

const preferenceKey = 'evireel:panel-ratio';
const divider = (page: Page) => page.getByRole('separator', {name: '调整学习区与 AI 助手宽度'});
async function proportion(page: Page) {return Number(await divider(page).getAttribute('aria-valuenow'))}
async function dragBy(page: Page, dx: number, release = true) {
  const box = (await divider(page).boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + 80);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + dx, box.y + 100, {steps: 5});
  if (release) await page.mouse.up();
}
async function noOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}

test('divider drags both ways, clamps, cancels, resets, persists and adapts to viewport', async ({page}) => {
  await page.goto('/');
  await expect(divider(page)).toBeVisible();
  await expect.poll(() => proportion(page)).toBe(65);
  await divider(page).hover();
  await expect(divider(page)).toHaveCSS('cursor', 'col-resize');
  await dragBy(page, -130);
  const left = await proportion(page);
  expect(left).toBeLessThan(60);
  await dragBy(page, 100);
  expect(await proportion(page)).toBeGreaterThan(left);
  const saved = await proportion(page);
  await page.reload();
  await expect.poll(() => proportion(page)).toBe(saved);
  await dragBy(page, -150, false);
  await page.keyboard.press('Escape'); await page.mouse.up();
  await expect.poll(() => proportion(page)).toBe(saved);
  await expect(page.locator('html')).not.toHaveClass(/is-resizing-panels/);
  await divider(page).evaluate(node => node.addEventListener('pointerdown', event => node.setAttribute('data-test-pointer', String((event as PointerEvent).pointerId)), {once: true}));
  await dragBy(page, -100, false);
  await divider(page).dispatchEvent('pointercancel', {pointerId: Number(await divider(page).getAttribute('data-test-pointer'))});
  await page.mouse.up();
  await expect.poll(() => proportion(page)).toBe(saved);
  await expect(page.locator('html')).not.toHaveClass(/is-resizing-panels/);
  await dragBy(page, 100, false);
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  await page.mouse.up();
  await expect.poll(() => proportion(page)).toBe(saved);
  await divider(page).focus();
  await page.keyboard.press('Home');
  await expect(divider(page)).toHaveAttribute('aria-valuenow', (await divider(page).getAttribute('aria-valuemin'))!);
  const minimum = await proportion(page);
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('Shift+ArrowRight');
  expect(await proportion(page)).toBeCloseTo(minimum + 7, 1);
  await page.keyboard.press('End');
  await expect(divider(page)).toHaveAttribute('aria-valuenow', (await divider(page).getAttribute('aria-valuemax'))!);
  const sizes = await page.evaluate(() => ({main: document.querySelector('.main-panel')!.getBoundingClientRect().width, chat: document.querySelector('.assistant')!.getBoundingClientRect().width}));
  expect(sizes.main).toBeGreaterThanOrEqual(419.5); expect(sizes.chat).toBeGreaterThanOrEqual(319.5);
  await noOverflow(page);
  await page.keyboard.press('Enter');
  await expect.poll(() => proportion(page)).toBe(65);
  await dragBy(page, -100); await divider(page).dblclick();
  await expect.poll(() => proportion(page)).toBe(65);
  await page.setViewportSize({width: 1920, height: 1080});
  await divider(page).focus(); await page.keyboard.press('End');
  await expect.poll(() => proportion(page)).toBe(75);
  await page.setViewportSize({width: 1024, height: 900});
  await expect(divider(page)).toBeVisible();
  await expect.poll(() => proportion(page)).toBeLessThan(75);  // re-clamp lands after the resize event
  await noOverflow(page);
  await page.setViewportSize({width: 940, height: 900});
  await expect(divider(page)).toBeHidden(); await noOverflow(page);
  await page.setViewportSize({width: 390, height: 900});
  await expect(divider(page)).toBeHidden(); await noOverflow(page);
  await page.setViewportSize({width: 1920, height: 1080});
  await expect.poll(() => proportion(page)).toBe(75);
  await page.screenshot({path: 'test-results/PANELS_WIDE.png', fullPage: true});
  await page.getByRole('button', {name: '切换深色外观'}).click();
  await divider(page).focus(); await page.keyboard.press('Home'); await noOverflow(page);
  await page.screenshot({path: 'test-results/PANELS_DARK.png', fullPage: true});
});

test('resizing preserves the media element, playback, draft and in-flight chat request', async ({page, request}) => {
  const media = await (await request.get('/__test__/media')).body();
  const upload = await (await request.post('/api/videos', {multipart: {video: {name: '分栏连续播放.mp4', mimeType: 'video/mp4', buffer: media}}})).json();
  await expect.poll(async () => (await (await request.get(`/api/videos/${upload.video_id}/study`)).json()).artifact?.artifact_id, {timeout: 30000}).toBeTruthy();
  const sent: string[] = [];
  page.on('websocket', ws => ws.on('framesent', event => {
    try {const frame = JSON.parse(String(event.payload)); if (frame.type === 'text') sent.push(frame.request_id)} catch { /* ping frames */ }
  }));
  await page.goto(`/?video=${upload.video_id}`);
  const player = page.getByLabel('视频播放器');
  await expect(page.getByLabel('向 AI 助手提问')).toBeEnabled();
  await player.evaluate(async (node: HTMLVideoElement) => {node.dataset.panelIdentity = 'preserved'; node.muted = true; node.playbackRate = .5; await node.play(); node.currentTime = 1});
  await page.getByLabel('向 AI 助手提问').fill('调整宽度后仍保留的草稿');
  await page.getByRole('button', {name: 'AI 看视频', exact: false}).click();
  await expect(page.locator('.pending')).toBeVisible();
  await expect.poll(() => sent.length).toBeGreaterThan(0);
  const requestsBefore = sent.length;
  await dragBy(page, -170);
  await expect(player).toHaveAttribute('data-panel-identity', 'preserved');
  expect(await player.evaluate((node: HTMLVideoElement) => node.currentTime)).toBeGreaterThanOrEqual(1);
  expect(await player.evaluate((node: HTMLVideoElement) => node.paused)).toBe(false);
  await expect(page.getByLabel('向 AI 助手提问')).toHaveValue('调整宽度后仍保留的草稿');
  await expect(page.locator('.message.assistant')).toHaveCount(1);
  expect(sent).toHaveLength(requestsBefore);
  expect(new Set(sent).size).toBe(1);
  const ratio = await proportion(page);
  await page.getByRole('button', {name: '新对话', exact: true}).click();
  await expect(page.locator('.message.assistant')).toHaveCount(0);
  await expect.poll(() => proportion(page)).toBe(ratio);
  await page.goto('/');
  await page.getByRole('button', {name: /分栏连续播放.mp4/}).first().click();
  await expect(page.getByRole('heading', {name: '分栏连续播放.mp4', exact: true})).toBeVisible();
  await expect.poll(() => proportion(page)).toBe(ratio);
  await noOverflow(page);
  await page.screenshot({path: 'test-results/PANELS_LEARNING.png', fullPage: true});
});

test('damaged or unavailable preference storage does not prevent resizing', async ({page}) => {
  await page.goto('/');
  await page.evaluate(key => localStorage.setItem(key, 'damaged'), preferenceKey);
  await page.reload();
  await expect.poll(() => proportion(page)).toBe(65);
  await page.addInitScript(key => {
    const originalSet = Storage.prototype.setItem, originalGet = Storage.prototype.getItem;
    Storage.prototype.setItem = function(name, value) {if (name === key) throw new DOMException('Test unavailable storage', 'QuotaExceededError'); originalSet.call(this, name, value)};
    Storage.prototype.getItem = function(name) {if (name === key) throw new DOMException('Test unavailable storage', 'SecurityError'); return originalGet.call(this, name)};
  }, preferenceKey);
  await page.reload();
  await expect.poll(() => proportion(page)).toBe(65);
  await dragBy(page, -100);
  expect(await proportion(page)).toBeLessThan(65);
  await expect(page.locator('html')).not.toHaveClass(/is-resizing-panels/);
  await divider(page).dblclick();
  await expect.poll(() => proportion(page)).toBe(65);
});
