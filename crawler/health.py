"""Crawler health checks and CI notification helpers."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from storage import JobStore

log = logging.getLogger("crawler.health")

# Tunables — adjust if alerts are too noisy or too quiet.
HISTORY_LIMIT = 10
MIN_HISTORY = 5
MIN_BASELINE = 3
DROP_RATIO = 0.4
DROP_MIN_BASELINE = 5

ISSUE_TITLE = "🚨 Crawler-Health: gebrochene Quellen"
HARD_FAIL_KINDS = frozenset({"fail"})


@dataclass
class Alert:
    source_id: str
    source_name: str
    adapter: str | None
    kind: str
    baseline: float | None
    count: int
    error: str | None = None


def detect_alerts(store: JobStore, run_id: int, source_names: dict[str, str]) -> list[Alert]:
    alerts: list[Alert] = []
    for stat in store.list_source_stats_for_run(run_id):
        source_id = str(stat["source_id"])
        adapter = stat.get("adapter")
        count = int(stat["count"] or 0)
        status = str(stat["status"] or "")
        error = stat.get("error")
        source_name = source_names.get(source_id, source_id)

        if status == "failed":
            alerts.append(
                Alert(
                    source_id=source_id,
                    source_name=source_name,
                    adapter=adapter,
                    kind="fail",
                    baseline=None,
                    count=count,
                    error=error,
                )
            )
            continue

        if status != "ok":
            continue

        history = store.list_recent_source_stats(
            source_id, exclude_run_id=run_id, limit=HISTORY_LIMIT
        )
        ok_history = [int(row["count"] or 0) for row in history if row.get("status") == "ok"]
        if len(ok_history) < MIN_HISTORY:
            continue

        baseline = float(median(ok_history))

        if count == 0 and baseline >= MIN_BASELINE:
            alerts.append(
                Alert(
                    source_id=source_id,
                    source_name=source_name,
                    adapter=adapter,
                    kind="zero",
                    baseline=baseline,
                    count=count,
                    error=None,
                )
            )
            continue

        if (
            count > 0
            and baseline >= DROP_MIN_BASELINE
            and count < DROP_RATIO * baseline
        ):
            alerts.append(
                Alert(
                    source_id=source_id,
                    source_name=source_name,
                    adapter=adapter,
                    kind="drop",
                    baseline=baseline,
                    count=count,
                    error=None,
                )
            )

    alerts.sort(key=lambda a: (a.kind, a.source_name))
    return alerts


def format_alerts_note(alerts: list[Alert]) -> str:
    if not alerts:
        return ""
    kinds: dict[str, int] = {}
    for alert in alerts:
        kinds[alert.kind] = kinds.get(alert.kind, 0) + 1
    parts = ", ".join(f"{kind}={kinds[kind]}" for kind in sorted(kinds))
    return f"alerts: {len(alerts)} ({parts})"


def write_health_json(path: Path | str, alerts: list[Alert]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "alerts": [asdict(alert) for alert in alerts],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def log_alerts(alerts: list[Alert]) -> None:
    if not alerts:
        log.info("Health: no alerts")
        return
    for alert in alerts:
        if alert.kind == "fail":
            log.error(
                "Health ALERT fail %s (%s): %s",
                alert.source_id,
                alert.adapter,
                alert.error or "unknown error",
            )
        elif alert.kind == "zero":
            log.warning(
                "Health ALERT zero %s (%s): ~%.0f -> 0",
                alert.source_id,
                alert.adapter,
                alert.baseline or 0,
            )
        else:
            log.warning(
                "Health ALERT drop %s (%s): ~%.0f -> %d",
                alert.source_id,
                alert.adapter,
                alert.baseline or 0,
                alert.count,
            )


def _issue_kinds(alerts: list[Alert]) -> list[Alert]:
    return [a for a in alerts if a.kind in {"fail", "zero", "drop"}]


def _build_issue_body(alerts: list[Alert]) -> str:
    lines = [
        "Automatischer Health-Check nach dem täglichen Crawl.",
        "",
        "Bitte Filter/Registry prüfen.",
        "",
        "| Quelle | Name | Adapter | Art | Count | Baseline | Fehler |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for alert in alerts:
        baseline = "" if alert.baseline is None else f"{alert.baseline:.1f}"
        err = (alert.error or "").replace("|", "\\|").replace("\n", " ")[:120]
        lines.append(
            f"| `{alert.source_id}` | {alert.source_name} | {alert.adapter or ''} "
            f"| **{alert.kind}** | {alert.count} | {baseline} | {err} |"
        )
    return "\n".join(lines)


def _build_step_summary(alerts: list[Alert], generated_at: str) -> str:
    lines = [
        "## Crawler Health",
        "",
        f"Generiert: {generated_at}",
        "",
    ]
    if not alerts:
        lines.append("Keine Alarme.")
        return "\n".join(lines)
    lines.extend(
        [
            f"**{len(alerts)} Alarm(e)**",
            "",
            "| Quelle | Art | Count | Baseline | Adapter |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for alert in alerts:
        baseline = "" if alert.baseline is None else f"{alert.baseline:.1f}"
        lines.append(
            f"| `{alert.source_id}` | **{alert.kind}** | {alert.count} | {baseline} | {alert.adapter or ''} |"
        )
    return "\n".join(lines)


def _run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def _find_open_issue_number() -> int | None:
    result = _run_gh(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title",
        ]
    )
    if result.returncode != 0:
        log.error("gh issue list failed: %s", result.stderr.strip())
        return None
    try:
        items = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for item in items:
        if item.get("title") == ISSUE_TITLE:
            return int(item["number"])
    return None


def run_ci_health_check(health_path: Path | str) -> int:
    """GitHub Actions entry: summary, issue sync, exit 1 on hard fails."""
    health_path = Path(health_path)
    if not health_path.exists():
        print(f"Missing {health_path}", file=sys.stderr)
        return 1

    payload = json.loads(health_path.read_text(encoding="utf-8"))
    raw_alerts = payload.get("alerts") or []
    alerts = [Alert(**item) for item in raw_alerts]
    generated_at = str(payload.get("generated_at") or "")

    summary_path = Path(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"))
    summary_path.write_text(_build_step_summary(alerts, generated_at), encoding="utf-8")

    issue_alerts = _issue_kinds(alerts)
    issue_number = _find_open_issue_number()

    if issue_alerts:
        body = _build_issue_body(issue_alerts)
        if issue_number is not None:
            _run_gh(["gh", "issue", "edit", str(issue_number), "--body", body])
            print(f"Updated issue #{issue_number}")
        else:
            result = _run_gh(
                ["gh", "issue", "create", "--title", ISSUE_TITLE, "--body", body]
            )
            if result.returncode != 0:
                print(result.stderr, file=sys.stderr)
                return 1
            print("Created health issue")
    elif issue_number is not None:
        _run_gh(
            [
                "gh",
                "issue",
                "close",
                str(issue_number),
                "--comment",
                "Keine offenen Health-Alarme mehr — Crawl wieder im grünen Bereich.",
            ]
        )
        print(f"Closed issue #{issue_number}")

    hard = [a for a in alerts if a.kind in HARD_FAIL_KINDS]
    if hard:
        print(f"{len(hard)} hard fail alert(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("../data/health.json")
    raise SystemExit(run_ci_health_check(target))
