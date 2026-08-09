import { JobExplorer } from "./JobExplorer";
import { loadJobs } from "@/lib/jobs";

export default function HomePage() {
  const jobs = loadJobs();

  return (
    <main className="mx-auto max-w-4xl px-5 py-12 md:py-16">
      <header className="mb-10 border-b border-[var(--line)] pb-8">
        <p
          className="mb-3 text-sm uppercase tracking-[0.18em] text-[var(--accent)]"
          style={{ fontFamily: "var(--font-sans), system-ui, sans-serif" }}
        >
          Stellenausschreibungen
        </p>
        <h1 className="max-w-3xl text-4xl leading-tight md:text-5xl">
          Ministerien-Job-Monitor
        </h1>
        <p
          className="mt-4 max-w-2xl text-lg leading-relaxed text-[var(--muted)]"
          style={{ fontFamily: "var(--font-sans), system-ui, sans-serif" }}
        >
          Aktuelle Ausschreibungen deutscher Bundesministerien — gesammelt aus
          Interamt, SuccessFactors und den öffentlichen Karriereseiten.
        </p>
      </header>

      <JobExplorer jobs={jobs} />
    </main>
  );
}
