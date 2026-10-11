#!/usr/bin/env python3
"""Export OddMuse .pg pages to Markdown, optionally including .kp history.

Tested against representative records based on kensanata/oddmuse's ParseData,
EncodePage, GetPageFile and GetKeepFile routines. No third-party dependencies.
The source wiki is never modified.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

LOG = logging.getLogger("oddmuse-export")
FIELD = re.compile(r"^([^\s:]+): (.*)$")
WIKIWORD = re.compile(r"(?<![\w])([A-Z][a-z]+[A-Z][A-Za-z0-9]*)(?![\w])")
URL = r"(?:https?|ftp|mailto|irc|news):[^\s\]]+"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}
MIME_EXT = {"image/jpeg": ".jpg", "image/svg+xml": ".svg", "text/plain": ".txt",
            "application/pdf": ".pdf", "image/png": ".png", "image/gif": ".gif"}


def read_record(path: Path, encoding: str) -> dict[str, str]:
    """Decode OddMuse's key: value / tab-prefixed continuation format.

    Every embedded newline is written as '\\n\\t' by EscapeNewlines().
    Not a YAML file: do not parse it as one.
    """
    raw = path.read_bytes()
    try:
        decoded = raw.decode(encoding)
    except UnicodeError:
        LOG.warning("Invalid %s bytes in %s; using replacement characters", encoding, path)
        decoded = raw.decode(encoding, errors="replace")
    record: dict[str, str] = {}
    key: str | None = None
    for line in decoded.splitlines():
        if line.startswith("\t") and key is not None:
            record[key] += "\n" + line[1:]
        else:
            match = FIELD.fullmatch(line)
            if match:
                key, value = match.groups()
                record[key] = value
            elif line.strip():
                LOG.warning("Ignoring unexpected record line in %s: %r", path, line[:80])
    if "text" not in record:
        raise ValueError("no text field (not a valid OddMuse page record?)")
    return record


def utc_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def canonical(name: str) -> str:
    # Corresponds to FreeToNormal: spaces -> underscores and collapse underscores.
    return re.sub(r"_+", "_", name.replace(" ", "_")).strip("_")


def page_title(page_id: str) -> str:
    return page_id.replace("_", " ")


def safe_stem(page_id: str) -> str:
    """Never let a page id create an arbitrary filesystem path."""
    s = unicodedata.normalize("NFC", page_id)
    s = re.sub(r'[^\w .()\-]+', "_", s, flags=re.UNICODE)
    s = s.replace(" ", "_").strip(" .")
    if s in {"", ".", ".."}:
        s = "page"
    if s.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "LPT1"}:
        s = "_" + s
    return s[:130].rstrip(" .") or "page"


def assign_stems(ids: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    used: set[str] = set()
    for page_id in sorted(ids):
        stem = safe_stem(page_id)
        if stem.casefold() in used:
            stem = f"{stem[:115]}-{hashlib.sha256(page_id.encode()).hexdigest()[:12]}"
        while stem.casefold() in used:
            stem += "_"
        used.add(stem.casefold())
        result[page_id] = stem
    return result


def markdown_url(from_file: Path, to_file: Path) -> str:
    relative = Path(os.path.relpath(to_file, from_file.parent)).as_posix()
    return quote(relative, safe="/-_.~")


def yaml_string(value: str) -> str:
    # JSON quoted strings are also valid YAML double-quoted scalars.
    return json.dumps(value, ensure_ascii=False)


def front_matter(page_id: str, rec: dict[str, str], content_kind: str = "page") -> str:
    props: dict[str, str | int] = {"title": page_title(page_id), "oddmuse_id": page_id,
                                   "oddmuse_kind": content_kind}
    props["layout"] = "page"
    if rec.get("revision", "").isdigit():
        props["oddmuse_revision"] = int(rec["revision"])
    for field, key in (("ts", "updated"), ("created", "created")):
        date = utc_date(rec.get(field))
        if date:
            props[key] = date
    for field in ("username", "summary", "languages"):
        if rec.get(field):
            props[field if field != "username" else "author"] = rec[field]
    # No host/IP or cache fields are published.
    lines = ["---"]
    for key, value in props.items():
        lines.append(f"{key}: {yaml_string(value) if isinstance(value, str) else value}")
    return "\n".join(lines) + "\n---\n\n"


def decode_upload(text: str, max_bytes: int) -> tuple[str, bytes] | None:
    """OddMuse stores uploads as '#FILE MIME [gzip]\\nBASE64...'."""
    match = re.match(r"\A#FILE\s+(\S+)(?:\s+(\S+))?\s*\n([\s\S]*)\Z", text)
    if not match:
        return None
    mime, encoding, data = match.groups()
    binary = base64.b64decode(data, validate=False)
    if len(binary) > max_bytes:
        raise ValueError(f"upload exceeds --max-asset-bytes ({max_bytes})")
    if encoding and encoding != "gzip":
        raise ValueError(f"unsupported upload encoding: {encoding}")
    if encoding == "gzip":
        # Bound decompression; avoids gzip bombs from compromised archives.
        import io
        chunks = []
        total = 0
        with gzip.GzipFile(fileobj=io.BytesIO(binary)) as inp:
            while True:
                chunk = inp.read(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("decompressed upload exceeds --max-asset-bytes")
                chunks.append(chunk)
        binary = b"".join(chunks)
    return mime, binary


def asset_filename(stem: str, mime: str) -> str:
    ext = Path(stem).suffix.lower()
    if ext and (mimetypes.guess_type("x" + ext)[0] or ext in {".tar", ".md", ".zip", ".csv"}):
        return stem
    return stem + MIME_EXT.get(mime, mimetypes.guess_extension(mime) or ".bin")


class Converter:
    def __init__(self, root: Path, out: Path, ids: list[str], stems: dict[str, str],
                 dialect: str, camelcase: bool, max_asset_bytes: int) -> None:
        self.root, self.out, self.stems = root, out, stems
        self.dialect, self.camelcase, self.max_asset_bytes = dialect, camelcase, max_asset_bytes
        self.by_canonical: dict[str, str] = {}
        self.by_casefold: dict[str, str] = {}
        self.unresolved: Counter[str] = Counter()
        self.asset_paths: dict[str, Path] = {}
        for page_id in ids:
            self.by_canonical.setdefault(canonical(page_id), page_id)
            self.by_casefold.setdefault(canonical(page_id).casefold(), page_id)

    def resolve(self, target: str) -> str | None:
        target = canonical(target)
        return self.by_canonical.get(target) or self.by_casefold.get(target.casefold())

    def link(self, target: str, label: str, output_file: Path, image: bool = False) -> str:
        page_id = self.resolve(target)
        if page_id is None:
            self.unresolved[target] += 1
            # Leave original wiki markup identifiable for later cleanup.
            return f"[[{'image:' if image else ''}{target}{'|' + label if label != target else ''}]]"
        path = self.asset_paths.get(page_id) if image else None
        if path is None and not image:
            path = self.out / "pages" / (self.stems[page_id] + ".md")
        if path is None:
            self.unresolved[f"image:{target}"] += 1
            return f"[[image:{target}]]"
        href = markdown_url(output_file, path)
        label = label.replace("[", "\\[").replace("]", "\\]")
        if image:
            return f"![{label}]({href})"
        return f"[{label}]({href})"

    def inline(self, raw: str, output_file: Path) -> str:
        """Convert supported link/inline formats while protecting generated Markdown."""
        tokens: list[str] = []

        def protect(value: str) -> str:
            index = len(tokens)
            tokens.append(value)
            return f"\x00OM{index:07d}\x00"

        # literal inline code and nowiki should not be interpreted as links.
        raw = re.sub(r"<nowiki>(.*?)</nowiki>", lambda m: protect(m[1]), raw, flags=re.I | re.S)
        raw = re.sub(r"\{\{\{([^\n]+?)\}\}\}",
                     lambda m: protect("`" + m[1].replace("`", "\\`") + "`"), raw)
        raw = re.sub(r"<code>(.*?)</code>",
                     lambda m: protect("`" + m[1].replace("`", "\\`") + "`"), raw, flags=re.I)

        # Extended OddMuse link syntax; [[image:Page]] and [[download:Page]].
        def double(m: re.Match[str]) -> str:
            inner = m[1].strip()
            target, separator, label = inner.partition("|")
            target = target.strip()
            label = label.strip() if separator else page_title(target)
            if target.lower().startswith("image:"):
                tid = target[6:].strip()
                return protect(self.link(tid, label if separator else page_title(tid), output_file, True))
            if target.lower().startswith("download:"):
                tid = target[9:].strip()
                page = self.resolve(tid)
                asset = self.asset_paths.get(page) if page else None
                if asset:
                    return protect(f"[{label if separator else page_title(tid)}]({markdown_url(output_file, asset)})")
                return protect(self.link(tid, label, output_file))
            if re.match(r"^(?:https?|ftp|mailto):", target, re.I):
                return protect(f"[{label}]({target})")
            return protect(self.link(target, label, output_file))

        # Triple-bracket links must be handled before double-bracket links.
        raw = re.sub(r"\[\[\[([^\[\]\n]+)\]\]\]",
                     lambda m: protect(self.link(m[1], "[" + page_title(m[1]) + "]", output_file)), raw)
        raw = re.sub(r"\[\[([^\[\]\n]+)\]\]", double, raw)
        raw = re.sub(r"\[([A-Z][A-Za-z0-9_]+)\s+([^\]\n]+)\]",
                     lambda m: protect(self.link(m[1], m[2], output_file)), raw)
        raw = re.sub(r"\[(" + URL + r")\s+([^\]\n]+)\]",
                     lambda m: protect(f"[{m[2]}]({m[1]})"), raw)
        raw = re.sub(r"\[(" + URL + r")\]", lambda m: protect(f"<{m[1]}>") , raw)
        # Stop CamelCase matcher touching words inside external URLs.
        raw = re.sub(r"(?<![\w/])" + URL, lambda m: protect(m[0]), raw)
        if self.camelcase:
            raw = WIKIWORD.sub(lambda m: protect(self.link(m[1], page_title(m[1]), output_file)), raw)
        if self.dialect in {"usemod", "markup", "creole"}:
            raw = re.sub(r"'''''(.+?)'''''", r"***\1***", raw)
            raw = re.sub(r"'''(.+?)'''", r"**\1**", raw)
            raw = re.sub(r"''(.+?)''", r"*\1*", raw)
        if self.dialect in {"markup", "creole"}:
            raw = re.sub(r"##([^#\n]+)##", r"`\1`", raw)
            raw = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?!\w)", r"**\1**", raw)
            raw = re.sub(r"(?<![\w/])//([^/\n]+)//(?!\w)", r"*\1*", raw)
        if self.dialect == "creole":
            raw = re.sub(r"\{\{([^{}|\n]+)(?:\|([^{}\n]+))?\}\}",
                         lambda m: protect(self.link(m[1], m[2] or page_title(m[1]), output_file, True)), raw)
        for i, token in enumerate(tokens):
            raw = raw.replace(f"\x00OM{i:07d}\x00", token)
        return raw

    def markup(self, source: str, output_file: Path) -> str:
        text = source.replace("\r\n", "\n").replace("\r", "\n")
        # Do not re-interpret already-converted Markdown pages.
        if text.startswith("#MARKDOWN\n"):
            return text[len("#MARKDOWN\n"):].strip("\n") + "\n"

        blocks: list[str] = []
        def block_token(contents: str) -> str:
            marker = f"\x00BLOCK{len(blocks):07d}\x00"
            count = max([3] + [len(m[0]) + 1 for m in re.finditer(r"`{3,}", contents)])
            blocks.append("`" * count + "\n" + contents.strip("\n") + "\n" + "`" * count)
            return marker

        # Protect preformatted blocks before applying any inline conversions.
        text = re.sub(r"(?ims)^<pre>\s*\n?(.*?)\n?</pre>[ \t]*(?=\n|$)",
                      lambda m: block_token(m[1]), text)
        text = re.sub(r"(?ms)^\{\{\{[ \t]*\n(.*?)\n\}\}\}[ \t]*(?=\n|$)",
                      lambda m: block_token(m[1]), text)
        text = re.sub(r"(?ms)^```[ \t]*\n(.*?)\n```[ \t]*(?=\n|$)",
                      lambda m: block_token(m[1]), text)
        lines = text.split("\n")
        result: list[str] = []
        i = 0
        while i < len(lines):
            s = lines[i]
            token = re.fullmatch(r"\x00BLOCK(\d{7})\x00", s.strip())
            if token:
                result += ["", blocks[int(token[1])], ""]
                i += 1
                continue
            # Optional OddMuse / UseMod table extension (|| cell || cell ||).
            if re.match(r"^\s*\|\|", s) and s.rstrip().endswith("||"):
                rows: list[list[str]] = []
                while i < len(lines) and re.match(r"^\s*\|\|", lines[i]) and lines[i].rstrip().endswith("||"):
                    row = lines[i].strip()[2:-2].split("||")
                    rows.append([self.inline(cell.strip(), output_file).replace("|", "\\|") for cell in row])
                    i += 1
                width = max(map(len, rows))
                rows = [row + [""] * (width - len(row)) for row in rows]
                result.append("| " + " | ".join(rows[0]) + " |")
                result.append("| " + " | ".join(["---"] * width) + " |")
                result.extend("| " + " | ".join(row) + " |" for row in rows[1:])
                continue
            if re.fullmatch(r"[ \t]*----+[ \t]*", s):
                result.append("---")
                i += 1
                continue
            m = re.fullmatch(r"\s*(=+)\s*(.*?)\s*=+\s*", s)
            if m and m[2]:
                # UseMod's = heading = maps to h2 (even single '=').
                depth = min(6, max(2, len(m[1])))
                result.append("#" * depth + " " + self.inline(m[2], output_file))
                i += 1
                continue
            # UseMod: asterisks and hashes encode nested bullet/number lists.
            m = re.match(r"^([*#]+)[ \t]+(.*)$", s)
            if m and (len(set(m[1])) == 1):
                depth = len(m[1])
                prefix = "- " if m[1][0] == "*" else "1. "
                result.append("    " * (depth - 1) + prefix + self.inline(m[2], output_file))
                i += 1
                continue
            # OddMuse UseMod indented lines are preformatted; skip lists and tabs
            # adjacent to list items where indentation can be meaningful.
            if s.startswith((" ", "\t")) and s.strip() and self.dialect == "usemod" and not (result and re.match(r"^\s*(?:-|\d+\.) ", result[-1])):
                code = []
                while i < len(lines) and lines[i].startswith((" ", "\t")) and lines[i].strip():
                    code.append(lines[i][1:])
                    i += 1
                result.append(block_token("\n".join(code)))
                result[-1] = blocks[-1]
                continue
            result.append(self.inline(s, output_file))
            i += 1
        # Normalize very large runs of empty lines; leave other whitespace alone.
        body = "\n".join(result)
        body = re.sub(r"\n{4,}", "\n\n\n", body)
        return body.strip("\n") + "\n"

    def export_one(self, page_id: str, record: dict[str, str], path: Path,
                   content_kind: str, asset_prefix: str = "") -> dict:
        raw = record["text"]
        info: dict = {"revision": record.get("revision"), "source_timestamp": record.get("ts"),
                      "output": path.relative_to(self.out).as_posix()}
        if raw.startswith("#FILE "):
            try:
                item = decode_upload(raw, self.max_asset_bytes)
                if item is None:
                    raise ValueError("unrecognized #FILE syntax")
                mime, binary = item
                filename = asset_filename(self.stems[page_id], mime)
                if asset_prefix:
                    filename = asset_prefix + "_" + filename
                asset = self.out / "assets" / filename
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_bytes(binary)
                if not asset_prefix:
                    self.asset_paths[page_id] = asset
                target = markdown_url(path, asset)
                text = (f"![{page_title(page_id)}]({target})\n" if mime.startswith("image/")
                        else f"[Download {page_title(page_id)}]({target})\n")
                info["asset"] = asset.relative_to(self.out).as_posix()
                info["mime"] = mime
            except (ValueError, OSError, EOFError, gzip.BadGzipFile) as exc:
                LOG.warning("Could not extract asset in %s: %s", page_id, exc)
                info["error"] = str(exc)
                text = "*An original OddMuse upload could not be extracted. See migration log.*\n"
        elif raw.lstrip().startswith("#REDIRECT "):
            target = raw.strip().split(" ", 1)[1].strip()
            text = "Redirect to " + self.link(target, page_title(target), path) + ".\n"
            info["redirect_to"] = target
        else:
            text = self.markup(raw, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = front_matter(page_id, record, content_kind) if self.include_frontmatter else ""
        title = "# " + page_title(page_id) + "\n\n" if self.include_title else ""
        path.write_text(prefix + title + text, encoding="utf-8")
        return info


def collect_pages(page_dir: Path, encoding: str, decode_names: bool) -> tuple[dict[str, dict], dict[str, Path]]:
    pages: dict[str, dict] = {}
    originals: dict[str, Path] = {}
    for p in sorted(page_dir.rglob("*.pg")):
        if not p.is_file() or p.is_symlink():
            continue
        page_id = unquote(p.stem) if decode_names else p.stem
        try:
            rec = read_record(p, encoding)
        except (OSError, ValueError) as exc:
            LOG.warning("Skipping %s: %s", p, exc)
            continue
        if page_id in pages:
            LOG.warning("Duplicate page id %r (%s); keeping %s", page_id, p, originals[page_id])
            continue
        pages[page_id] = rec
        originals[page_id] = p
    return pages, originals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump", type=Path, help="OddMuse data directory (with page/ and keep/), or page/ itself")
    ap.add_argument("output", type=Path, help="Destination directory; never the original wiki directory")
    ap.add_argument("--history", action="store_true", help="Also export all available old .kp revisions")
    ap.add_argument("--dialect", choices=["usemod", "markup", "creole", "plain"], default="usemod",
                    help="Source wiki markup dialect; default: usemod")
    ap.add_argument("--no-camelcase", action="store_true", help="Disable automatic CamelCase wiki links")
    ap.add_argument("--no-frontmatter", action="store_true", help="Do not write YAML metadata")
    ap.add_argument("--no-title", action="store_true", help="Do not insert a Markdown H1 above each page")
    ap.add_argument("--decode-filenames", action="store_true", help="URL-decode percent-encoded .pg/.kp page names")
    ap.add_argument("--encoding", default="utf-8", help="OddMuse record encoding (default: utf-8)")
    ap.add_argument("--max-asset-bytes", type=int, default=100 * 1024 * 1024,
                    help="Maximum bytes per extracted upload (default: 100 MiB)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

    root = args.dump.expanduser().resolve()
    out = args.output.expanduser().resolve()
    page_dir = root if root.name == "page" else root / "page"
    data_root = root.parent if root.name == "page" else root
    keep_dir = data_root / "keep"
    if not page_dir.is_dir():
        ap.error(f"No OddMuse page directory found: {page_dir}")
    if out == data_root or out == page_dir or out.is_relative_to(page_dir):
        ap.error("output must be outside the OddMuse page/ directory")
    if args.max_asset_bytes <= 0:
        ap.error("--max-asset-bytes must be positive")
    pages, originals = collect_pages(page_dir, args.encoding, args.decode_filenames)
    if not pages:
        LOG.error("No readable .pg files found under %s", page_dir)
        return 2
    stems = assign_stems(list(pages))
    conv = Converter(data_root, out, list(pages), stems, args.dialect, not args.no_camelcase, args.max_asset_bytes)
    conv.include_frontmatter, conv.include_title = not args.no_frontmatter, not args.no_title
    out.mkdir(parents=True, exist_ok=True)

    # First pass: extract current uploads so earlier pages can link to them.
    for page_id, record in pages.items():
        if record["text"].startswith("#FILE "):
            path = out / "pages" / (stems[page_id] + ".md")
            conv.export_one(page_id, record, path, "upload")
    manifest = {"source": str(data_root), "dialect": args.dialect, "pages": {},
                "history": {}, "warnings": []}
    for page_id, record in sorted(pages.items()):
        path = out / "pages" / (stems[page_id] + ".md")
        detail = conv.export_one(page_id, record, path, "upload" if record["text"].startswith("#FILE ") else "page")
        detail["source"] = str(originals[page_id].relative_to(data_root))
        manifest["pages"][page_id] = detail

    count_history = 0
    if args.history:
        if not keep_dir.is_dir():
            LOG.warning("--history requested, but no keep/ directory exists")
        else:
            for item in sorted(keep_dir.rglob("*.kp")):
                if not item.is_file() or item.is_symlink() or not item.stem.isdigit():
                    continue
                page_id = unquote(item.parent.name) if args.decode_filenames else item.parent.name
                if page_id not in pages:
                    LOG.warning("Skipping revision belonging to absent page %s (%s)", page_id, item)
                    continue
                try:
                    record = read_record(item, args.encoding)
                    revision = int(item.stem)
                    if record.get("revision") not in (None, str(revision)):
                        LOG.warning("Revision filename/metadata mismatch in %s", item)
                    target = out / "history" / stems[page_id] / f"{revision}.md"
                    detail = conv.export_one(page_id, record, target, "historical_revision",
                                             asset_prefix=f"history_{stems[page_id]}_{revision}" if record["text"].startswith("#FILE ") else "")
                    detail["source"] = str(item.relative_to(data_root))
                    manifest["history"].setdefault(page_id, []).append(detail)
                    count_history += 1
                except (ValueError, OSError) as exc:
                    LOG.warning("Could not export revision %s: %s", item, exc)
                    manifest["warnings"].append(f"Could not export revision {item}: {exc}")

    index = ["---\nlayout: \"page\"\n---\n# Imported OddMuse pages", ""]
    for page_id in sorted(pages, key=str.casefold):
        link = quote("pages/" + stems[page_id] + ".html", safe="/-_.~")
        index.append(f"- [{page_title(page_id)}]({link})")
    (out / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    manifest["unresolved_links"] = dict(sorted(conv.unresolved.items()))
    manifest["totals"] = {"pages": len(pages), "historical_revisions": count_history,
                          "unresolved_link_occurrences": sum(conv.unresolved.values()),
                          "assets": len(list((out / "assets").iterdir())) if (out / "assets").exists() else 0}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    LOG.info("Exported %d pages, %d old revisions, %d assets, %d unresolved links into %s",
             manifest["totals"]["pages"], count_history, manifest["totals"]["assets"],
             manifest["totals"]["unresolved_link_occurrences"], out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
