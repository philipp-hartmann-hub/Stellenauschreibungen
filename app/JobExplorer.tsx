"use client";

import { useEffect, useMemo, useState } from "react";
import type { JobRecord } from "@/lib/types";
import { isNewJob } from "@/lib/new";

type Props = {
  jobs: JobRecord[];
};

type SortMode = "ebene" | "title";

function ebeneRank(value: string): number {
  if (value === "bund") return 0;
  if (value === "land") return 1;
  return 2;
}

export function JobExplorer({ jobs }: Props) {
  const [q, setQ] = useState("");
  const [ebene, setEbene] = useState("alle");
  const [land, setLand] = useState("alle");
  const [ministerium, setMinisterium] = useState("alle");
  const [sort, setSort] = useState<SortMode>("ebene");

  const lands = useMemo(() => {
    const set = new Set<string>();
    for (const j of jobs) {
      if (ebene !== "alle" && j.ebene !== ebene) continue;
      if (j.land) set.add(j.land);
    }
    return Array.from(set).sort((a, b) => a.localeCompare(b, "de"));
  }, [jobs, ebene]);

  const ministerien = useMemo(() => {
    const map = new Map<string, string>();
    for (const j of jobs) {
      if (ebene !== "alle" && j.ebene !== ebene) continue;
      if (land !== "alle" && (j.land || "") !== land) continue;
      map.set(j.source_id, j.source_name);
    }
    return Array.from(map.entries()).sort((a, b) =>
      a[1].localeCompare(b[1], "de"),
    );
  }, [jobs, ebene, land]);

  useEffect(() => {
    if (land !== "alle" && !lands.includes(land)) {
      setLand("alle");
    }
  }, [ebene, lands, land]);

  useEffect(() => {
    if (
      ministerium !== "alle" &&
      !ministerien.some(([id]) => id === ministerium)
    ) {
      setMinisterium("alle");
    }
  }, [ebene, land, ministerien, ministerium]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const list = jobs.filter((j) => {
      if (ebene !== "alle" && j.ebene !== ebene) return false;
      if (land !== "alle" && (j.land || "") !== land) return false;
      if (ministerium !== "alle" && j.source_id !== ministerium) return false;
      if (!needle) return true;
      const hay = [j.title, j.location, j.source_name, j.url]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return hay.includes(needle);
    });

    list.sort((a, b) => {
      if (sort === "ebene") {
        const byEbene = ebeneRank(a.ebene) - ebeneRank(b.ebene);
        if (byEbene !== 0) return byEbene;
      }
      const byTitle = a.title.localeCompare(b.title, "de");
      if (byTitle !== 0) return byTitle;
      return a.source_name.localeCompare(b.source_name, "de");
    });
    return list;
  }, [jobs, q, ebene, land, ministerium, sort]);

  return (
    <div className="space-y-8">
      <form
        className="grid gap-3 md:grid-cols-4"
        onSubmit={(e) => e.preventDefault()}
        style={{ fontFamily: "var(--font-sans), system-ui, sans-serif" }}
      >
        <label className="md:col-span-4 block">
          <span className="mb-1 block text-sm tracking-wide text-[var(--muted)]">
            Suche
          </span>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Titel, Ort, Ministerium…"
            className="w-full rounded-md border border-[var(--line)] bg-white/80 px-3 py-2 outline-none ring-[var(--accent)] focus:ring-2"
          />
        </label>

        <label className="block">
          <span className="mb-1 block text-sm text-[var(--muted)]">Ebene</span>
          <select
            value={ebene}
            onChange={(e) => setEbene(e.target.value)}
            className="w-full rounded-md border border-[var(--line)] bg-white/80 px-3 py-2"
          >
            <option value="alle">Alle</option>
            <option value="bund">Bund</option>
            <option value="land">Land</option>
          </select>
        </label>

        <label className="block">
          <span className="mb-1 block text-sm text-[var(--muted)]">Land</span>
          <select
            value={land}
            onChange={(e) => setLand(e.target.value)}
            className="w-full rounded-md border border-[var(--line)] bg-white/80 px-3 py-2"
          >
            <option value="alle">Alle</option>
            {lands.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="mb-1 block text-sm text-[var(--muted)]">
            Sortierung
          </span>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as SortMode)}
            className="w-full rounded-md border border-[var(--line)] bg-white/80 px-3 py-2"
          >
            <option value="ebene">Ebene (Bund zuerst)</option>
            <option value="title">Alphabetisch</option>
          </select>
        </label>

        <label className="block md:col-span-4">
          <span className="mb-1 block text-sm text-[var(--muted)]">
            Ministerium
          </span>
          <select
            value={ministerium}
            onChange={(e) => setMinisterium(e.target.value)}
            className="w-full rounded-md border border-[var(--line)] bg-white/80 px-3 py-2"
          >
            <option value="alle">Alle</option>
            {ministerien.map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </label>
      </form>

      <p
        className="text-sm text-[var(--muted)]"
        style={{ fontFamily: "var(--font-sans), system-ui, sans-serif" }}
      >
        {filtered.length} von {jobs.length} Stellen
      </p>

      <ul className="divide-y divide-[var(--line)] border-y border-[var(--line)]">
        {filtered.map((job) => {
          const neu = isNewJob(job);
          return (
            <li key={job.uid} className="py-5">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <a
                  href={job.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xl leading-snug text-[var(--ink)] underline-offset-4 hover:underline"
                >
                  {job.title}
                </a>
                {neu ? (
                  <span
                    className="rounded px-1.5 py-0.5 text-xs font-semibold uppercase tracking-wide text-white"
                    style={{
                      background: "var(--new)",
                      fontFamily: "var(--font-sans), system-ui, sans-serif",
                    }}
                  >
                    Neu
                  </span>
                ) : null}
              </div>
              <div
                className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-[var(--muted)]"
                style={{ fontFamily: "var(--font-sans), system-ui, sans-serif" }}
              >
                <span>{job.source_name}</span>
                <span className="uppercase tracking-wide">{job.ebene}</span>
                {job.location ? <span>{job.location}</span> : null}
                {job.posted_at ? <span>eingestellt {job.posted_at}</span> : null}
                {job.deadline ? <span>Frist {job.deadline}</span> : null}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
