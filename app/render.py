"""Turn the report and concept templates into a PDF and a picture.

The pages are plain HTML. Microsoft Edge or Google Chrome, already on any
Windows PC, prints the report to PDF and photographs the concept, working
invisibly ("headless"). Nothing is uploaded: the browser reads local files and
is given its own throwaway profile.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent / "report_templates"
CONCEPT_SIZE = (1440, 1800)
RENDER_TIMEOUT = 90


class RenderError(Exception):
    """A problem the user can act on, worded for them."""


def find_browser() -> str | None:
    """Path to Edge or Chrome, or None."""
    override = os.environ.get("CFA_BROWSER", "").strip()
    if override and os.path.isfile(override):
        return override
    if sys.platform == "win32":
        roots = [os.environ.get(name, "") for name in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
        for root in roots:
            if not root:
                continue
            for parts in (("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe")):
                path = os.path.join(root, *parts)
                if os.path.isfile(path):
                    return path
        return None
    if sys.platform == "darwin":
        for path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ):
            if os.path.isfile(path):
                return path
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _run_browser(browser: str, args: list[str], output: Path) -> None:
    profile = tempfile.mkdtemp(prefix="cfa-browser-")
    command = [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--hide-scrollbars",
        f"--user-data-dir={profile}",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.append("--no-sandbox")  # Chromium refuses to start as root otherwise (Linux test machines only)
    command += args
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash on Windows
    try:
        if output.exists():
            output.unlink()
        completed = subprocess.run(
            command, capture_output=True, timeout=RENDER_TIMEOUT, creationflags=flags, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError("Making the file took too long. Close other programs and try again.") from exc
    except OSError as exc:
        raise RenderError(f"The browser could not be started ({type(exc).__name__}).") from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if not output.exists() or output.stat().st_size < 500:
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise RenderError(f"The browser did not produce the file. {detail[0][:200]}".strip())


def html_to_pdf(browser: str, html_file: Path, pdf_file: Path) -> None:
    _run_browser(browser, [
        "--no-pdf-header-footer",
        "--print-to-pdf-no-header",          # older spelling of the same switch
        f"--print-to-pdf={pdf_file}",
        html_file.as_uri(),
    ], pdf_file)


def html_to_png(browser: str, html_file: Path, png_file: Path, size: tuple[int, int] = CONCEPT_SIZE) -> None:
    _run_browser(browser, [
        f"--window-size={size[0]},{size[1]}",
        "--force-device-scale-factor=1",
        f"--screenshot={png_file}",
        html_file.as_uri(),
    ], png_file)


def render_templates(context: dict[str, Any]) -> tuple[str, str]:
    """Return (report HTML, concept HTML) for a context from report.build_context."""
    try:
        import jinja2
    except ImportError as exc:  # an update added this library; start.bat installs it
        raise RenderError("One of the app's parts is missing. Close the app, run update.bat, then start it again.") from exc
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    try:
        report_html = env.get_template("report.html.j2").render(**context)
        concept_html = env.get_template("concept.html.j2").render(**context)
    except jinja2.TemplateError as exc:
        raise RenderError(f"The report layout could not be filled in ({type(exc).__name__}).") from exc
    return report_html, concept_html


def make_report_files(context: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    """Write report.pdf and concept.png into ``out_dir``. Blocking: call from a worker thread."""
    browser = find_browser()
    if not browser:
        raise RenderError("Making the PDF needs Microsoft Edge or Google Chrome. Install one of them, then try again.")
    report_html, concept_html = render_templates(context)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cfa-report-"))
    try:
        report_file = work / "report.html"
        concept_file = work / "concept.html"
        report_file.write_text(report_html, encoding="utf-8")
        concept_file.write_text(concept_html, encoding="utf-8")
        # Render into the work folder first, so a failure never leaves half a report behind.
        pdf_tmp, png_tmp = work / "report.pdf", work / "concept.png"
        html_to_pdf(browser, report_file, pdf_tmp)
        html_to_png(browser, concept_file, png_tmp)
        pdf_out, png_out = out_dir / "report.pdf", out_dir / "concept.png"
        shutil.move(str(pdf_tmp), str(pdf_out))
        shutil.move(str(png_tmp), str(png_out))
        return {"pdf": pdf_out, "png": png_out}
    finally:
        shutil.rmtree(work, ignore_errors=True)
