// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsView } from "@/components/settings-view";
import { ApiRequestError } from "@/lib/api/client";
import type { AppSettings } from "@/lib/api/types";

const state = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
}));

vi.mock("@/lib/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/client")>(
      "@/lib/api/client",
    );
  return {
    ...actual,
    getApiClient: () => ({
      getSettings: state.getSettings,
      updateSettings: state.updateSettings,
    }),
  };
});

const FOLDER = "D:\\Clips\\AutoClipper";

function settings(overrides: Partial<AppSettings> = {}): AppSettings {
  return { output_folder: null, last_copy: null, ...overrides };
}

function folderInput(): HTMLInputElement {
  return screen.getByLabelText("Folder path");
}

afterEach(() => {
  cleanup();
  state.getSettings.mockReset();
  state.updateSettings.mockReset();
});

describe("SettingsView", () => {
  it("shows a loading state, then the saved folder and the last copy", async () => {
    state.getSettings.mockResolvedValue(
      settings({
        output_folder: FOLDER,
        last_copy: {
          render_id: "exp_1",
          kind: "export",
          state: "copied",
          filename: "user_001_2026-10-12_Big-play_00-01-55.mp4",
          message: null,
          finished_at: "2026-11-26T20:00:00Z",
        },
      }),
    );
    render(<SettingsView />);

    expect(screen.getByText("Loading settings…")).toBeTruthy();
    expect(await screen.findByDisplayValue(FOLDER)).toBeTruthy();
    expect(
      screen.getByText("user_001_2026-10-12_Big-play_00-01-55.mp4"),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Stop copying" })).toBeTruthy();
  });

  it("saves a folder and confirms it", async () => {
    state.getSettings.mockResolvedValue(settings());
    state.updateSettings.mockResolvedValue(settings({ output_folder: FOLDER }));
    render(<SettingsView />);
    await screen.findByLabelText("Folder path");
    expect(screen.queryByRole("button", { name: "Stop copying" })).toBeNull();

    fireEvent.change(folderInput(), { target: { value: `  ${FOLDER} ` } });
    fireEvent.click(screen.getByRole("button", { name: "Save folder" }));

    expect(
      await screen.findByText(
        "Saved. New clips and highlights videos will be copied there.",
      ),
    ).toBeTruthy();
    expect(state.updateSettings).toHaveBeenCalledWith({
      output_folder: FOLDER,
    });
    expect(folderInput().value).toBe(FOLDER);
  });

  it("shows the server's reason when a folder can't be used", async () => {
    state.getSettings.mockResolvedValue(settings());
    state.updateSettings.mockRejectedValue(
      new ApiRequestError(422, {
        code: "invalid_output_folder",
        message: "That path is a file, not a folder.",
        details: { reason: "not_a_directory" },
      }),
    );
    render(<SettingsView />);
    await screen.findByLabelText("Folder path");

    fireEvent.change(folderInput(), { target: { value: "C:\\notes.txt" } });
    fireEvent.click(screen.getByRole("button", { name: "Save folder" }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "That path is a file, not a folder.",
    );
    expect(folderInput().getAttribute("aria-invalid")).toBe("true");
  });

  it("clears the folder", async () => {
    state.getSettings.mockResolvedValue(settings({ output_folder: FOLDER }));
    state.updateSettings.mockResolvedValue(settings());
    render(<SettingsView />);
    await screen.findByDisplayValue(FOLDER);

    fireEvent.click(screen.getByRole("button", { name: "Stop copying" }));

    expect(
      await screen.findByText("Cleared. Finished files stay in the app only."),
    ).toBeTruthy();
    expect(state.updateSettings).toHaveBeenCalledWith({ output_folder: null });
    expect(folderInput().value).toBe("");
  });

  it("reports a failed copy without hiding the form", async () => {
    state.getSettings.mockResolvedValue(
      settings({
        output_folder: FOLDER,
        last_copy: {
          render_id: "hl_1",
          kind: "highlights",
          state: "failed",
          filename: null,
          message: "The output folder is missing.",
          finished_at: "2026-11-26T20:00:00Z",
        },
      }),
    );
    render(<SettingsView />);

    expect(
      await screen.findByText(
        "The last highlights video wasn't saved to the folder. The output folder is missing.",
      ),
    ).toBeTruthy();
    expect(folderInput().value).toBe(FOLDER);
  });

  it("offers a retry when the settings can't be loaded", async () => {
    state.getSettings
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(settings());
    render(<SettingsView />);

    expect(await screen.findByText("Can't load settings")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByLabelText("Folder path")).toBeTruthy();
    expect(state.getSettings).toHaveBeenCalledTimes(2);
  });
});
