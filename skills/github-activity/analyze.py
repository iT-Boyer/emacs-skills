#!/usr/bin/env python3
"""Analyze a GitHub project's recent PR/issue activity and emit charts + a JSON summary.

Shells out to `gh` to fetch PRs and issues, computes throughput / backlog / responsiveness
metrics over a trailing window, writes matplotlib charts (transparent, Emacs-themed), and
prints a compact JSON summary on stdout for the calling agent to turn into a report.

Usage:
  uv run --with matplotlib analyze.py --repo owner/name [--months 6] [--fg "#eeffff"] [--outdir /tmp]
"""
import argparse, json, subprocess, sys, time
import datetime as dt


def gh_json(repo, kind):
    """kind is 'pr' or 'issue'. Returns list of dicts with created/closed/author/number."""
    cmd = ["gh", kind, "list", "--repo", repo, "--state", "all", "--limit", "1000",
           "--json", "number,title,createdAt,closedAt,author"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"gh {kind} list failed for {repo}:\n{out.stderr.strip()}")
    return json.loads(out.stdout)


def top_open_issues(repo, n=3):
    """Most-reacted open issues (demand signal), via the search API's server-side sort."""
    cmd = ["gh", "api", "-X", "GET", "search/issues",
           "-f", f"q=repo:{repo} is:issue is:open",
           "-f", "sort=reactions-+1", "-f", "order=desc", "-f", f"per_page={n}",
           "--jq", '.items[] | {number, title, up: .reactions["+1"], comments, url: .html_url}']
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        return []  # non-fatal: popularity is a nice-to-have, not core
    res = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if not d.get("up"):  # drop issues with zero 👍 — no demand signal
            continue
        t = d["title"]
        res.append({"number": d["number"],
                    "title": t if len(t) <= 60 else t[:57] + "...",
                    "reactions": d["up"], "comments": d["comments"],
                    "url": d["url"]})
    return res


def parse(items):
    rows = []
    for x in items:
        c = dt.datetime.fromisoformat(x["createdAt"].replace("Z", "+00:00"))
        cl = x.get("closedAt")
        cl = dt.datetime.fromisoformat(cl.replace("Z", "+00:00")) if cl else None
        author = (x.get("author") or {}).get("login") or "unknown"
        rows.append({"number": x.get("number"), "title": x.get("title") or "",
                     "created": c, "closed": cl, "author": author})
    return rows


def item_url(repo, kind, number):
    """kind is 'issues' or 'pull'."""
    return f"https://github.com/{repo}/{kind}/{number}"


def oldest_open(rows, now, repo, kind, n=5):
    """Top-n still-open items by age, for concrete actionables."""
    openi = [r for r in rows if r["closed"] is None]
    openi.sort(key=lambda r: r["created"])
    out = []
    for r in openi[:n]:
        title = r["title"] if len(r["title"]) <= 60 else r["title"][:57] + "..."
        out.append({"number": r["number"], "title": title,
                    "age_days": (now - r["created"]).days,
                    "url": item_url(repo, kind, r["number"])})
    return out


def months_ago(now, m):
    y, mo = now.year, now.month - m
    while mo <= 0:
        mo += 12
        y -= 1
    return now.replace(year=y, month=mo, day=min(now.day, 28))


def open_at(rows, t):
    return sum(1 for r in rows if r["created"] <= t and (r["closed"] is None or r["closed"] > t))


def weekly(rows, weeks, now):
    """Return (opened, closed) per week bucket; last bucket may be partial."""
    op = [0] * len(weeks)
    cl = [0] * len(weeks)
    for r in rows:
        for i, w in enumerate(weeks):
            we = min(w + dt.timedelta(days=7), now)
            if w <= r["created"] < we:
                op[i] += 1
            if r["closed"] and w <= r["closed"] < we:
                cl[i] += 1
    return op, cl


def durations_days(rows, start):
    """Days-to-close for items CLOSED within the window."""
    return sorted((r["closed"] - r["created"]).total_seconds() / 86400
                  for r in rows if r["closed"] and r["closed"] >= start)


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo), 1)


def style(ax, fg):
    ax.set_facecolor("none")
    for s in ("bottom", "left"):
        ax.spines[s].set_color(fg)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors=fg)
    ax.xaxis.label.set_color(fg)
    ax.yaxis.label.set_color(fg)
    ax.title.set_color(fg)
    ax.grid(True, alpha=0.2, color=fg)


def plot_throughput(weeks, partial, iss, prs, fg, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    OPEN, CLOSE, POS, NEG = "#ff9e64", "#9ece6a", "#f7768e", "#7dcfff"
    fig, axes = plt.subplots(2, 1, figsize=(11, 8), sharex=True)
    fig.patch.set_alpha(0)
    for ax, (o, c, name) in zip(axes, [(iss[0], iss[1], "Issues"), (prs[0], prs[1], "PRs")]):
        style(ax, fg)
        for x, oo, cc, p in zip(weeks, o, c, partial):
            n = oo - cc
            ax.bar(x, n, width=5, color=POS if n >= 0 else NEG, alpha=0.35,
                   hatch="///" if p else None, edgecolor=fg if p else "none", linewidth=0.6)
        ax.plot(weeks, o, color=OPEN, marker="o", ms=4, lw=2, label="opened/wk")
        ax.plot(weeks, c, color=CLOSE, marker="o", ms=4, lw=2, label="closed/wk")
        ax.axhline(0, color=fg, lw=0.8, alpha=0.5)
        ax.set_ylabel(name + " / week")
        ax.legend(facecolor="#1a1a2e", edgecolor=fg, labelcolor=fg, loc="upper left", fontsize=9, ncol=2)
    axes[0].set_title("Weekly throughput — opened vs closed, with net (bars: pink=grew, blue=shrank; hatched=in-progress week)")
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    axes[1].xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    fig.autofmt_xdate(rotation=40)
    plt.tight_layout()
    plt.savefig(path, dpi=150, transparent=True)
    plt.close(fig)


def plot_backlog(samples, fg, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    BLUE, ORANGE = "#7aa2f7", "#ff9e64"
    dates = [s[0] for s in samples]
    op_iss = [s[1] for s in samples]
    op_prs = [s[2] for s in samples]
    fig, ax = plt.subplots(figsize=(11, 5))
    fig.patch.set_alpha(0)
    style(ax, fg)
    ax.fill_between(dates, op_iss, color=ORANGE, alpha=0.15)
    ax.fill_between(dates, op_prs, color=BLUE, alpha=0.15)
    ax.plot(dates, op_iss, color=ORANGE, marker="o", ms=4, lw=2, label="Open Issues")
    ax.plot(dates, op_prs, color=BLUE, marker="o", ms=4, lw=2, label="Open PRs")
    ax.set_ylabel("open count")
    ax.set_title("Backlog level — open PRs & Issues over time")
    ax.legend(facecolor="#1a1a2e", edgecolor=fg, labelcolor=fg, loc="upper left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    fig.autofmt_xdate(rotation=40)
    plt.tight_layout()
    plt.savefig(path, dpi=150, transparent=True)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--fg", default="#eeffff")
    ap.add_argument("--outdir", default="/tmp")
    a = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    start = months_ago(now, a.months)

    prs_all = parse(gh_json(a.repo, "pr"))
    iss_all = parse(gh_json(a.repo, "issue"))

    def in_window(rows, key):
        return [r for r in rows if r[key] and r[key] >= start]

    weeks, t = [], start
    while t < now:
        weeks.append(t)
        t += dt.timedelta(days=7)
    partial = [(min(w + dt.timedelta(days=7), now) - w).days < 7 for w in weeks]

    pr_o, pr_c = weekly(prs_all, weeks, now)
    is_o, is_c = weekly(iss_all, weeks, now)

    # backlog samples at each week boundary + now
    samples = [(w, open_at(iss_all, w), open_at(prs_all, w)) for w in weeks]
    samples.append((now, open_at(iss_all, now), open_at(prs_all, now)))

    pr_dur = durations_days(prs_all, start)
    is_dur = durations_days(iss_all, start)

    # busiest weeks by total activity (opened+closed across PRs+issues), full weeks only
    activity = [(weeks[i].date().isoformat(), pr_o[i] + pr_c[i] + is_o[i] + is_c[i])
                for i in range(len(weeks)) if not partial[i]]
    busiest = sorted(activity, key=lambda x: -x[1])[:3]

    stamp = int(time.time())
    tp_path = f"{a.outdir}/agent-ghactivity-throughput-{stamp}.png"
    bl_path = f"{a.outdir}/agent-ghactivity-backlog-{stamp}.png"
    plot_throughput(weeks, partial, (is_o, is_c), (pr_o, pr_c), a.fg, tp_path)
    plot_backlog(samples, a.fg, bl_path)

    def net(o, c):
        return sum(o) - sum(c)

    summary = {
        "repo": a.repo,
        "window": {"months": a.months, "from": start.date().isoformat(), "to": now.date().isoformat()},
        "prs": {
            "opened": sum(pr_o), "closed": sum(pr_c), "net": net(pr_o, pr_c),
            "open_now": open_at(prs_all, now),
            "median_days_to_close": pct(pr_dur, 0.5), "p90_days_to_close": pct(pr_dur, 0.9),
        },
        "issues": {
            "opened": sum(is_o), "closed": sum(is_c), "net": net(is_o, is_c),
            "open_now": open_at(iss_all, now),
            "median_days_to_close": pct(is_dur, 0.5), "p90_days_to_close": pct(is_dur, 0.9),
        },
        "backlog_trend": {
            "issues_open_start": samples[0][1], "issues_open_now": samples[-1][1],
            "prs_open_start": samples[0][2], "prs_open_now": samples[-1][2],
        },
        "busiest_weeks": [{"week": w, "activity": n} for w, n in busiest],
        "oldest_open_issues": oldest_open(iss_all, now, a.repo, "issues"),
        "oldest_open_prs": oldest_open(prs_all, now, a.repo, "pull"),
        "top_open_issues": top_open_issues(a.repo),
        "current_week_partial": True,
        "charts": {"throughput": tp_path, "backlog": bl_path},
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
