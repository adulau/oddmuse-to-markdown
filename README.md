# OddMuse to Markdown

A standalone Python 3.10+ exporter for old OddMuse wiki data directories. Requires **only the Python standard library**. The original OddMuse dump is **read-only**.

## Quick start

```bash
python3 oddmuse_to_markdown.py /path/to/oddmuse-data ./export --history
```

Where `/path/to/oddmuse-data` contains `page/` and optionally `keep/`. You can also pass `page/` itself as the input, though history export then expects its sibling `keep/`.

For wikis using the Markup Extension:

```bash
python3 oddmuse_to_markdown.py /path/to/oddmuse-data ./export \
    --history --dialect markup
```

For an older dump with percent-encoded filenames or a non-UTF-8 encoding:

```bash
python3 oddmuse_to_markdown.py /path/to/oddmuse-data ./export \
    --history --decode-filenames --encoding latin-1
```

Only enable `--decode-filenames` if filenames are actually percent-encoded; OddMuse normally uses the page IDs directly as filenames.

## Input layouts

**Recent layout:**

```text
oddmuse-data/
  page/
    HomePage.pg
    Another_Page.pg
  keep/
    HomePage/
      1.kp
      2.kp
```

**Older (sharded) layout:**

```text
oddmuse-data/
  page/
    H/HomePage.pg
    A/Another_Page.pg
    other/.hidden.pg
  keep/
    H/HomePage/1.kp
    A/Another_Page/1.kp
```

Records are not raw wiki text. They consist of `key: value` fields, with additional lines prefixed by a tab. The exporter extracts `text` and preserves relevant metadata in front matter. It handles both storage layouts without modifying files.

## Output

```text
export/
  INDEX.md
  manifest.json
  pages/
    HomePage.md
    Another_Page.md
  history/                    # only with --history
    HomePage/
      1.md
      2.md
  assets/                     # #FILE embedded uploads
    Picture.png
```

The pages include a first-level title and YAML front matter by default (`title`, `oddmuse_id`, revision, timestamps, author if known, summary if present). Use `--no-title` and/or `--no-frontmatter` for a different publishing pipeline.

`manifest.json` maps original OddMuse IDs to output files, including historical revisions, as well as unresolved page links. `INDEX.md` links all exported current pages.

## Conversion rules

Supports common OddMuse / UseMod conventions, including:

- Internal links `[[Page]]`, `[[Page_Name|Label]]`, `[[Page Name]]`, CamelCase wiki links, and `[WikiWord label]`.
- External links such as `[https://example.org label]`.
- Uploaded images `[[image:Picture]]` and downloads `[[download:Document]]` when their embedded `#FILE` content is available.
- `== Heading ==` and related equals-sign headings, lists with repeated `*` or `#`, horizontal rules, preformatted blocks, and `||cell||cell||` tables.
- UseMod-style `''italic''`, `'''bold'''`, and `'''''bold italic'''''`.
- OddMuse Markup Extension's `*bold*`, `//italic//`, `##code##`, when using `--dialect markup`.
- Wiki pages already beginning with `#MARKDOWN` (their Markdown is preserved).
- `#REDIRECT ...` pages, exported as Markdown pages pointing to their targets.
- `#FILE MIME [gzip]` uploads (base64, optionally gzip-compressed), extracted to `assets/` with a limit on decompressed size.

## Known limitations

This is a best-effort static migration, **not an implementation of OddMuse's Perl rendering engine**. Old installations can use different modules and custom markup, so review the output before republishing. In particular:

- Dynamic macros such as `<journal>`, `<include>`, RSS, and custom modules are left as literal text; they are not executed.
- Interwiki mappings, namespace extensions, special anchors, custom modules, and some complex table features may need post-processing.
- Wiki dialect options are approximate; check the modules installed by your old wiki (usually in `modules/`).
- Historical revisions are exported as separate Markdown files, not recreated as Git commits.
- Pages which exist *only* in `keep/` with no corresponding `page/` file are skipped and reported as warnings.
- Filename collisions are disambiguated with a short hash, and unresolved links are left identifiable in `[[...]]` notation. Review `manifest.json` for missing links.
- Archives may contain private wiki contents, author names, IP addresses, malicious uploads, or active content; review before publishing. The exporter does not reproduce host/IP metadata in front matter.

### Flags

```text
--history                 Export old revisions
--dialect usemod           Default; standard old UseMod/OddMuse formatting
--dialect markup           Additional Markup Extension formatting
--dialect creole           Common Creole-like inline formatting (partial)
--dialect plain            Minimal formatting; only links and generic blocks
--no-camelcase             Disable implicit CamelCase page links
--no-frontmatter           Exclude YAML metadata
--no-title                 Exclude Markdown H1
--decode-filenames         Decode percent-encoded source page filenames
--encoding ENCODING        Set original encoding (default UTF-8)
--max-asset-bytes N        Limit asset size (default 104857600 = 100 MiB)
--verbose                  Log additional information
```

### Run regression tests

```bash
python3 test_oddmuse_export.py -v
```

## Source references

- [OddMuse core](https://github.com/kensanata/oddmuse/blob/main/wiki.pl), `ParseData`, `EncodePage`, `GetPageFile`, `GetKeepFile`
- [Old OddMuse 2.2.6 implementation](https://github.com/kensanata/oddmuse/blob/main/t/oddmuse-2.2.6.pl), including sharded page and history paths
- [OddMuse Markdown converter module](https://github.com/kensanata/oddmuse/blob/main/modules/markdown-converter.pl)
- [OddMuse UseMod formatting extension](https://github.com/kensanata/oddmuse/blob/main/modules/usemod.pl)

## LICENSE

oddmuse-to-markdown is free software and licensed under the GNU General Public License.
