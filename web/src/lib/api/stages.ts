// Stage lists from the API contract (§ 3.4 analysis, § 6.3 exports). `stage` is an open enum:
// code that shows stages must cope with one it doesn't know.

export interface StageInfo {
  stage: string;
  message: string;
}

/** The six analysis stages, in order. */
export const JOB_STAGES: readonly StageInfo[] = [
  { stage: "resolving_vod", message: "Looking up the VOD" },
  { stage: "fetching_clips", message: "Fetching viewer clips" },
  { stage: "downloading_chat", message: "Downloading chat replay" },
  { stage: "downloading_audio", message: "Downloading audio" },
  { stage: "computing_features", message: "Computing chat and audio features" },
  { stage: "ranking", message: "Ranking moments" },
];

/** The three export stages, in order. */
export const EXPORT_STAGES: readonly StageInfo[] = [
  { stage: "obtaining_media", message: "Getting the video" },
  { stage: "transcribing", message: "Generating captions" },
  { stage: "rendering", message: "Rendering the clip" },
];
