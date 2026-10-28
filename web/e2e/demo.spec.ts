import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#metrics")).toContainText("准备就绪");
});

test("normal delivery, export, import and step-verified replay", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.locator("#run").click();
  await expect(page.locator("#metrics")).toContainText("目标已完成");
  const downloadPromise = page.waitForEvent("download");
  await page.locator("#export").click();
  const download = await downloadPromise;
  const contents = await readFile((await download.path())!, "utf8");
  const record = JSON.parse(contents);
  expect(record.steps.length).toBeGreaterThan(5);
  expect(contents).not.toMatch(/api_key|Authorization|fixture-secret/);
  await page.locator("#reset").click();
  await expect(page.locator("#metrics")).toContainText("TICK 0");
  await page.locator("#import").setInputFiles({
    name: "record.json",
    mimeType: "application/json",
    buffer: Buffer.from(contents),
  });
  await expect(page.locator("#replay-status")).toContainText("全部一致");
  await page.locator("#replay-start").click();
  await expect(page.locator("#replay-status")).toContainText("已验证 0 /");
  await page.locator("#replay-next").click();
  await expect(page.locator("#replay-status")).toContainText("已验证 1 /");
  await expect(page.locator("#step")).toBeDisabled();
  expect(errors).toEqual([]);
});

test("hidden bridge change, local recovery and honest comparison", async ({
  page,
}) => {
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  await page.locator("#block").click();
  await expect(page.locator("#map .closed")).toHaveCount(1);
  await page.locator("#perspective").selectOption("actor");
  await expect(page.locator("#map .closed")).toHaveCount(0);
  await page.locator("#run").click();
  await expect(page.locator("#metrics")).toContainText("目标已完成");
  await expect(page.locator("#timeline")).toContainText("局部修复");
  await expect(page.locator("#map .closed")).toHaveCount(1);
  await page.locator("#compare").click();
  await expect(page.locator("#comparison tbody tr")).toHaveCount(2);
  await expect(page.locator("#comparison")).toContainText("Full");
  await expect(page.locator("#comparison")).toContainText("Local");
  await expect(page.locator("#comparison")).toContainText(
    "不构成算法优越性结论",
  );
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: "test-results/desktop.png", fullPage: true });
});

test("pause, cancel, reset and change goal", async ({ page }) => {
  // Delay the real pause response: a click does not mean the server has acknowledged it.
  await page.route("**/api/sessions/*/control", async (route) => {
    if (route.request().postDataJSON().operation === "pause") {
      const response = await route.fetch();
      await new Promise((resolve) => setTimeout(resolve, 500));
      await route.fulfill({ response });
    } else await route.continue();
  });
  await page.locator("#run").click();
  await expect(page.locator("#metrics")).toContainText("执行中");
  await page.locator("#pause").click();
  await expect(page.locator("#metrics")).toContainText("已暂停");
  const paused = await page.locator("#metrics").innerText();
  await page.waitForTimeout(700);
  expect(await page.locator("#metrics").innerText()).toBe(paused);
  await page.locator("#cancel").click();
  await expect(page.locator("#metrics")).toContainText("已取消");
  await page.locator("#reset").click();
  await expect(page.locator("#metrics")).toContainText("TICK 0");
  await page.locator("#goal").selectOption("herb_clinic");
  await page.locator("#changeGoal").click();
  await expect(page.locator("#plan")).toContainText("向诊所送药材");
  await page.locator("#run").click();
  await expect(page.locator("#metrics")).toContainText("目标已完成");
});

test("Full strategy and material loss produce bounded outcome", async ({
  page,
}) => {
  await page.locator("#policy").selectOption("Full");
  await expect(page.locator("#metrics")).toContainText("Full");
  await page.locator("#material").selectOption("garden");
  await page.locator("#remove").click();
  await expect(page.locator("#timeline")).toContainText("garden");
  await page.locator("#material").selectOption("warehouse");
  await page.locator("#remove").click();
  await expect(page.locator("#timeline")).toContainText("warehouse");
  await page.locator("#run").click();
  await expect(page.locator("#metrics")).toContainText("当前认知下未找到计划");
  await expect(page.locator("#error")).toBeHidden();
});

test("corrupt import rejected without changing active simulation", async ({
  page,
}) => {
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  const downloadPromise = page.waitForEvent("download");
  await page.locator("#export").click();
  const download = await downloadPromise;
  const data = JSON.parse(await readFile((await download.path())!, "utf8"));
  data.steps[0].state_hash = "0".repeat(64);
  await page.locator("#import").setInputFiles({
    name: "corrupt.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(data)),
  });
  await expect(page.locator("#error")).toContainText(
    "replay divergence at step 1",
  );
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  await expect(page.locator("#mode")).toContainText("实际执行");
});

test("mobile layout, no API credentials and model calls disabled", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator("#model-enabled")).toContainText("默认关闭");
  await expect(page.locator("#model-propose")).toBeDisabled();
  await expect(page.locator("#model-access")).toBeDisabled();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  await page.screenshot({ path: "test-results/mobile.png", fullPage: true });
});

test("archive survives reload, verifies replay and resumes a branch without changing the source", async ({
  page,
}) => {
  const { version } = JSON.parse(
    await readFile(new URL("../package.json", import.meta.url), "utf8"),
  );
  await expect(page.locator("#app-version")).toHaveText(`v${version}`);
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 2");
  await page
    .locator("#archive-name")
    .fill("断桥回归 <script>never execute</script>");
  await page.locator("#model-access").evaluate((input: HTMLInputElement) => {
    input.value = "private-demo-passphrase-not-for-storage";
  });
  await page.locator("#archive-save").click();
  await expect(page.locator("#archive-status")).toContainText("已保存");
  await expect(page.locator("#archives .archive-row")).toHaveCount(1);
  await expect(page.locator("#archives script")).toHaveCount(0);
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(
    "private-demo-passphrase-not-for-storage",
  );
  await page.reload();
  await expect(page.locator("#metrics")).toContainText("准备就绪");
  await expect(page.locator("#archives")).toContainText("断桥回归");
  await page.getByRole("button", { name: "恢复回放", exact: true }).click();
  await expect(page.locator("#archive-status")).toContainText("已验证并恢复");
  await expect(page.locator("#metrics")).toContainText("TICK 2");
  await expect(page.locator("#step")).toBeDisabled();
  await expect(page.locator("#mode")).toContainText("录制回放");
  await page.locator("#replay-start").click();
  await expect(page.locator("#replay-status")).toContainText("已验证 0 /");
  await page.locator("#replay-next").click();
  await expect(page.locator("#replay-status")).toContainText("已验证 1 /");
  await page.locator("#replay-resume").click();
  await expect(page.locator("#archive-status")).toContainText("可执行分支");
  await expect(page.locator("#replay-controls")).toBeHidden();
  await expect(page.locator("#step")).toBeEnabled();
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  await page.locator("#goal").selectOption("herb_clinic");
  await page.locator("#changeGoal").click();
  await page.locator("#run").click();
  await expect(page.locator("#metrics")).toContainText("目标已完成");
  const branchDownload = page.waitForEvent("download");
  await page.locator("#export").click();
  const branchFile = await branchDownload;
  const branchContents = await readFile((await branchFile.path())!, "utf8");
  await page.locator("#import").setInputFiles({
    name: "branch.json",
    mimeType: "application/json",
    buffer: Buffer.from(branchContents),
  });
  await expect(page.locator("#replay-status")).toContainText("全部一致");
  await page.getByRole("button", { name: "恢复回放", exact: true }).click();
  await expect(page.locator("#metrics")).toContainText("TICK 2");
  await expect(page.locator("#replay-status")).toContainText("已验证 2 / 2");
  await page.getByRole("button", { name: "删除存档", exact: true }).click();
  await expect(page.locator("#archive-status")).toContainText("已删除");
  await expect(page.locator("#archives .archive-row")).toHaveCount(0);
  expect(
    await page.evaluate(() =>
      JSON.parse(localStorage.getItem("worldmind.archive-capabilities.v1")!),
    ),
  ).toEqual([]);
});

test("late response from a replaced session cannot overwrite the new session", async ({
  page,
}) => {
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  let captured!: () => void;
  const snapshotCaptured = new Promise<void>((resolve) => {
    captured = resolve;
  });
  let first = true;
  await page.route(/\/api\/sessions\/[^/]+$/, async (route) => {
    if (first && route.request().method() === "GET") {
      first = false;
      const response = await route.fetch();
      captured();
      await delayed;
      await route.fulfill({ response });
    } else await route.continue();
  });
  await page.locator("#run").click();
  await snapshotCaptured;
  await page.locator("#policy").selectOption("Full");
  await expect(page.locator("#metrics")).toContainText("TICK 0 · Full");
  const observed: string[] = [];
  await page.exposeFunction("observeMetrics", (text: string) => {
    observed.push(text);
  });
  await page.evaluate(() => {
    new MutationObserver(() => {
      (
        window as unknown as { observeMetrics: (text: string) => void }
      ).observeMetrics(document.querySelector("#metrics")!.textContent!);
    }).observe(document.querySelector("#metrics")!, {
      childList: true,
      subtree: true,
    });
  });
  release();
  await page.waitForTimeout(750);
  await expect(page.locator("#metrics")).toContainText("TICK 0 · Full");
  expect(observed.every((text) => text.includes("TICK 0 · Full"))).toBe(true);
  await expect(page.locator("#error")).toBeHidden();
});

test("idle views do not poll, actor maps reuse their projection, and active polls never overlap", async ({
  page,
}) => {
  let requests = 0;
  let actorRequests = 0;
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  let captured!: () => void;
  const snapshotCaptured = new Promise<void>((resolve) => {
    captured = resolve;
  });
  page.on("request", (request) => {
    if (request.url().endsWith("/actor")) actorRequests += 1;
  });
  await page.route(/\/api\/sessions\/[^/]+$/, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    requests += 1;
    const response = await route.fetch();
    if (requests === 1) {
      captured();
      await delayed;
    }
    await route.fulfill({ response });
  });
  await page.locator("#perspective").selectOption("actor");
  await expect(page.locator("#map svg")).toBeVisible();
  await page.waitForTimeout(1100);
  expect(requests).toBe(0);
  expect(actorRequests).toBe(0);
  await page.locator("#run").click();
  await snapshotCaptured;
  await page.waitForTimeout(1100);
  expect(requests).toBe(1);
  release();
  await expect(page.locator("#metrics")).toContainText("目标已完成");
  const completedRequests = requests;
  await page.waitForTimeout(1100);
  expect(requests).toBe(completedRequests);
  await expect(page.locator("#run")).toBeDisabled();
  await expect(page.locator("#step")).toBeDisabled();
});

test("background visibility suspends polling and foreground refreshes the final state once", async ({
  page,
}) => {
  let requests = 0;
  page.on("request", (request) => {
    if (
      request.method() === "GET" &&
      /\/api\/sessions\/[^/]+$/.test(request.url())
    )
      requests += 1;
  });
  await page.locator("#run").click();
  // Dispatch the browser visibility lifecycle while leaving the real server running.
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const backgroundRequests = requests;
  await page.waitForTimeout(3000);
  expect(requests).toBe(backgroundRequests);
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.locator("#metrics")).toContainText("目标已完成");
  await page.waitForTimeout(1100);
  expect(requests).toBe(backgroundRequests + 1);
});

test("missing sessions stop automatic retries", async ({ page }) => {
  let requests = 0;
  await page.route(/\/api\/sessions\/[^/]+$/, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    requests += 1;
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "session_not_found" }),
    });
  });
  await page.locator("#run").click();
  await expect(page.locator("#error")).toContainText("自动刷新已暂停");
  await page.waitForTimeout(1600);
  expect(requests).toBe(1);
});
