import { expect, test, type Page } from "@playwright/test";

import {
  DEMO_VOD_URL,
  EDITOR_URL,
  JOB_URL,
  RESULTS_URL,
  editorPage,
  highlightsDialog,
  progressPage,
  readMomentCount,
  resultsPage,
  siteHeader,
  startPage,
} from "./selectors";

// The demo path from the README: start → progress → results → clip editor → highlights.

/** Submit `vod` on the start page and wait for the progress screen; returns the job URL. */
async function startAnalysis(page: Page, vod: string): Promise<string> {
  const start = startPage(page);
  await start.vodInput.fill(vod);
  await expect(start.submit).toBeEnabled();
  await start.submit.click();
  await page.waitForURL(JOB_URL);
  return page.url();
}

test("demo VOD: analyze, review clips, open highlights, edit a clip, start again", async ({
  page,
}) => {
  await page.goto("/");
  const firstJobUrl = await startAnalysis(page, DEMO_VOD_URL);

  // Progress: the job runs through its stages and reports how many moments it found.
  const progress = progressPage(page);
  await expect(progress.seeClips).toBeVisible({ timeout: 90_000 });
  const momentCount = await readMomentCount(page);
  expect(momentCount).toBeGreaterThan(0);

  // Results: one clip card per moment.
  await progress.seeClips.click();
  await page.waitForURL(RESULTS_URL);
  const resultsUrl = page.url();
  const results = resultsPage(page);
  await expect(results.editLinks).toHaveCount(momentCount);

  // The highlights dialog opens with every moment listed, and closes again.
  await results.openHighlights.click();
  const highlights = highlightsDialog(page);
  await expect(highlights.dialog).toBeVisible();
  await expect(highlights.momentCheckboxes).toHaveCount(momentCount);
  await highlights.close.click();
  await expect(highlights.dialog).toBeHidden();

  // Clip editor for the top clip, then back to the results.
  await results.editClip(1).click();
  await page.waitForURL(EDITOR_URL);
  const editor = editorPage(page);
  await expect(editor.title).toBeVisible();
  await expect(editor.startInput).toBeVisible();
  await expect(editor.endInput).toBeVisible();
  await editor.backToClips.click();
  await page.waitForURL(resultsUrl);
  await expect(results.editLinks).toHaveCount(momentCount);

  // Back on the start page the form works again without a reload (regression for #184).
  await siteHeader(page).home.click();
  await page.waitForURL((url) => url.pathname === "/");
  const start = startPage(page);
  await expect(start.vodInput).toBeEnabled();
  await expect(start.submit).toBeEnabled();
  // The first job has finished, and a finished job is never reused, so this is a new job.
  const secondJobUrl = await startAnalysis(page, DEMO_VOD_URL);
  expect(secondJobUrl).not.toBe(firstJobUrl);
});

test("browser back to the start page leaves the form usable (#184)", async ({
  page,
}) => {
  await page.goto("/");
  const jobUrl = await startAnalysis(page, DEMO_VOD_URL);

  await page.goBack();
  await page.waitForURL((url) => url.pathname === "/");
  const start = startPage(page);
  await expect(start.vodInput).toBeEnabled();
  await expect(start.submit).toBeEnabled();
  // The first job is still running, so the API hands back that same job (api-contract.md
  // § 4); reaching its progress page again shows the submit went through.
  expect(await startAnalysis(page, DEMO_VOD_URL)).toBe(jobUrl);
});
