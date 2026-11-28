import { expect, test } from "@playwright/test";

import {
  DEMO_VOD_ID,
  DEMO_VOD_URL,
  JOB_URL,
  libraryPage,
  siteHeader,
  startPage,
} from "./selectors";

test("library page loads", async ({ page }) => {
  await page.goto("/library");
  const library = libraryPage(page);
  await expect(library.heading).toBeVisible();
  await expect(library.search).toBeVisible();
});

test("library lists a VOD after it is submitted", async ({ page }) => {
  await page.goto("/");
  const start = startPage(page);
  await start.vodInput.fill(DEMO_VOD_URL);
  await start.submit.click();
  await page.waitForURL(JOB_URL);

  await siteHeader(page).library.click();
  await page.waitForURL((url) => url.pathname === "/library");
  const library = libraryPage(page);
  await expect(library.heading).toBeVisible();
  await expect(library.jobs).toContainText(DEMO_VOD_ID);
});
