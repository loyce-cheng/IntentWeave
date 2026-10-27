import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";

let sessions: Promise<{ id: string; token: string }>[];
test.beforeEach(async ({ page }) => {
  sessions = [];
  page.on("response", (response) => {
    if (
      response.url().endsWith("/api/sessions") &&
      response.request().method() === "POST" &&
      response.status() === 201
    )
      sessions.push(response.json());
  });
});
test.afterEach(async ({ request }) => {
  // Reloads leave an active session behind; do not consume the service's cap.
  for (const { id, token } of await Promise.all(sessions)) {
    const response = await request.delete(`/api/sessions/${id}`, {
      headers: { "X-Session-Token": token },
    });
    expect([200, 404]).toContain(response.status());
  }
});

test("cooperation shows separate knowledge, completes delivery and restores an active action", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.locator("#metrics")).toContainText("准备就绪");
  await page.locator("#scenario").selectOption("cooperation");
  await expect(page.locator("#cooperation-panel")).toBeVisible();
  await expect(page.locator("#orders")).toContainText("快递员已收到：尚无消息");
  await page.locator("#actor-select").selectOption("doctor");
  await expect(page.locator("#orders")).toContainText("医生已收到：已提议");
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  await expect(page.locator("#orders")).toContainText(
    "世界状态（特权调试）：履行中",
  );
  await expect(page.locator("#orders")).toContainText("医生已收到：已提议");
  for (let tick = 2; tick <= 7; tick++) {
    await page.locator("#step").click();
    await expect(page.locator("#metrics")).toContainText(`TICK ${tick}`);
  }
  await expect(page.locator("#orders")).toContainText("实际履行 t7");
  await expect(page.locator("#plan")).toContainText("制作药物进行中");
  await page.locator("#archive-name").fill("协作交接后制作中");
  await page.locator("#archive-save").click();
  await expect(page.locator("#archive-status")).toContainText("已保存");
  await page.reload();
  await expect(page.locator("#metrics")).toContainText("准备就绪");
  await page.getByRole("button", { name: "恢复回放", exact: true }).click();
  await expect(page.locator("#scenario")).toHaveValue("cooperation");
  await page.locator("#actor-select").selectOption("doctor");
  await expect(page.locator("#plan")).toContainText("制作药物进行中");
  await page.locator("#replay-resume").click();
  await page.locator("#run").click();
  await expect(page.locator("#metrics")).toContainText("目标已完成");
  await expect(page.locator("#timeline")).toContainText("物资实际交接");
  await page.locator("#compare").click();
  await expect(page.locator("#comparison tbody tr")).toHaveCount(2);
  await page.getByRole("button", { name: "恢复回放", exact: true }).click();
  await expect(page.locator("#metrics")).toContainText("TICK 7");
  await expect(page.locator("#plan")).toContainText("制作药物进行中");
  await page.screenshot({
    path: "test-results/cooperation.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("cancelling a pending handover releases resources without crediting the doctor", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator("#metrics")).toContainText("准备就绪");
  await page.locator("#scenario").selectOption("cooperation");
  for (let tick = 1; tick <= 6; tick++) {
    await page.locator("#step").click();
    await expect(page.locator("#metrics")).toContainText(`TICK ${tick}`);
  }
  await expect(page.locator("#plan")).toContainText("交接药材进行中");
  await page.locator("#cancel-order").click();
  await expect(page.locator("#orders")).toContainText(
    "世界状态（特权调试）：已取消",
  );
  await page.locator("#run").click();
  await expect(page.locator("#metrics")).toContainText("当前认知下未找到计划");
  await page.locator("#actor-select").selectOption("doctor");
  await expect(page.locator("#metrics")).toContainText("0 / 0");
  await expect(page.locator("#error")).toBeHidden();
});

test("invalid scene settings preserve the running session and restored controls match the recording", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator("#metrics")).toContainText("准备就绪");
  await page.locator("#scenario").selectOption("cooperation");
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  await page.getByText("新协作场景配置", { exact: true }).click();
  await page.locator("#order-deadline").fill("0");
  await page.locator("#apply-cooperation").click();
  await expect(page.locator("#error")).toContainText("原模拟仍可继续");
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 2");

  await page.locator("#order-deadline").fill("45");
  await page.locator("#message-delay").fill("3");
  await page.locator("#merchant-tick").fill("8");
  await page.locator("#goal").selectOption("medicine_clinic");
  await page.locator("#policy").selectOption("Full");
  await expect(page.locator("#metrics")).toContainText("TICK 0 · Full");
  await expect(page.locator("#error")).toBeHidden();
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 1");
  await page.locator("#archive-name").fill("协作配置恢复");
  await page.locator("#archive-save").click();
  await expect(page.locator("#archive-status")).toContainText("已保存");
  const downloadPromise = page.waitForEvent("download");
  await page.locator("#export").click();
  const download = await downloadPromise;
  const contents = await readFile((await download.path())!, "utf8");

  await page.locator("#policy").selectOption("Local");
  await expect(page.locator("#metrics")).toContainText("TICK 0 · Local");
  await page.locator("#goal").selectOption("herb_clinic");
  await page.locator("#order-deadline").fill("40");
  await page.locator("#message-delay").fill("2");
  await page.locator("#merchant-tick").fill("0");
  await page.getByRole("button", { name: "恢复回放", exact: true }).click();
  await expect(page.locator("#policy")).toHaveValue("Full");
  await expect(page.locator("#goal")).toHaveValue("medicine_clinic");
  await expect(page.locator("#order-deadline")).toHaveValue("45");
  await expect(page.locator("#message-delay")).toHaveValue("3");
  await expect(page.locator("#merchant-tick")).toHaveValue("8");

  await page.locator("#scenario").selectOption("river");
  await expect(page.locator("#cooperation-panel")).toBeHidden();
  await page.locator("#import").setInputFiles({
    name: "configured-cooperation.json",
    mimeType: "application/json",
    buffer: Buffer.from(contents),
  });
  await expect(page.locator("#scenario")).toHaveValue("cooperation");
  await expect(page.locator("#policy")).toHaveValue("Full");
  await expect(page.locator("#goal")).toHaveValue("medicine_clinic");
  await expect(page.locator("#order-deadline")).toHaveValue("45");
  await expect(page.locator("#message-delay")).toHaveValue("3");
  await expect(page.locator("#merchant-tick")).toHaveValue("8");
  await page.locator("#replay-resume").click();
  await page.locator("#goal").selectOption("herb_clinic");
  await page.locator("#step").click();
  await expect(page.locator("#metrics")).toContainText("TICK 2");
  await expect(page.locator("#goal")).toHaveValue("herb_clinic");
  await expect(page.locator("#error")).toBeHidden();
});
