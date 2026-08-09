import { readFileSync, existsSync } from "fs";
import path from "path";
import type { JobRecord } from "./types";

export type { JobRecord } from "./types";

function jobsJsonPath(): string {
  return path.join(process.cwd(), "..", "data", "jobs.json");
}

export function loadJobs(): JobRecord[] {
  const file = jobsJsonPath();
  if (!existsSync(file)) {
    return [];
  }
  const raw = readFileSync(file, "utf-8");
  const data = JSON.parse(raw) as JobRecord[];
  return data.filter((j) => j.active !== false);
}
