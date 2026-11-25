"""기능 나누는 기준 진단: 폴더 구조·ARXML(SWC/Runnable)·매핑 파일 중 무엇으로 기능을 나눌지, ARXML을 쓸 수 있는지.

  quick_diagnose  코드 분석(파싱) 전에 파일만 보고 판단한다. 소스 텍스트에서 함수 정의를 찾아 ARXML Runnable
                  SYMBOL과 맞춰 보고(연결률), 소스 경로로 모듈 폴더를 센다. new_project와 tools/feature_basis.py --quick.
  full_diagnose   분석 그래프(파싱 결과) 위에서 실제 매핑(FeatureMapping)을 만들어 기능별 함수와 근거까지 보인다.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from . import portable as pt
from .arxml import arxml_files_for_version, find_arxml, load_arxml
from .build_env import function_definitions_in_text
from .feature_impact import BASIS_LABEL, module_keys

_SRC_EXT = (".c", ".cc", ".cpp")


def _source_files(root: str, compile_db: Optional[str]) -> List[str]:
    """분석할 소스('/' 상대 경로): compile DB가 있으면 그 목록, 없으면 root 아래 .c."""
    out: List[str] = []
    if compile_db and os.path.isfile(compile_db):
        try:
            db = json.loads(pt.read_text_any(compile_db))
            base = os.path.dirname(os.path.abspath(compile_db))
            for e in db:
                d = e.get("directory") or base
                if not os.path.isabs(d):
                    d = os.path.normpath(os.path.join(base, d))
                f = e.get("file") or ""
                f = os.path.normpath(f if os.path.isabs(f) else os.path.join(d, f))
                r = pt.rel_posix(f, root)
                if r:
                    out.append(r)
        except (OSError, ValueError):
            out = []
    if not out:
        skip = {"debug", "release", "build", "out", "obj", "bin"}
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x.lower() not in skip)
            out += [pt.rel_posix(os.path.join(d, f), root) for f in sorted(files) if f.lower().endswith(_SRC_EXT)]
    return sorted({x for x in out if x})


def quick_for_config(cfg, version: str, basis: Optional[str] = None) -> dict:
    from .feature_impact import load_feature_map
    v = cfg.version(version)
    has_map = False
    if cfg.feature_map and os.path.isfile(cfg.feature_map):
        try:
            sp = load_feature_map(cfg.feature_map)
            has_map = bool(sp["features"] or sp["structure"] or sp["overrides"])
        except Exception:
            has_map = False
    return quick_diagnose(v.root, v.compile_commands, None, basis or cfg.feature_basis, has_map,
                          arxml_files=arxml_files_for_version(cfg, version))


def quick_diagnose(root: str, compile_db: Optional[str] = None, arxml_patterns: Optional[List[str]] = None,
                   basis: str = "auto", has_map: bool = False, extra_arxml: Optional[List[str]] = None,
                   arxml_files: Optional[List[str]] = None) -> dict:
    """arxml_files가 있으면 그 목록을 그대로 쓰고, 없으면 root 아래(또는 arxml_patterns)에서 찾는다."""
    files = _source_files(root, compile_db)
    defined: Dict[str, str] = {}
    for rel in files:
        try:
            text = pt.read_text_any(pt.native(root, rel))
        except OSError:
            continue
        for name in function_definitions_in_text(text, []):
            defined.setdefault(name, rel)
    if arxml_files is not None:
        ax_files = list(arxml_files)
    else:
        ax_files = find_arxml([root], arxml_patterns or None)
        ax_files += [x for x in extra_arxml or [] if x not in ax_files]
    ax = load_arxml(ax_files) if ax_files else {"swcs": {}, "compositions": {}, "errors": [], "files": []}
    runs = [(s, r["name"], r.get("symbol") or r["name"]) for s, sd in ax["swcs"].items() for r in sd["runnables"]]
    linked = [x for x in runs if x[2] in defined]
    ratio = len(linked) / len(runs) if runs else 0.0
    swcs_linked = sorted({s for s, _, _ in linked})
    mods = module_keys(files)
    modules = sorted({v[2] for v in mods.values() if v[1] == "structure"})
    state = "없음" if not runs else ("사용 가능" if ratio >= 0.5 else ("부분 사용" if linked else "사용 불가"))
    if basis != "auto":
        chosen = basis
    elif state in ("사용 가능", "부분 사용"):
        chosen = "arxml"
    elif len(modules) >= 2:
        chosen = "folder"
    else:
        chosen = "file"
    homes = sorted({mods[defined[sym]][2] for _, _, sym in linked
                    if defined.get(sym) in mods and mods[defined[sym]][1] == "structure"})
    return {"root": root, "source_files": len(files), "functions": len(defined), "arxml_files": [os.path.basename(x) for x in ax_files],
            "arxml_errors": ax["errors"][:5], "swcs": len(ax["swcs"]), "swcs_linked": swcs_linked,
            "compositions": len(ax["compositions"]), "runnables": len(runs), "linked": len(linked), "ratio": round(ratio, 3),
            "unlinked": [{"swc": s, "runnable": r, "symbol": y} for s, r, y in runs if y not in defined][:10],
            "arxml_state": state, "modules": len(modules), "module_examples": modules[:10], "swc_home_folders": homes[:10],
            "file_only": sum(1 for v in mods.values() if v[1] == "file"), "basis": chosen, "basis_label": BASIS_LABEL[chosen],
            "has_map": has_map}


def quick_text(d: dict) -> List[str]:
    out = [f"소스 {d['source_files']}개, 함수 정의 {d['functions']}개 ({d['root']})"]
    if d["arxml_files"]:
        out.append(f"ARXML {len(d['arxml_files'])}개({', '.join(d['arxml_files'][:3])}{' …' if len(d['arxml_files']) > 3 else ''}): "
                   f"SWC {d['swcs']}개, 컴포지션 {d['compositions']}개, Runnable {d['runnables']}개 중 코드에 정의가 있는 것 "
                   f"{d['linked']}개({d['ratio']:.0%}) → {d['arxml_state']}")
        if d["unlinked"]:
            out.append("  코드에서 찾지 못한 Runnable: " + ", ".join(f"{u['runnable']}(SYMBOL {u['symbol']})" for u in d["unlinked"][:5]))
        if d["swc_home_folders"]:
            out.append("  SWC Runnable이 있는 폴더: " + ", ".join(d["swc_home_folders"][:6]))
    else:
        out.append("ARXML 없음(소스 폴더 아래에서 찾지 못함 — 다른 곳에 있으면 설정 arxml에 위치 지정)")
    out.append(f"폴더 구조: 모듈 폴더 {d['modules']}개" + (f"(예: {', '.join(d['module_examples'][:5])})" if d["modules"] else "")
               + (f", 폴더 구분 없이 파일 이름으로만 나뉘는 파일 {d['file_only']}개" if d["file_only"] else ""))
    out.append(f"→ 기능 나누는 기준: {d['basis_label']}" + (" (매핑 파일 규칙을 먼저 적용)" if d["has_map"] else ""))
    return out


def full_diagnose(cfg, version: str, basis: Optional[str] = None, log=print) -> dict:
    """분석 그래프를 만들고(캐시 재사용) 실제 매핑을 계산한다."""
    from .arxml import arxml_for_version
    from .feature_impact import FeatureMapping, load_feature_map
    from .pipeline import get_graph
    g = get_graph(cfg, version, log=log)
    spec = load_feature_map(cfg.feature_map)
    m = FeatureMapping(g, spec, arxml_for_version(cfg, version), basis or cfg.feature_basis)
    feats: Dict[str, dict] = {}
    for f, a in m.func.items():
        e = feats.setdefault(a["feature"], {"functions": [], "sources": {}})
        e["functions"].append(g.node(f).get("name"))
        e["sources"][a["source"]] = e["sources"].get(a["source"], 0) + 1
    return {"decision": m.decision, "features": feats, "mapping": m, "graph": g, "spec": spec}
