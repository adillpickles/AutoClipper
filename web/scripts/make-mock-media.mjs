// Generates the sample videos mock mode plays (`public/mock-media/`, git-ignored): an FFmpeg
// test pattern with a tone, 1080x1920 for clips and 640x360 for the stream preview. Needs
// FFmpeg on PATH. Run with `npm run mock-media`; existing files are kept.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, renameSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const out = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "public",
  "mock-media",
);
mkdirSync(out, { recursive: true });

const samples = [
  { name: "clip.mp4", size: "1080x1920", seconds: 15 },
  { name: "preview.mp4", size: "640x360", seconds: 360 },
];

for (const { name, size, seconds } of samples) {
  const path = join(out, name);
  if (existsSync(path)) {
    console.log(`kept ${path}`);
    continue;
  }
  const result = spawnSync(
    "ffmpeg",
    [
      "-v",
      "error",
      "-y",
      "-f",
      "lavfi",
      "-i",
      `testsrc2=size=${size}:rate=24:duration=${seconds}`,
      "-f",
      "lavfi",
      "-i",
      `sine=frequency=440:sample_rate=48000:duration=${seconds}`,
      "-c:v",
      "libx264",
      "-preset",
      "ultrafast",
      "-crf",
      "32",
      "-pix_fmt",
      "yuv420p",
      "-c:a",
      "aac",
      "-movflags",
      "+faststart",
      "-shortest",
      "-f",
      "mp4",
      `${path}.part`,
    ],
    { stdio: "inherit" },
  );
  if (result.error || result.status !== 0) {
    rmSync(`${path}.part`, { force: true });
    console.error(`FFmpeg failed for ${name}. Is it installed and on PATH?`);
    process.exit(1);
  }
  renameSync(`${path}.part`, path); // only finished files get the final name
  console.log(`wrote ${path}`);
}
