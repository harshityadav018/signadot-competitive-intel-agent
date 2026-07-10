#!/usr/bin/env python3
"""
Step 3 of the pipeline: deterministic digest rendering. No AI in this file.

Input:  work/classified_<run>.json — the fetch output after the AI review step
        has added, per item:
            "priority"           final HIGH/MEDIUM/LOW (may override the keyword hint)
            "summary"            one-sentence human summary
            "priority_reason"    why it got that priority
            "suggested_response" (HIGH items only) what the growth team should do

        If a run was never AI-reviewed, this script falls back to the
        deterministic keyword hints, so the pipeline degrades instead of blocking.
        (That's also why you can point it straight at a new_items_*.json file.)

Output: digests/digest_<date>.md — a DRAFT for human review. Never auto-published.

Usage:  python3 make_digest.py [path/to/classified_run.json]
        (defaults to the newest classified_*.json in work/)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text())
WORK_DIR = HERE / CONFIG["settings"]["work_dir"]
DIGEST_DIR = HERE / CONFIG["settings"]["digest_dir"]

STATUS_ICON = {"ok": "OK", "blocked": "DEGRADED", "failed": "FAILED"}


def pick_input() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    candidates = sorted(WORK_DIR.glob("classified_*.json"))
    if not candidates:
        sys.exit("No classified_*.json found in work/ — run fetch_items.py, "
                 "then the AI review step, first.")
    return candidates[-1]


def fmt_item(it: dict, with_response: bool = False) -> str:
    date = it.get("date") or "no date"
    lines = [f"- **[{it['source']}]** [{it['title']}]({it['url']}) — {date}",
             f"  - {it.get('summary') or it.get('snippet', '')[:200]}",
             f"  - *Why this priority:* {it.get('priority_reason') or it.get('suggested_reason')}"]
    if with_response and it.get("suggested_response"):
        lines.append(f"  - **Suggested response:** {it['suggested_response']}")
    if it.get("hn_link") and it.get("hn_link") != it["url"]:
        lines.append(f"  - HN thread: {it['hn_link']}")
    return "\n".join(lines)


def main() -> None:
    src = pick_input()
    data = json.loads(src.read_text())
    items = data["items"]
    for it in items:  # fallback: keyword hint stands in if the AI pass didn't run
        it.setdefault("priority", it.get("suggested_priority", "LOW"))

    high = [i for i in items if i["priority"] == "HIGH"]
    medium = [i for i in items if i["priority"] == "MEDIUM"]
    low = [i for i in items if i["priority"] == "LOW"]

    run_date = datetime.fromisoformat(data["run_at"]).date()
    out = []
    out.append(f"# Competitive & Brand Intelligence Digest — {run_date}")
    out.append("")
    out.append(f"> **DRAFT — for human review only. Nothing in this file is auto-published "
               f"anywhere.** Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
               f"| lookback: {data['lookback_days']} days | {len(items)} new items "
               f"({data['duplicates_skipped']} duplicates skipped)")
    out.append("")

    out.append(f"## Action needed ({len(high)} HIGH)")
    out.append("")
    out.extend([fmt_item(i, with_response=True) for i in high] or ["*Nothing urgent this week.*"])
    out.append("")

    out.append(f"## Worth knowing ({len(medium)} MEDIUM)")
    out.append("")
    out.extend([fmt_item(i) for i in medium] or ["*No medium-priority items.*"])
    out.append("")

    srcs = sorted({i["source"] for i in low})
    out.append(f"**Low priority:** {len(low)} routine items"
               + (f" (from: {', '.join(srcs)})" if srcs else "")
               + f" — full detail in `work/{src.name}`.")
    out.append("")

    out.append("## Pipeline health")
    out.append("")
    for r in data["fetch_report"]:
        icon = STATUS_ICON.get(r["status"], r["status"].upper())
        out.append(f"- `{icon}` {r['source']} — {r['detail']}"
                   + (f" ({r['fetched']} fetched, {r['new']} new)" if r["status"] == "ok" else ""))
    out.append("")

    DIGEST_DIR.mkdir(exist_ok=True)
    out_path = DIGEST_DIR / f"digest_{run_date}.md"
    out_path.write_text("\n".join(out))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
