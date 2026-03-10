from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml

from _util import now_iso_z, read_json, write_json


PLACEHOLDER_RE = re.compile(
    r"(홍길동|고길동|\bOO\b|OOOOOO|00년|00월|00일|C12345|제00-|IATF|ISO14001|KC\s*인증)"
)


def _extract_fields(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict) and isinstance(obj.get("fields"), dict):
        return obj["fields"]
    if isinstance(obj, dict):
        return obj
    return {}


def _is_placeholder_value(v: Any, *, original: str = "") -> bool:
    if v is None:
        return True
    if isinstance(v, list):
        # Checkbox group: treat missing selection as placeholder.
        return len([x for x in v if str(x).strip()]) == 0
    s = str(v).strip()
    if not s:
        return True
    if original and s == str(original).strip():
        return True
    # Narrative instructions and sample placeholders.
    if "※" in s or "예시" in s:
        return True
    if PLACEHOLDER_RE.search(s):
        return True
    return False


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        obj = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Sanitize/auto-fill placeholder fields for submit-ready output.")
    ap.add_argument("--spec", required=True, help="spec.json path")
    ap.add_argument("--fields", required=True, help="fields json path (auto/import/merged)")
    ap.add_argument("--out", required=True, help="Output sanitized fields json path")
    ap.add_argument("--snippets", help="Optional YAML snippets (default: profile/dotori_newventure_snippets.yml if present)")
    args = ap.parse_args()

    spec = read_json(args.spec)
    fields_obj = read_json(args.fields)
    fields_in = _extract_fields(fields_obj)

    # Default snippet location (project-relative) if not provided.
    snippets_path = Path(args.snippets).resolve() if args.snippets else None
    if snippets_path is None:
        biz_root = Path(__file__).resolve().parents[2]
        cand = biz_root / "profile" / "dotori_newventure_snippets.yml"
        snippets_path = cand if cand.exists() else None

    snippets: dict[str, str] = {}
    overrides_by_id: dict[str, str] = {}
    if snippets_path:
        y = _load_yaml(snippets_path)
        sn = y.get("snippets")
        if isinstance(sn, dict):
            snippets = {str(k): str(v) for k, v in sn.items() if str(k).strip()}
        ov = y.get("overrides_by_id")
        if isinstance(ov, dict):
            overrides_by_id = {str(k): str(v) for k, v in ov.items() if str(k).strip()}

    # Map spec fields -> snippet keys, including unlabeled duplicates based on original_text.
    def snippet_key_for_field(label: str, original: str) -> str | None:
        label = (label or "").strip()
        original = (original or "").strip()
        if label in snippets:
            return label

        # Unlabeled duplicates: match by the original placeholder/instruction text.
        if "개요(사용 용도" in original or "고객 제공 혜택" in original:
            return "아이템 개요" if "아이템 개요" in snippets else None
        if "핵심기술 역량" in original:
            return "핵심 인력기술 역량" if "핵심 인력기술 역량" in snippets else None
        if "기술 개발/개선 필요성과" in original or "목표시장(고객) 설정" in original:
            return "배경 및필요성" if "배경 및필요성" in snippets else None
        if "현재 개발·구현 수준" in original or "사업화 진행 수준" in original:
            return "아이템 현황" if "아이템 현황" in snippets else None
        if "사업 참여 이전" in original or "협약 기간 내" in original:
            return "기술고도화및 사업화 방안" if "기술고도화및 사업화 방안" in snippets else None
        if "수익화 모델" in original or "시장진입 방안" in original:
            return "목표시장 진출방안" if "목표시장 진출방안" in snippets else None
        if "참고 사진" in original or "설계도" in original:
            # Use either the explicit title placeholder key or the generic image key if present.
            if "< 사진(이미지) 또는 설계도 제목 >" in snippets:
                return "< 사진(이미지) 또는 설계도 제목 >"
            if "이미지" in snippets:
                return "이미지"
        return None

    fields_out: dict[str, Any] = dict(fields_in)

    updated = 0

    # If the user already filled a primary narrative cell (e.g., "아이템 개요") in fields.json,
    # reuse it as the snippet source to also fill unlabeled duplicates in the template.
    if "아이템 개요" not in snippets:
        for f in spec.get("fields", []):
            if not isinstance(f, dict):
                continue
            if str(f.get("label") or "").strip() != "아이템 개요":
                continue
            fid = f.get("id")
            if not isinstance(fid, str) or not fid:
                continue
            cur = fields_out.get(fid)
            if not _is_placeholder_value(cur, original=str(f.get("original_text") or "")):
                snippets["아이템 개요"] = str(cur)
                break

    for f in spec.get("fields", []):
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        if not isinstance(fid, str) or not fid:
            continue

        label = str(f.get("label") or "")
        original = str(f.get("original_text") or "")

        key = snippet_key_for_field(label, original)
        if not key:
            continue

        desired = snippets.get(key)
        if desired is None:
            continue

        cur = fields_out.get(fid)
        if _is_placeholder_value(cur, original=original):
            fields_out[fid] = desired
            updated += 1

    # Apply explicit overrides (even if not present in spec).
    for fid, desired in overrides_by_id.items():
        cur = fields_out.get(fid)
        if _is_placeholder_value(cur, original="") or PLACEHOLDER_RE.search(str(cur or "")) or ("※" in str(cur or "")):
            fields_out[fid] = desired
            updated += 1

    write_json(
        Path(args.out),
        {
            "generated_at": now_iso_z(),
            "sanitized_from": {"spec": str(Path(args.spec).resolve()), "fields": str(Path(args.fields).resolve())},
            "updated": updated,
            "fields": fields_out,
        },
    )
    print(f"OK: wrote {args.out} (updated {updated} fields)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
