import { test, expect } from "@playwright/test";

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
