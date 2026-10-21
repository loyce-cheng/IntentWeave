import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#metrics')).toContainText('准备就绪');
});

test('normal delivery, export, import and step-verified replay', async ({ page }) => {
  const errors:string[]=[];page.on('pageerror', error=>errors.push(error.message));
  await page.locator('#run').click();
  await expect(page.locator('#metrics')).toContainText('目标已完成');
  const downloadPromise=page.waitForEvent('download');
  await page.locator('#export').click();
  const download=await downloadPromise;
  const contents=await readFile((await download.path())!, 'utf8');
  const record=JSON.parse(contents);
  expect(record.steps.length).toBeGreaterThan(5);
  expect(contents).not.toMatch(/api_key|Authorization|fixture-secret/);
  await page.locator('#reset').click();
  await expect(page.locator('#metrics')).toContainText('TICK 0');
  await page.locator('#import').setInputFiles({name:'record.json',mimeType:'application/json',buffer:Buffer.from(contents)});
  await expect(page.locator('#replay-status')).toContainText('全部一致');
  await page.locator('#replay-start').click();
  await expect(page.locator('#replay-status')).toContainText('已验证 0 /');
  await page.locator('#replay-next').click();
  await expect(page.locator('#replay-status')).toContainText('已验证 1 /');
  await expect(page.locator('#step')).toBeDisabled();
  expect(errors).toEqual([]);
});

test('hidden bridge change, local recovery and honest comparison', async ({ page }) => {
  await page.locator('#step').click();
  await expect(page.locator('#metrics')).toContainText('TICK 1');
  await page.locator('#block').click();
  await expect(page.locator('#map .closed')).toHaveCount(1);
  await page.locator('#perspective').selectOption('actor');
  await expect(page.locator('#map .closed')).toHaveCount(0);
  await page.locator('#run').click();
  await expect(page.locator('#metrics')).toContainText('目标已完成');
  await expect(page.locator('#timeline')).toContainText('局部修复');
  await expect(page.locator('#map .closed')).toHaveCount(1);
  await page.locator('#compare').click();
  await expect(page.locator('#comparison tbody tr')).toHaveCount(2);
  await expect(page.locator('#comparison')).toContainText('Full');
  await expect(page.locator('#comparison')).toContainText('Local');
  await expect(page.locator('#comparison')).toContainText('不构成算法优越性结论');
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:'test-results/desktop.png',fullPage:true});
});

test('pause, cancel, reset and change goal', async ({ page }) => {
  await page.locator('#run').click();
  await expect(page.locator('#metrics')).toContainText('执行中');
  await page.locator('#pause').click();
  const paused=await page.locator('#metrics').innerText();
  await page.waitForTimeout(700);
  expect(await page.locator('#metrics').innerText()).toBe(paused);
  await page.locator('#cancel').click();
  await expect(page.locator('#metrics')).toContainText('已取消');
  await page.locator('#reset').click();
  await expect(page.locator('#metrics')).toContainText('TICK 0');
  await page.locator('#goal').selectOption('herb_clinic');
  await page.locator('#changeGoal').click();
  await expect(page.locator('#plan')).toContainText('向诊所送药材');
  await page.locator('#run').click();
  await expect(page.locator('#metrics')).toContainText('目标已完成');
});

test('Full strategy and material loss produce bounded outcome', async ({ page }) => {
  await page.locator('#policy').selectOption('Full');
  await expect(page.locator('#metrics')).toContainText('Full');
  await page.locator('#material').selectOption('garden');
  await page.locator('#remove').click();
  await expect(page.locator('#timeline')).toContainText('garden');
  await page.locator('#material').selectOption('warehouse');
  await page.locator('#remove').click();
  await expect(page.locator('#timeline')).toContainText('warehouse');
  await page.locator('#run').click();
  await expect(page.locator('#metrics')).toContainText('当前认知下未找到计划');
  await expect(page.locator('#error')).toBeHidden();
});

test('corrupt import rejected without changing active simulation', async ({ page }) => {
  await page.locator('#step').click();
  await expect(page.locator('#metrics')).toContainText('TICK 1');
  const downloadPromise=page.waitForEvent('download');await page.locator('#export').click();
  const download=await downloadPromise;
  const data=JSON.parse(await readFile((await download.path())!,'utf8'));
  data.steps[0].state_hash='0'.repeat(64);
  await page.locator('#import').setInputFiles({name:'corrupt.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(data))});
  await expect(page.locator('#error')).toContainText('replay divergence at step 1');
  await expect(page.locator('#metrics')).toContainText('TICK 1');
  await expect(page.locator('#mode')).toContainText('实际执行');
});

test('mobile layout, no API credentials and model calls disabled', async ({ page }) => {
  await page.setViewportSize({width:390,height:844});
  await expect(page.locator('#model-enabled')).toContainText('默认关闭');
  await expect(page.locator('#model-propose')).toBeDisabled();
  await expect(page.locator('#model-access')).toBeDisabled();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.locator('#step').click();
  await expect(page.locator('#metrics')).toContainText('TICK 1');
  await page.screenshot({path:'test-results/mobile.png',fullPage:true});
});
