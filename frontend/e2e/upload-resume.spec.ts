// E1 browser acceptance on the explicit platform server (tests/e1_browser_server.py):
// real PostgreSQL, real S3-compatible storage, real media; deterministic model adapter only.
import {expect, test, type Page, type Request} from '@playwright/test';

const base = process.env.EVIREEL_E1_URL;
const password = process.env.EVIREEL_E2E_PASSWORD || '';
test.skip(!base || !password, 'E1 platform server not configured (EVIREEL_E1_URL / EVIREEL_E2E_PASSWORD)');
test.use({baseURL: base});
test.describe.configure({mode: 'serial', timeout: 240000});

const isPart = (r: Request) => r.method() === 'PUT' && /[?&]partNumber=\d+/.test(r.url()) && /[?&]uploadId=/.test(r.url());
const partOf = (url: string) => Number(new URL(url).searchParams.get('partNumber'));

async function login(page: Page, user: string) {
  await page.goto('/app/');
  await page.getByLabel('用户名').fill(user);
  await page.getByLabel('密码').fill(password);
  await page.getByRole('button', {name: '登录'}).click();
  await expect(page.getByRole('button', {name: '上传视频'})).toBeEnabled();
}

async function fixtures(page: Page) {
  return (await page.request.get('/__test__/fixtures')).json() as Promise<Record<'lecture' | 'other', {path: string; size: number; sha256: string}>>;
}

test('interrupted upload resumes after refresh, re-signs, sends only missing parts, and bytes match', async ({page}) => {
  const files = await fixtures(page);
  await login(page, 'alice');
  const sent: number[] = [];
  page.on('requestfinished', async r => {if (isPart(r) && (await r.response())?.status() === 200) sent.push(partOf(r.url()))});
  // Network trouble: the first attempt of part 2 fails; parts >= 3 are held so we can refresh mid-upload.
  let failedOnce = false;
  let hold = true;
  await page.route(/partNumber=/, async route => {
    const part = partOf(route.request().url());
    if (part === 2 && !failedOnce) {failedOnce = true; return route.abort('connectionreset')}
    if (part >= 3 && hold) return;  // never answered: simulates a stalled connection until the refresh
    return route.continue();
  });
  const created = page.waitForResponse(r => r.url().endsWith('/api/uploads') && r.request().method() === 'POST');
  await page.getByLabel('选择视频文件').setInputFiles(files.lecture.path);
  const session = await (await created).json();
  await expect.poll(() => [...new Set(sent)].sort().join(','), {timeout: 60000}).toBe('1,2');
  await expect(page.getByTestId('upload-detail')).toContainText('已重试 1 次');

  // Browser refresh: in-flight requests die; the server still knows parts 1 and 2.
  hold = false;
  await page.unrouteAll({behavior: 'ignoreErrors'});
  await page.reload();
  sent.length = 0;
  await expect(page.getByRole('region', {name: '未完成的上传'})).toContainText('lecture.mp4');

  // Choosing a different file with the same name is refused before any byte is sent.
  let chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', {name: '选择文件继续'}).click();
  await (await chooser).setFiles({name: 'lecture.mp4', mimeType: 'video/mp4', buffer: Buffer.alloc(files.lecture.size, 1)});
  await expect(page.getByRole('alert')).toContainText('不是同一个文件');
  expect(sent).toEqual([]);

  // Same file: one part's signature expires before it reaches storage -> client re-signs.
  let delayed = false;
  await page.route(/partNumber=/, async route => {
    if (!delayed && partOf(route.request().url()) === 4) {delayed = true; await new Promise(r => setTimeout(r, 4500))}
    return route.continue();
  });
  chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', {name: '选择文件继续'}).click();
  await (await chooser).setFiles(files.lecture.path);
  await expect(page.getByText('服务器正在核对文件大小')).toBeVisible({timeout: 120000});
  await expect(page.locator('.video-heading h1')).toHaveText('lecture.mp4', {timeout: 120000});
  const resent = [...new Set(sent)].sort((a, b) => a - b);
  expect(resent[0]).toBeGreaterThanOrEqual(3);                       // parts 1-2 were never re-sent
  expect(resent).toEqual(Array.from({length: session.part_count - 2}, (_, i) => i + 3));
  const stored = await (await page.request.get(`/__test__/object-sha256/${session.upload_id}`)).json();
  expect(stored.sha256).toBe(files.lecture.sha256);

  // Indexing then playback through the existing pipeline.
  await expect(page.getByLabel('视频播放器')).toBeVisible({timeout: 120000});
  const range = await page.request.get(`/api/videos/${session.video_id}/content`, {headers: {Range: 'bytes=0-99'}});
  expect(range.status()).toBe(206);
  await page.screenshot({path: 'test-results/e1-resumed-upload.png'});
});

test('pause and continue, then cross-user isolation in the browser', async ({page, browser}) => {
  const files = await fixtures(page);
  await login(page, 'alice');
  await page.route(/partNumber=/, async route => {await new Promise(r => setTimeout(r, 400)); return route.continue()});
  const created = page.waitForResponse(r => r.url().endsWith('/api/uploads') && r.request().method() === 'POST');
  await page.getByLabel('选择视频文件').setInputFiles(files.other.path);
  const session = await (await created).json();
  await page.getByRole('button', {name: '暂停'}).click();
  await expect(page.getByRole('button', {name: '继续上传'})).toBeVisible();
  const pausedAt = await page.getByTestId('upload-percent').textContent();
  await page.waitForTimeout(1500);
  expect(await page.getByTestId('upload-percent').textContent()).toBe(pausedAt);
  await page.getByRole('button', {name: '继续上传'}).click();
  await expect(page.locator('.video-heading h1')).toHaveText('other.mp4', {timeout: 180000});

  const bob = await browser.newPage();
  await login(bob, 'bob');
  await expect(bob.locator('.video-list .video-item')).toHaveCount(0);
  const denied = await bob.request.get(`/api/videos/${session.video_id}`);
  expect(denied.status()).toBe(404);
  const content = await bob.request.get(`/api/videos/${session.video_id}/content`);
  expect(content.status()).toBe(404);
  await bob.close();
});
