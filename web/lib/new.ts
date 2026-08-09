import type { JobRecord } from "./types";

export function isNewJob(job: Pick<JobRecord, "first_seen" | "last_seen">): boolean {
  // Nach dem ersten Upsert sind first_seen und last_seen gleich.
  // In Folge-Läufen wird last_seen aktualisiert → nur frische Einträge bleiben „neu“.
  return job.first_seen === job.last_seen;
}
