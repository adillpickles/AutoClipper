import type { Page } from "@playwright/test";

// Every selector the e2e specs use, in one place, so a UI redesign only needs edits here.
// They go by role and accessible name (what a screen reader announces), not CSS classes or
// layout. Names are case-insensitive patterns so small copy changes still match.

/** The demo VOD from the README and `scripts/demo.*`. */
export const DEMO_VOD_URL = "https://www.twitch.tv/videos/2941757172";
export const DEMO_VOD_ID = "2941757172";

export const JOB_URL = /\/jobs\/[^/]+$/;
export const RESULTS_URL = /\/jobs\/[^/]+\/results$/;
export const EDITOR_URL = /\/jobs\/[^/]+\/results\/[^/]+$/;

export function startPage(page: Page) {
  return {
    vodInput: page.getByLabel(/twitch vod link/i),
    submit: page.getByRole("button", { name: /find the best moments/i }),
  };
}

export function progressPage(page: Page) {
  return {
    /** Shown once the analysis is done, e.g. "Found 3 moments." */
    foundMoments: page
      .getByRole("status")
      .filter({ hasText: /found \d+ moments?/i }),
    seeClips: page.getByRole("link", { name: /see the clips/i }),
  };
}

export function resultsPage(page: Page) {
  return {
    /** One "Edit clip N" link per clip card. */
    editLinks: page.getByRole("link", { name: /^edit clip \d+/i }),
    editClip: (rank: number) =>
      page.getByRole("link", {
        name: new RegExp(`^edit clip ${rank}\\b`, "i"),
      }),
    openHighlights: page
      .getByRole("main")
      .getByRole("button", { name: /make highlights video/i }),
  };
}

export function highlightsDialog(page: Page) {
  const dialog = page.getByRole("dialog", { name: /highlights/i });
  return {
    dialog,
    momentCheckboxes: dialog.getByRole("checkbox", { name: /include moment/i }),
    close: dialog.getByRole("button", { name: /^close$/i }),
  };
}

export function editorPage(page: Page) {
  return {
    title: page.getByRole("main").getByRole("heading", { level: 1 }),
    startInput: page.getByRole("spinbutton", { name: /^start/i }),
    endInput: page.getByRole("spinbutton", { name: /^end/i }),
    backToClips: page.getByRole("link", { name: /back to (the )?clips/i }),
  };
}

export function siteHeader(page: Page) {
  const banner = page.getByRole("banner");
  return {
    home: banner.getByRole("link", { name: /autoclipper home/i }),
    library: banner.getByRole("link", { name: /^library$/i }),
  };
}

export function libraryPage(page: Page) {
  return {
    heading: page.getByRole("main").getByRole("heading", { level: 1 }),
    search: page.getByRole("searchbox", { name: /search/i }),
    jobs: page.getByRole("list", { name: /analysis jobs/i }),
  };
}

/** Parse the moment count out of "Found N moments." on the progress screen. */
export async function readMomentCount(page: Page): Promise<number> {
  const text = (await progressPage(page).foundMoments.textContent()) ?? "";
  const match = /found (\d+) moments?/i.exec(text);
  if (!match) throw new Error(`No moment count in ${JSON.stringify(text)}`);
  return Number(match[1]);
}
