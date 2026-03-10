from __future__ import annotations

import argparse
import html
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from _util import now_iso_z, norm_space, read_json, write_json


FIELD_ID_RE = re.compile(
    r"^(?P<section>Contents/section\d+\.xml):tbl:(?P<table_id>\d+):r(?P<row>\d+):c(?P<col>\d+)$"
)

CELLADDR_RE_TEMPLATE = rb"<hp:cellAddr[^>]*\bcolAddr=\"{col}\"[^>]*\browAddr=\"{row}\"[^>]*/>"


def _find_tbl_bounds(xml: bytes, table_id: str) -> tuple[int, int] | None:
    needle = (f'<hp:tbl id="{table_id}"').encode("utf-8")
    start = xml.find(needle)
    if start == -1:
        return None

    open_tag = b"<hp:tbl"
    close_tag = b"</hp:tbl>"
    depth = 0
    i = start
    while True:
        j_open = xml.find(open_tag, i)
        j_close = xml.find(close_tag, i)
        if j_close == -1:
            return None
        if j_open != -1 and j_open < j_close:
            depth += 1
            i = j_open + len(open_tag)
            continue
        depth -= 1
        i = j_close + len(close_tag)
        if depth == 0:
            return (start, i)


def _find_cell_sublist_bounds(
    xml: bytes,
    *,
    tbl_bounds: tuple[int, int],
    row: int,
    col: int,
) -> tuple[int, int] | None:
    tbl_start, tbl_end = tbl_bounds
    tbl = xml[tbl_start:tbl_end]

    pat = re.compile(
        CELLADDR_RE_TEMPLATE.replace(b"{col}", str(col).encode("ascii")).replace(
            b"{row}", str(row).encode("ascii")
        )
    )
    m = pat.search(tbl)
    if not m:
        return None

    addr_pos = tbl_start + m.start()

    tc_start = xml.rfind(b"<hp:tc", tbl_start, addr_pos)
    if tc_start == -1:
        return None

    sub_start = xml.find(b"<hp:subList", tc_start, addr_pos)
    if sub_start == -1:
        return None
    sub_end = xml.find(b"</hp:subList>", sub_start, addr_pos)
    if sub_end == -1:
        return None
    sub_end += len(b"</hp:subList>")

    return (sub_start, sub_end)


_RE_LINEBREAK = re.compile(r"<hp:lineBreak[^>]*/>", flags=re.IGNORECASE)
_RE_TAB = re.compile(r"<hp:tab[^>]*/>", flags=re.IGNORECASE)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_P_CLOSE = re.compile(r"</hp:p>", flags=re.IGNORECASE)


def _extract_text_from_sublist(sub: bytes) -> str:
    s = sub.decode("utf-8", errors="ignore")
    # Preserve paragraph boundaries crudely.
    s = _RE_P_CLOSE.sub("\n", s)
    # Preserve inline breaks/tabs.
    s = _RE_LINEBREAK.sub("\n", s)
    s = _RE_TAB.sub("\t", s)
    s = _RE_TAG.sub("", s)
    s = html.unescape(s)
    s = s.replace("\t", "    ")

    # Normalize spaces within lines, but keep intentional newlines.
    lines = [re.sub(r"[ \u00a0]+", " ", ln).rstrip() for ln in s.splitlines()]
    # Trim leading/trailing empty lines.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln.strip():
            if not blank:
                out.append("")
            blank = True
            continue
        out.append(ln.strip())
        blank = False

    return "\n".join(out).strip()


def _extract_checked_options(text: str) -> list[str]:
    flat = norm_space(text)
    hits = [(m.start(), m.group(0)) for m in re.finditer(r"[☐☑□■]", flat)]
    if len(hits) < 1:
        return []
    checked = {"☑", "■"}
    out: list[str] = []
    for i, (pos, mark) in enumerate(hits):
        start = pos + 1
        end = hits[i + 1][0] if i + 1 < len(hits) else len(flat)
        label = norm_space(flat[start:end])
        if not label:
            continue
        if mark in checked:
            out.append(label)
    return out


def _read_extras(path: str | None) -> list[str]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(obj, list):
        return [str(x) for x in obj if str(x).strip()]
    if isinstance(obj, dict) and isinstance(obj.get("ids"), list):
        return [str(x) for x in obj["ids"] if str(x).strip()]
    return []


def _extract_fields(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict) and isinstance(obj.get("fields"), dict):
        return obj["fields"]
    if isinstance(obj, dict):
        return obj
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Import fields.json from an existing filled .hwpx/.hwtx using spec.json ids.")
    ap.add_argument("--spec", required=True, help="spec.json path")
    ap.add_argument("--hwpx", required=True, help="Source filled .hwpx/.hwtx path")
    ap.add_argument("--out", required=True, help="Output fields.imported.json")
    ap.add_argument("--extras", help="Optional JSON list of extra field ids to import")
    args = ap.parse_args()

    spec = read_json(args.spec)
    spec_fields = spec.get("fields", [])
    if not isinstance(spec_fields, list):
        raise SystemExit("Invalid spec.json: fields must be a list")

    kind_by_id: dict[str, str] = {}
    wanted: list[str] = []
    seen: set[str] = set()
    for f in spec_fields:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        if not isinstance(fid, str) or not fid:
            continue
        kind_by_id[fid] = str(f.get("kind") or "text")
        if fid not in seen:
            wanted.append(fid)
            seen.add(fid)

    for fid in _read_extras(args.extras):
        if fid not in seen:
            wanted.append(fid)
            seen.add(fid)

    src = Path(args.hwpx).resolve()
    out = Path(args.out).resolve()
    if not src.exists():
        raise SystemExit(f"Source not found: {src}")

    fields_out: dict[str, Any] = {}
    counts = {"wanted": len(wanted), "matched": 0, "missing": 0}

    # Cache section xml + table bounds per section.
    section_xml: dict[str, bytes] = {}
    table_bounds: dict[tuple[str, str], tuple[int, int]] = {}

    with zipfile.ZipFile(src, "r") as z:
        for fid in wanted:
            m = FIELD_ID_RE.match(fid)
            if not m:
                counts["missing"] += 1
                continue

            sec = m.group("section")
            table_id = m.group("table_id")
            row = int(m.group("row"))
            col = int(m.group("col"))

            xml = section_xml.get(sec)
            if xml is None:
                try:
                    xml = z.read(sec)
                except KeyError:
                    counts["missing"] += 1
                    continue
                section_xml[sec] = xml

            b = table_bounds.get((sec, table_id))
            if b is None:
                b = _find_tbl_bounds(xml, table_id)
                if b is None:
                    counts["missing"] += 1
                    continue
                table_bounds[(sec, table_id)] = b

            sub_bounds = _find_cell_sublist_bounds(xml, tbl_bounds=b, row=row, col=col)
            if sub_bounds is None:
                counts["missing"] += 1
                continue
            sub = xml[sub_bounds[0] : sub_bounds[1]]

            text = _extract_text_from_sublist(sub)
            kind = kind_by_id.get(fid, "text")
            if kind == "checkbox_group":
                fields_out[fid] = _extract_checked_options(text)
            else:
                fields_out[fid] = text
            counts["matched"] += 1

    write_json(
        out,
        {
            "generated_at": now_iso_z(),
            "imported_from": {"spec": str(Path(args.spec).resolve()), "hwpx": str(src)},
            "counts": counts,
            "fields": fields_out,
        },
    )
    print(f"OK: wrote {out} ({len(fields_out)} fields)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

