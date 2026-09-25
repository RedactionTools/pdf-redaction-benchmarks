"""`pdfredeval` command line.

Covers the whole pipeline: generating cases, inspecting them, driving an adapter through
submit/collect, scoring what comes back, reporting on it, and publishing scored runs (and,
for staff, cases) to redaction-tools.com.

Stdlib argparse only: generation and the adapter layer have no runtime dependencies, and
a CLI is a poor reason to acquire the first one. `score` does - reporting included, so
`--no-report` is the flag for an environment without the report extra - which is why it
imports inside the command rather than at module scope: `pdfredeval generate` must keep
working in an environment that never installed the score extra.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .errors import BenchmarkError
from .types import Case
from .workspace import Overwrite, Workspace, confirm_overwrite, family_of, resolve_case


def parse_seeds(text: str) -> list[int]:
    """`7`, `1,2,3`, `1-10`, or any comma-separated mixture of those."""
    seeds: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            lo, _, hi = part.partition("-")
            start, end = int(lo), int(hi)
            if end < start:
                raise ValueError(f"empty seed range {part!r}")
            seeds.extend(range(start, end + 1))
        else:
            seeds.append(int(part))
    if not seeds:
        raise ValueError("no seeds given")
    return seeds


def _load_profile(text: str | None) -> dict[str, object]:
    """A profile is JSON inline, or `@path` to a JSON file."""
    if not text:
        return {}
    raw = Path(text[1:]).read_text() if text.startswith("@") else text
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError(f"profile must be a JSON object, got {type(loaded).__name__}")
    return loaded


# --- commands ---------------------------------------------------------------------


def cmd_families(args: argparse.Namespace) -> int:
    from .generate import FAMILIES

    for name in sorted(FAMILIES):
        print(name)
    return 0


def _workspace(args: argparse.Namespace) -> Workspace:
    return Workspace.from_args(args.bench_root, args.bench_version)


def _overwrite(args: argparse.Namespace) -> Overwrite:
    return Overwrite.from_flags(force=args.force, skip=args.skip_existing)


def cmd_generate(args: argparse.Namespace) -> int:
    from .generate import FAMILIES, generate

    seeds = parse_seeds(args.seed)
    if len(seeds) > 1 and args.case_id:
        raise BenchmarkError("--case-id cannot be used with more than one seed")
    families = sorted(FAMILIES) if args.family == "all" else [args.family]
    if args.case_id and len(families) > 1:
        raise BenchmarkError("--case-id cannot be used with every family")
    for family in families:
        if family not in FAMILIES:
            raise KeyError(
                f"unknown family {family!r}; known: all, {', '.join(sorted(FAMILIES))}"
            )

    # Without -o the case goes into this version's tree, stamped with its revision.
    ws = None if args.out else _workspace(args)
    revision = args.dataset_revision or (ws.version if ws else None)
    planned = [
        (family, seed, (ws.cases_dir(family) if ws else args.out),
         args.case_id or f"{family}-{seed}")
        for family in families for seed in seeds
    ]
    existing = [out / cid for _, _, out, cid in planned if (out / cid).exists()]
    if not confirm_overwrite(existing, "cases", _overwrite(args)):
        planned = [p for p in planned if (p[2] / p[3]) not in existing]
        print(f"skipped       {len(existing)} existing case(s)", file=sys.stderr)

    for family, seed, out, _ in planned:
        result = generate(
            family,
            out,
            seed=seed,
            case_id=args.case_id,
            dataset_revision=revision,
            generated_at=args.generated_at,
        )
        case = result.case
        print(f"{case.case_id}  {len(case.probes)} probes  {case.pdf_path}")
        for skip in result.skipped:
            varied = ", ".join(f"{k}={v}" for k, v in skip.conditions.varied_axes().items())
            print(f"    skipped [{varied}]: {skip.reason}", file=sys.stderr)
    if ws:
        ws.write_index()
    return 0


def _case_dir(args: argparse.Namespace, given: Path) -> Path:
    """A case directory, or a bare case id looked up in this version's tree."""
    if given.exists() or len(given.parts) > 1:
        return given
    return resolve_case(str(given), _workspace(args).cases_root)


def cmd_inspect(args: argparse.Namespace) -> int:
    case_dir = _case_dir(args, args.case_dir)
    case = Case.from_dir(case_dir)
    truth = json.loads((case_dir / "ground_truth.json").read_text())
    print(f"case          {case.case_id}")
    print(f"family        {case.family}")
    print(f"seed          {truth.get('seed')}")
    print(f"revision      {case.dataset_revision}")
    print(f"generator     {truth.get('generator_version')}")
    print(f"generated at  {truth.get('generated_at')}")
    print(f"pdf           {case.pdf_path}")
    print(f"probes        {len(case.probes)}  "
          f"({len(case.targets)} targets, {len(case.distractors)} distractors)")

    by_kind: dict[str, int] = {}
    for probe in case.probes:
        by_kind[probe.kind.value] = by_kind.get(probe.kind.value, 0) + 1
    print("kinds         " + ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))

    conflicts = case.lexical_conflicts()
    print(f"conflicts     {len(conflicts)}" + (" (INVALID CASE)" if conflicts else ""))

    if args.probes:
        print()
        for probe in case.probes:
            varied = probe.conditions.varied_axes()
            detail = ", ".join(f"{k}={v}" for k, v in varied.items())
            print(f"  {probe.id:6} {probe.kind.value:13} "
                  f"{'target' if probe.must_redact else 'keep  '} "
                  f"{(probe.category or ''):12} {probe.severity.value:8} "
                  f"{detail}{' trap=' + probe.trap if probe.trap else ''}")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    from .tools import available, load_entry_points, snapshot

    load_entry_points()
    ids = available()
    if not ids:
        print("no adapters registered.", file=sys.stderr)
        print(
            "Adapters are discovered through the 'pdfredeval.tools' entry-point group; "
            "install one, or register a class with @pdfredeval.tools.register.",
            file=sys.stderr,
        )
        return 1
    paths = snapshot()
    for tool_id in ids:
        print(f"{tool_id:24} {paths[tool_id]}")
    return 0


def _create_tool(tool_id: str, **kwargs: Any) -> Any:
    """The registered adapter, or a manual placeholder that only lays out the run."""
    from .errors import UnknownToolError
    from .tools import create
    from .tools.manual import placeholder

    try:
        return create(tool_id, **kwargs)
    except UnknownToolError:
        print(f"pdfredeval: no adapter for {tool_id!r}; laying out a manual run "
              f"for it instead", file=sys.stderr)
        return placeholder(tool_id)(**kwargs)


def cmd_submit(args: argparse.Namespace) -> int:

    ws = _workspace(args)
    case = Case.from_dir(_case_dir(args, args.case_dir),
                         dataset_revision=args.dataset_revision)
    tool = _create_tool(
        args.tool_id,
        runs_dir=args.runs_dir or ws.runs_dir(args.tool_id),
        tier=args.tier,
        operator=args.operator,
    )
    handle = tool.submit(case, _load_profile(args.profile), attempt=args.attempt)
    print(f"run           {handle.run_id}")
    print(f"directory     {handle.run_dir}")
    print(f"transport     {handle.transport.value}")
    print(f"input         {case.pdf_path}")
    task = handle.run_dir / "TASK.md"
    if task.exists():
        print(f"deliver to    {handle.run_dir}/  (the tool's PDF, any name)")
        print(f"next          follow {task}, then: pdfredeval collect {handle.run_dir}")
    if ws.contains(handle.run_dir):
        ws.write_index()
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from .tools import Handle

    handle = Handle.load(args.run_dir)
    tool = _create_tool(
        handle.tool_id,
        runs_dir=Path(args.run_dir).parent,
        operator=args.operator,
        tier=args.tier,
    )
    result = tool.collect(handle)
    manifest = result.manifest
    print(f"run           {manifest.run_id}")
    print(f"output        {result.output_path}")
    print(f"sha256        {result.output_sha256}")
    print(f"complete      {manifest.complete}")
    _refresh_index(Path(args.run_dir))
    if not manifest.complete:
        print(f"missing       {', '.join(manifest.missing_fields())}", file=sys.stderr)
        return 1
    return 0


def _refresh_index(path: Path) -> None:
    """Rewrite the index of whichever version tree `path` sits in, if any."""
    for parent in path.resolve().parents:
        if parent.name.startswith("v") and (parent / "runs").is_dir() \
                and path.resolve().is_relative_to(parent / "runs"):
            Workspace(parent.parent, parent.name).write_index()
            return


@dataclass(frozen=True, slots=True)
class _Scored:
    """One target as `score` left it: where its score/ sits, and what that found.

    `fresh` says this invocation computed it. A target read back from its artefacts is
    reported but never re-scored - a published number must not move because the engines
    underneath them drifted. Removing score/ is what asks for it again.
    """

    out: Path
    report: dict[str, Any]
    fresh: bool


def _expand_targets(targets: Sequence[Path]) -> list[Path]:
    """A parent directory stands for every run under it, so `score runs/` sweeps.

    Two levels deep, so a version's `runs/` (tool folders, then runs) sweeps as readily
    as one tool's folder. A parent with no runs underneath falls through as itself, and
    is scored as a run - which fails with the error naming what is missing, rather than
    a vaguer one about the directory.
    """
    from .score import ROWS_NAME, SCORE_DIR

    def is_run(path: Path) -> bool:
        return (path / "manifest.json").exists() or (path / SCORE_DIR / ROWS_NAME).exists()

    def runs_under(path: Path, depth: int) -> list[Path]:
        found: list[Path] = []
        for child in sorted(c for c in path.iterdir() if c.is_dir()):
            if is_run(child):
                found.append(child)
            elif depth > 1:
                found.extend(runs_under(child, depth - 1))
        return found

    resolved: list[Path] = []
    for given in targets:
        target = Path(given)
        if (
            target.is_dir()
            and not (target / "manifest.json").exists()
            and not (target / SCORE_DIR).exists()
        ):
            resolved.extend(runs_under(target, 2) or [target])
        else:
            resolved.append(target)
    return list(dict.fromkeys(resolved))


def cmd_score(args: argparse.Namespace) -> int:
    from .score import REPORT_NAME, ROWS_NAME, SCORE_DIR, score, score_run
    from .score.detection import load_entities
    from .thresholds import DEFAULTS

    ws = _workspace(args)
    if not args.target and not ws.runs_root.is_dir():
        raise BenchmarkError(
            f"nothing to score: {ws.runs_root} does not exist. Name a run directory, or "
            f"submit one first: pdfredeval submit <vendor:surface> <case_id>"
        )
    targets = _expand_targets(args.target or [ws.runs_root])
    cases_dir = args.cases_dir or ws.cases_root
    if args.out and len(targets) > 1:
        raise BenchmarkError(
            "--out says where one score/ goes; it cannot place one for every run"
        )
    if any(not t.is_dir() for t in targets) and len(targets) > 1:
        raise BenchmarkError(
            "a bare PDF is scored alone: it carries no run of its own to pool with "
            "the other targets"
        )

    thresholds = DEFAULTS.but(dpi=args.dpi) if args.dpi else DEFAULTS
    scored: list[_Scored] = []

    # A published number must not move under it, so re-scoring is asked for, never implied.
    already = [
        t for t in targets
        if t.is_dir() and not args.out
        and (t / SCORE_DIR / ROWS_NAME).exists() and (t / SCORE_DIR / REPORT_NAME).exists()
    ]
    rescore: set[Path] = set()
    if args.rescore and confirm_overwrite(
        [t / SCORE_DIR for t in already], "scores", _overwrite(args)
    ):
        rescore = set(already)

    for target in targets:
        if not target.is_dir():
            # A bare PDF against a case: the whole pipeline, with no adapter and no run
            # directory. This is how a tool's output gets scored when it arrived by email,
            # and how the framework is exercised end to end without a registered vendor.
            if not args.case_dir:
                raise BenchmarkError(
                    "scoring a PDF directly needs --case-dir pointing at the case it was "
                    "made from; a run directory carries that in its manifest instead"
                )
            case = Case.from_dir(args.case_dir, dataset_revision=args.dataset_revision)
            result = score(
                case, target.read_bytes(),
                thresholds=thresholds,
                entities=load_entities(target.parent),
                ocr_enabled=not args.no_ocr,
            )
            out = Path(args.out) if args.out else target.parent / SCORE_DIR
        else:
            out = Path(args.out) if args.out else target / SCORE_DIR
            if (out / ROWS_NAME).exists() and (out / REPORT_NAME).exists() \
                    and target not in rescore:
                scored.append(_Scored(
                    out, json.loads((out / REPORT_NAME).read_text()), fresh=False,
                ))
                continue
            result = score_run(
                target,
                case_dir=args.case_dir,
                cases_root=cases_dir,
                thresholds=thresholds,
                ocr_enabled=not args.no_ocr,
                verify=not args.no_verify,
            )
            revision = result.case.dataset_revision
            if ws.contains(target) and revision != ws.version:
                print(f"pdfredeval: {target.name} was run on dataset revision "
                      f"{revision!r}, but sits in {ws.version}/", file=sys.stderr)

        if not args.json:
            result.write(out)
        scored.append(_Scored(out, result.report(), fresh=True))

    if args.json:
        reports = [s.report for s in scored]
        print(json.dumps(reports[0] if len(reports) == 1 else reports,
                         indent=2, sort_keys=True, default=str))
        return 0

    for record in scored:
        if record.fresh or len(scored) == 1:
            _print_score_report(record.report, record.out, existing=not record.fresh)
        else:
            print(f"using         {record.out.parent} - already scored; "
                  f"pass --rescore to score it again")

    if args.no_report:
        code = 0
    else:
        # A sweep (no target, or a parent that expanded) fills the tree's reports/ even
        # when it finds one run; naming that run itself reports beside it.
        swept = not args.target or targets != [Path(t) for t in args.target]
        code = _write_report(args, scored, ws, cases_dir, swept=swept)
    if ws.contains(scored[0].out):
        ws.write_index()
    return code


def _print_score_report(
    report: dict[str, Any], out: Path, *, existing: bool = False
) -> None:
    """The terminal record of one scored run - just written, or read back from score/."""
    from .score import REPORT_NAME, ROWS_NAME

    summary = report["summary"]
    print(f"case          {report['case_id']}")
    print(f"probes        {summary['probes']} on {summary['pages']} page")
    print(f"alignment     {report['alignment']['method']} "
          f"(residual {report['alignment']['residual_px']}px"
          f"{'' if report['alignment']['confident'] else ', NOT TRUSTED'})")
    print(f"leak rate     {_rate(summary['leak_rate'])}   <- privacy")
    print(f"over-redact   {_rate(summary['over_redaction_rate'])}   <- utility")
    print(f"weighted LR   {_fmt(summary['weighted_leak_rate'])}")
    print(f"text retained {_fmt(summary['text_retention'])}")
    print(f"AOC           {_fmt(summary['area_over_coverage'])}")
    print("counts        " + "  ".join(f"{k}={v}" for k, v in summary["counts"].items()))

    leaking = {
        name: stats for name, stats in report["layers"].items()
        if (stats["layer_leak_rate"]["value"] or 0) > 0
    }
    if leaking:
        print("leak layers   " + ", ".join(
            _layer_line(name, stats) for name, stats in leaking.items()
        ))

    failed = [g["gate"] for g in report["survivability"]["gates"] if not g["passed"]]
    print(f"gates         {'all pass' if not failed else 'FAILED: ' + ', '.join(failed)}")
    if report["detection"]["mode"] == "inferred":
        print(f"detection     inferred - {report['detection']['note']}")
    else:
        detection = report["detection"]
        print(f"detection     recall {_fmt(detection['recall'])}  "
              f"precision {_fmt(detection['precision'])}  "
              f"type {_fmt(detection['type_accuracy'])}")
    label = "existing      " if existing else "written       "
    again = "  (pass --rescore to score it again)" if existing else ""
    print(f"{label}{out / ROWS_NAME}, {out / REPORT_NAME}{again}")

    # Notes go to stderr so a piped summary stays clean, but after the summary they
    # describe - which needs the flush, since stdout is block-buffered through a pipe.
    sys.stdout.flush()
    for note in report["notes"]:
        print(f"    note: {note}", file=sys.stderr)


def _write_report(
    args: argparse.Namespace, scored: Sequence[_Scored], ws: Workspace, cases_dir: Path,
    *, swept: bool = False,
) -> int:
    """The report step: what `report` used to be, on what `score` just made sure of.

    One run reports beside itself. Runs from this version's tree report into its
    `reports/`: per tool, per tool and family, and across tools when there are several.
    Anything else pools into `--report-out`, or ./report.
    Reports are derived from score/, so they are rewritten without asking - but only
    when a score changed or a page is missing.
    """
    from . import report as reporter

    paths = [s.out for s in scored]
    in_tree = all(ws.contains(p) for p in paths)
    if len(scored) == 1 and not (swept and in_tree and not args.report_out):
        groups = [(scored[0].out.parent / "report", paths, args.title, True)]
    elif args.report_out or not in_tree:
        groups = [(args.report_out or Path("report"), paths, args.title, True)]
    else:
        groups = _report_groups(reporter.load(paths).runs, ws, args.title)
        # Reports are derived from score/: if no score changed and every page exists,
        # redrawing them - a bootstrap each - would only rewrite the same numbers.
        # A page that should carry overlays and does not is not up to date either.
        # And a page older than any score it reports on is stale, whoever rescored it:
        # `score <one run> --rescore` updates that run's score/ but not the tree's pages.
        def current(out: Path, members: list[Path]) -> bool:
            from .score import REPORT_NAME

            page = out / reporter.HTML_NAME
            if not page.exists():
                page = out / reporter.MARKDOWN_NAME
                if not page.exists():
                    return False
            elif args.overlay is not False and 'class="shot"' not in page.read_text():
                return False
            newest = max((m / REPORT_NAME).stat().st_mtime for m in members)
            return page.stat().st_mtime >= newest

        if not any(s.fresh for s in scored) and all(
            current(out, members) for out, members, *_ in groups
        ):
            print(f"reports       up to date in {ws.reports_root}")
            return 0

    # Overlays are drawn once per run, into the group that owns them, and then shown on
    # every page that includes the run - its tool page and the summary as well. Pages
    # inline their images, so this repeats no PNG on disk.
    drawn: list[Path] = []
    if args.overlay is not False:
        for out, members, _, owns in groups:
            if owns:
                drawn += reporter.render_overlays(
                    reporter.load(members), out / reporter.OVERLAY_DIR,
                    case_dir=args.case_dir, cases_root=cases_dir,
                )
        # On by default; `None` is that default, so only an explicit --overlay complains
        # when there is nothing to draw on (a bare PDF carries no run to find it by).
        if not drawn and args.overlay:
            print("pdfredeval: no overlay drawn - needs the run's output PDF and its "
                  "case beside it", file=sys.stderr)

    for out, members, title, _ in groups:
        loaded = reporter.load(members, title=title)
        ids = [run.run_id for run in loaded.runs]
        images = [p for p in drawn if any(p.name.startswith(f"{i}-") for i in ids)]

        if len(loaded.runs) > 1 and len(groups) == 1:
            # Several runs pool into the comparison tables; one run's numbers were
            # already printed, and printing them twice as tables helps nobody.
            print(reporter.terminal_summary(loaded))
            print()

        written = reporter.write(
            loaded, out,
            markdown=not args.no_markdown, page=not args.no_html, images=images,
        )
        for path in written.paths():
            print(f"written       {path}")
    return 0


def _report_groups(
    runs: Sequence[Any], ws: Workspace, title: str | None,
) -> list[tuple[Path, list[Path], str | None, bool]]:
    """(directory, score dirs, title, draws overlays?) for every report a sweep implies.

    Each run's overlay PNG is drawn once, into its tool-and-family report; every page
    that includes the run shows it.
    """
    by_tool: dict[str, list[Any]] = {}
    for run in runs:
        by_tool.setdefault(run.tool_id, []).append(run)

    def families(members: list[Any]) -> dict[str, list[Path]]:
        split: dict[str, list[Path]] = {}
        for run in members:
            family = run.family or family_of(run.case_id) or "unknown"
            split.setdefault(family, []).append(run.path)
        return split

    groups: list[tuple[Path, list[Path], str | None, bool]] = []
    scopes = [(tool_id, members) for tool_id, members in sorted(by_tool.items())]
    if len(by_tool) > 1:
        scopes.append((None, list(runs)))
    for tool_id, members in scopes:
        name = tool_id or "All tools"
        groups.append((ws.reports_dir(tool_id), [r.path for r in members],
                       title or f"{name}, {ws.version}", False))
        for family, paths in sorted(families(members).items()):
            groups.append((ws.reports_dir(tool_id, family), paths,
                           title or f"{name}, {family}, {ws.version}",
                           tool_id is not None))
    return groups


def _layer_line(name: str, stats: dict[str, Any]) -> str:
    """`layer 0.4 (exclusive 0.1)` - the vector a vendor can act on."""
    line = f"{name} {_fmt(stats['layer_leak_rate']['value'])}"
    exclusive = stats["exclusive_leak_rate"]["value"] or 0
    return line + (f" (exclusive {_fmt(exclusive)})" if exclusive > 0 else "")


def _fmt(value: Any) -> str:
    """An undefined number prints as `n/a`, never as 0."""
    return "n/a" if value is None else f"{float(value):.4g}"


def _rate(rate: dict[str, Any]) -> str:
    """A rate is never printed without its interval or its n."""
    if rate.get("value") is None:
        return "n/a (no probes in this population)"
    low, high = (float(v) for v in rate["ci95"])
    return f"{float(rate['value']):.4g}  [{low:.3g}, {high:.3g}] over n={rate['n']}"


def cmd_publish(args: argparse.Namespace) -> int:
    from .publish import SUBMISSIONS_PAGE, client_for, load_run, publish_runs

    ws = _workspace(args)
    targets = _expand_targets(args.target or [ws.runs_root])
    # Every run is checked before anything is sent: a half-published batch is a
    # submission the site has to review twice.
    runs = [load_run(Path(target)) for target in targets]
    if not runs:
        raise BenchmarkError("nothing to publish: no runs found")

    if args.dry_run:
        for run in runs:
            overlay = "with overlay" if run.overlay else "no overlay (the site draws one)"
            print(f"{run.manifest.tool_id}  {run.manifest.dataset_revision}  "
                  f"{run.manifest.run_id}  ({overlay})")
        print(f"dry run: {len(runs)} run(s) would be published; nothing was sent")
        return 0

    client = client_for(args.site)
    for submission_id, count in publish_runs(client, runs, suite=args.suite, notes=args.notes):
        print(f"submission {submission_id}: {count} run{'s' if count != 1 else ''} sent "
              "for scoring and review")
    print(f"follow them at {SUBMISSIONS_PAGE}")
    return 0


def cmd_publish_cases(args: argparse.Namespace) -> int:
    from .publish import case_dirs, client_for

    ws = _workspace(args)
    root = args.cases_dir or ws.cases_root
    cases = list(case_dirs(root))
    if not cases:
        raise BenchmarkError(f"no cases under {root}")
    holdout = set(args.holdout or ())
    client = client_for(args.site)
    for case_dir in cases:
        visibility = "holdout" if case_dir.name in holdout else "public"
        client.publish_case(args.suite, case_dir, visibility)
        print(f"{case_dir.name}  {visibility}")
    print(f"published {len(cases)} cases")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from .auth import login
    from .publish import site_url

    user = login(site_url(args.site), open_browser=not args.no_browser)
    who = user.get("name") or user.get("email") or "your account"
    print(f"Logged in as {who}. `pdfredeval publish` will use this machine's key.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    from .auth import logout
    from .publish import site_url

    site = site_url(args.site)
    if logout(site):
        print(f"Logged out of {site}; this machine's key is revoked.")
    else:
        print(f"Not logged in to {site}.")
    return 0


# --- wiring -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdfredeval",
        description="Benchmark third-party PDF redaction tools.",
    )
    parser.add_argument("--version", action="version", version=f"pdfredeval {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("families", help="list case families")
    p.set_defaults(func=cmd_families)

    tree = argparse.ArgumentParser(add_help=False)
    group = tree.add_argument_group("benchmark tree")
    group.add_argument("--bench-root", type=Path,
                       help="root of the versioned tree (default: $PDFREDEVAL_HOME "
                            "or ./benchmarks)")
    group.add_argument("--bench-version",
                       help=f"version folder to use (default: v{__version__})")
    clobber = tree.add_mutually_exclusive_group()
    clobber.add_argument("--force", action="store_true",
                         help="overwrite existing data without asking")
    clobber.add_argument("--skip-existing", action="store_true",
                         help="keep existing data and do only what is missing")

    p = sub.add_parser("generate", parents=[tree],
                       help="write cases: PDF + ground truth")
    p.add_argument("family", help="a family from `pdfredeval families`, or all")
    p.add_argument("-s", "--seed", required=True,
                   help="seed, list or range: 7 | 1,2,3 | 1-10")
    p.add_argument("-o", "--out", type=Path,
                   help="write here instead of <bench-root>/<version>/cases/<family>/")
    p.add_argument("--case-id", help="override the generated id (single seed only)")
    p.add_argument("--dataset-revision",
                   help="default: the version folder, when writing into the tree")
    p.add_argument(
        "--generated-at",
        help="UTC ISO-8601 stamp for the page banner, e.g. 2026-09-23T11:00:00Z. "
             "Pin it (or set SOURCE_DATE_EPOCH) to regenerate a case byte-for-byte; "
             "the value used is recorded in ground_truth.json.",
    )
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("inspect", parents=[tree], help="summarise a generated case")
    p.add_argument("case_dir", type=Path, help="a case directory, or a case id")
    p.add_argument("--probes", action="store_true", help="list every probe")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("tools", help="list registered adapters")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("submit", parents=[tree], help="start a run against a tool")
    p.add_argument("tool_id", help="vendor:surface, e.g. acme:web")
    p.add_argument("case_dir", type=Path, help="a case directory, or a case id")
    p.add_argument("--runs-dir", type=Path,
                   help="default: <bench-root>/<version>/runs/<vendor-surface>/")
    p.add_argument("--profile", help="JSON, or @path/to/profile.json")
    p.add_argument("--operator")
    p.add_argument("--tier")
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--dataset-revision")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("collect", help="collect a delivered run and write its manifest")
    p.add_argument("run_dir", type=Path)
    p.add_argument("--operator", help="only fills a field the submission left unset")
    p.add_argument("--tier", help="only fills a field the submission left unset")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser(
        "score", parents=[tree],
        help="score a collected run - or any PDF against its case - and report on it",
    )
    p.add_argument("target", type=Path, nargs="*",
                   help="run directories, a parent holding several, or an output PDF "
                        "with --case-dir (default: every run of this version)")
    p.add_argument("--case-dir", type=Path,
                   help="the case this output came from; required for a bare PDF")
    p.add_argument("--cases-dir", type=Path,
                   help="where to look up a run's case by id "
                        "(default: <bench-root>/<version>/cases)")
    p.add_argument("--rescore", action="store_true",
                   help="score runs again that already have a score/ (asks first)")
    p.add_argument("--report-out", type=Path,
                   help="write one pooled report here instead of the tree's reports/")
    p.add_argument("--out", type=Path,
                   help="where to write score/ (default: beside it; one target only)")
    p.add_argument("--dpi", type=int,
                   help="render resolution for the raster checks (default: 300)")
    p.add_argument("--no-ocr", action="store_true",
                   help="skip the OCR layer; it is then reported unavailable, not clean")
    p.add_argument("--no-verify", action="store_true",
                   help="score bytes that do not match the manifest's checksums")
    p.add_argument("--dataset-revision")
    p.add_argument("--json", action="store_true",
                   help="write nothing; print the full report to stdout")
    p.add_argument("--no-report", action="store_true",
                   help="stop after score/ and the terminal summary; write no page")
    p.add_argument("--title", help="heading for the page; defaults to the tool id")
    p.add_argument("--overlay", action=argparse.BooleanOptionalAction, default=None,
                   help="draw ground truth over each output - leaks boxed red, "
                        "redactions green (default: on)")
    p.add_argument("--no-html", action="store_true")
    p.add_argument("--no-markdown", action="store_true")
    p.set_defaults(func=cmd_score)

    site = argparse.ArgumentParser(add_help=False)
    group = site.add_argument_group("site")
    group.add_argument("--site", help="the site's API origin (default: $PDFREDEVAL_SITE "
                                      "or https://backend.redaction-tools.com)")
    group.add_argument("--suite", default="pdf", help="benchmark suite (default: pdf)")

    p = sub.add_parser(
        "login", parents=[site],
        help="sign in to redaction-tools.com in the browser and save a key for publish",
    )
    p.add_argument("--no-browser", action="store_true",
                   help="print the sign-in link instead of opening it (e.g. over SSH)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", parents=[site],
                       help="revoke and forget this machine's key")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser(
        "publish", parents=[tree, site],
        help="publish scored runs to redaction-tools.com (after `pdfredeval login`)",
    )
    p.add_argument("target", type=Path, nargs="*",
                   help="run directories or a parent holding several "
                        "(default: every run of this version)")
    p.add_argument("--notes", default="", help="shown to the editor who reviews it")
    p.add_argument("--dry-run", action="store_true",
                   help="check the runs and list what would be sent; send nothing")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser(
        "publish-cases", parents=[tree, site],
        help="publish cases and their ground truth to the site (staff API key)",
    )
    p.add_argument("--cases-dir", type=Path,
                   help="default: <bench-root>/<version>/cases")
    p.add_argument("--holdout", nargs="*", metavar="CASE_ID",
                   help="cases the site scores but never shows")
    p.set_defaults(func=cmd_publish_cases)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BenchmarkError as exc:
        print(f"pdfredeval: {exc}", file=sys.stderr)
        return 1
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"pdfredeval: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # `| head`
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
