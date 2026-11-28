import { expect, test, type Page } from "@playwright/test";

import {
  DEMO_VOD_URL,
  EDITOR_URL,
  JOB_URL,
  RESULTS_URL,
  editorPage,
  highlightsDialog,
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
  await startAnalysis(page, DEMO_VOD_URL);

  // Progress: the job runs through its stages, then the results open by themselves (#187).
  await page.waitForURL(RESULTS_URL, { timeout: 90_000 });
  const resultsUrl = page.url();
  const results = resultsPage(page);

  // Results: one clip card per moment.
  await expect(results.momentsHeading).toBeVisible();
  const momentCount = await readMomentCount(page);
  expect(momentCount).toBeGreaterThan(0);
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
  // Re-submitting the same VOD may reuse the finished job (api-contract.md § 4), so only
  // check that the submit goes through to a job page.
  await startAnalysis(page, DEMO_VOD_URL);
});

test("browser back to the start page leaves the form usable (#184)", async ({
  page,
}) => {
  await page.goto("/");
  await startAnalysis(page, DEMO_VOD_URL);

  await page.goBack();
  await page.waitForURL((url) => url.pathname === "/");
  const start = startPage(page);
  await expect(start.vodInput).toBeEnabled();
  await expect(start.submit).toBeEnabled();
  // The API may hand back this job or another one for the same VOD (api-contract.md § 4,
  // and other tests share the backend); reaching a job page shows the submit went through.
  await startAnalysis(page, DEMO_VOD_URL);
});
