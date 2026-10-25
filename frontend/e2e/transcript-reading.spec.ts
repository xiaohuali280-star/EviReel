import {expect, test} from '@playwright/test';
import type {APIRequestContext, Page} from '@playwright/test';

async function ingest(request: APIRequestContext, name: string) {
  const media = await (await request.get('/__test__/media')).body();
  const result = await (await request.post('/api/videos', {multipart: {video: {name, mimeType: 'video/mp4', buffer: media}}})).json();
  await expect.poll(async () => (await (await request.get(`/api/videos/${result.video_id}/study`)).json()).artifact?.artifact_id, {timeout: 30000}).toBeTruthy();
  return result.video_id as string;
}
async function fixture(page: Page, vid: string, count = 154) {
  const state = {version: 'reading-v1', count, fail: false, calls: [] as number[], release: null as (() => void) | null, hold: false};
  await page.route(`**/api/videos/${vid}`, async route => {
    const response = await route.fetch(), detail = await response.json();
    await route.fulfill({response, json: {...detail, duration: 480, transcript: {...detail.transcript, text_version: state.version}}});
  });
  await page.route(`**/api/videos/${vid}/transcripts?*`, async route => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset'));
    state.calls.push(offset);
    const version = state.version;
    if (state.hold) {state.hold = false; await new Promise<void>(resolve => {state.release = resolve})}
    if (state.fail) {state.fail = false; await route.fulfill({status: 503, json: {error: {message: 'retry test'}}}); return}
    const all = Array.from({length: state.count}, (_, i) => ({segment_id: `s-${i}`, transcript_version: version, start_t: i * 3, end_t: i * 3 + 3, text: `${version} 第${i}句。`}));
    await route.fulfill({json: {items: all.slice(offset, offset + 100), total: state.count, limit: 100, offset}});
  });
  return state;
}
const open = async (page: Page, vid: string) => {await page.goto(`/?video=${vid}`); await page.getByRole('button', {name: '文稿', exact: true}).click()};

test('paragraphs preserve every original record across pagination and remember mode without duplicate requests', async ({page, request}) => {
  const vid = await ingest(request, '文稿双模式.mp4'), state = await fixture(page, vid);
  await open(page, vid);
  await expect(page.getByRole('button', {name: '段落阅读', exact: true})).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByText('已加载 100/154 条原始片段', {exact: false})).toBeVisible();
  await page.getByRole('button', {name: '加载更多', exact: false}).click();
  await expect(page.getByText('已加载 154/154 条原始片段', {exact: false})).toBeVisible();
  const paragraphCount = await page.locator('.transcript-line').count();
  expect(paragraphCount).toBeGreaterThanOrEqual(20); expect(paragraphCount).toBeLessThanOrEqual(30);
  const paragraphs = (await page.locator('.transcript-line>span').allTextContents()).join('');
  await page.getByRole('button', {name: '逐句模式', exact: true}).click();
  await expect(page.locator('.transcript-line')).toHaveCount(154);
  expect((await page.locator('.transcript-line>span').allTextContents()).join('')).toBe(paragraphs);
  expect(state.calls).toEqual([0, 100]);
  await page.getByRole('button', {name: '收起笔记和文稿', exact: true}).click();
  await page.getByRole('button', {name: '展开笔记和文稿', exact: true}).click();
  await expect(page.locator('.transcript-line')).toHaveCount(154);
  expect(state.calls).toEqual([0, 100]);
  await page.reload(); await page.getByRole('button', {name: '文稿', exact: true}).click();
  await expect(page.getByRole('button', {name: '逐句模式', exact: true})).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.transcript-line')).toHaveCount(100);
});

test('mode switches retain the source anchor, player and draft, with keyboard and mobile layouts', async ({page, request}) => {
  const vid = await ingest(request, '文稿阅读锚点.mp4'); await fixture(page, vid);
  await open(page, vid); await expect(page.locator('.transcript-line').first()).toBeVisible();
  const body = page.getByLabel('文稿正文', {exact: true});
  await body.evaluate(node => {node.scrollTop = 400});
  const firstVisible = () => body.evaluate(node => Array.from(node.querySelectorAll<HTMLElement>('[data-source]')).find(n => n.getBoundingClientRect().bottom > node.getBoundingClientRect().top)?.dataset.source);
  const anchor = await firstVisible();
  const player = page.getByLabel('视频播放器');
  await player.evaluate(async (node: HTMLVideoElement) => {node.dataset.identity = 'reader-player'; node.muted = true; node.playbackRate = .5; await node.play(); node.currentTime = 1});
  await page.getByLabel('向 AI 助手提问').fill('保留阅读时的草稿');
  const switcher = page.getByRole('button', {name: '逐句模式', exact: true});
  await switcher.focus(); await page.keyboard.press('Enter');
  expect(await firstVisible()).toBe(anchor);
  await expect(player).toHaveAttribute('data-identity', 'reader-player');
  expect(await player.evaluate((node: HTMLVideoElement) => node.paused)).toBe(false);
  await expect(page.getByLabel('向 AI 助手提问')).toHaveValue('保留阅读时的草稿');
  await page.getByRole('button', {name: '段落阅读', exact: true}).click();
  expect(await firstVisible()).toBe(anchor);
  await page.screenshot({path: 'test-results/TRANSCRIPT_PARAGRAPHS.png', fullPage: true});
  await page.setViewportSize({width: 390, height: 900});
  await page.getByRole('button', {name: '切换深色外观'}).click();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', {name: '逐句模式', exact: true}).click();
  await page.screenshot({path: 'test-results/TRANSCRIPT_SEGMENTS_MOBILE.png', fullPage: true});
});

test('failed pagination retains text and retries once; a new response version replaces rather than mixes records', async ({page, request}) => {
  const vid = await ingest(request, '文稿分页恢复.mp4'), state = await fixture(page, vid);
  await open(page, vid); await expect(page.getByText('已加载 100/154', {exact: false})).toBeVisible();
  const original = await page.locator('.transcript-line>span').allTextContents();
  state.fail = true;
  await page.getByRole('button', {name: '加载更多', exact: false}).click();
  await expect(page.getByRole('button', {name: '重试读取文稿'})).toBeVisible();
  expect(await page.locator('.transcript-line>span').allTextContents()).toEqual(original);
  await page.getByRole('button', {name: '重试读取文稿'}).click();
  await expect(page.getByText('已加载 154/154', {exact: false})).toBeVisible();
  expect(state.calls).toEqual([0, 100, 100]);
  await page.reload(); await page.getByRole('button', {name: '文稿', exact: true}).click();
  await expect(page.getByText('已加载 100/154', {exact: false})).toBeVisible();
  state.version = 'reading-v2';
  await page.getByRole('button', {name: '加载更多', exact: false}).click();
  await expect(page.locator('.transcript-line>span').first()).toContainText('reading-v2');
  expect((await page.locator('.transcript-line>span').allTextContents()).join('')).not.toContain('reading-v1');
});

test('leaving a video with an in-flight page cannot overwrite a new video or block its load', async ({page, request}) => {
  const a = await ingest(request, '迟到文稿A.mp4'), b = await ingest(request, '迟到文稿B.mp4');
  const state = await fixture(page, a); await fixture(page, b, 1);
  state.hold = true; await open(page, a);
  await expect.poll(() => !!state.release).toBe(true);
  await page.getByRole('button', {name: /迟到文稿B.mp4/}).first().click();
  await expect(page.getByRole('heading', {name: '迟到文稿B.mp4', exact: true})).toBeVisible();
  state.release!();
  await page.getByRole('button', {name: '文稿', exact: true}).click();
  await expect(page.getByText('已加载 1/1', {exact: false})).toBeVisible();
  await expect(page.locator('.transcript-line')).toHaveCount(1);
});

test('original-mode seeking and gaps remain precise, with safe blocked-storage fallback', async ({page, request}) => {
  const vid = await ingest(request, '文稿定位与偏好.mp4');
  await page.addInitScript(() => {Object.defineProperty(window, 'localStorage', {get() {throw Error('disabled')}})});
  await open(page, vid);
  await expect(page.locator('.transcript-line')).toHaveCount(1);
  const player = page.getByLabel('视频播放器');
  await expect.poll(() => player.evaluate((node: HTMLVideoElement) => node.readyState)).toBeGreaterThanOrEqual(1);
  await player.evaluate((node: HTMLVideoElement) => {node.pause(); node.currentTime = 2.95});
  await expect(page.locator('.transcript-line.current')).toHaveCount(0);
  await page.getByRole('button', {name: '逐句模式', exact: true}).click();
  await expect(page.locator('.transcript-line')).toHaveCount(2);
  await page.locator('.transcript-line').last().click();
  await expect.poll(() => player.evaluate((node: HTMLVideoElement) => node.currentTime)).toBeCloseTo(3.1, 1);
  await expect(page.locator('.transcript-line.current')).toHaveCount(1);
  await page.getByRole('button', {name: '段落阅读', exact: true}).click();
  await page.locator('.transcript-line').first().click();
  await expect.poll(() => player.evaluate((node: HTMLVideoElement) => node.currentTime)).toBeCloseTo(.1, 1);
});

test('a shorter replacement manuscript restarts at page zero, and an empty manuscript stops loading', async ({page, request}) => {
  const vid = await ingest(request, '文稿缩短与清空.mp4'), state = await fixture(page, vid);
  await open(page, vid); await expect(page.getByText('已加载 100/154', {exact: false})).toBeVisible();
  state.version = 'short-v2'; state.count = 3;
  await page.getByRole('button', {name: '加载更多', exact: false}).click();
  await expect(page.getByText('已加载 3/3', {exact: false})).toBeVisible();
  expect(state.calls).toEqual([0, 100, 0]);
  expect((await page.locator('.transcript-line>span').allTextContents()).join('')).not.toContain('reading-v1');
  state.version = 'empty-v3'; state.count = 0;
  await page.getByRole('button', {name: '刷新学习内容', exact: true}).click();
  await expect(page.getByText('已加载 0/0', {exact: false})).toBeVisible();
  await expect(page.getByText('该视频未识别到语音内容。', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: '加载更多', exact: false})).toHaveCount(0);
});

test('a metadata version update cancels an older page and admits the replacement immediately', async ({page, request}) => {
  const vid = await ingest(request, '文稿在途版本更新.mp4'), state = await fixture(page, vid);
  state.hold = true; await open(page, vid); await expect.poll(() => !!state.release).toBe(true);
  state.version = 'fresh-v2';
  await page.getByRole('button', {name: '刷新学习内容', exact: true}).click();
  await expect(page.getByText('已加载 100/154', {exact: false})).toBeVisible();
  await expect(page.locator('.transcript-line>span').first()).toContainText('fresh-v2');
  state.release!();
  await expect(page.getByRole('button', {name: '加载更多', exact: false})).toBeEnabled();
  expect((await page.locator('.transcript-line>span').allTextContents()).join('')).not.toContain('reading-v1');
});
