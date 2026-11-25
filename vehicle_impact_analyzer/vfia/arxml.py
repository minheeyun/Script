"""AUTOSAR ARXML 읽기(기능 나누는 기준용).

소스 폴더 아래(또는 설정 arxml 글롭)의 *.arxml을 찾아 다음을 모은다. 큰 시스템 기술 파일도 요소 단위로 읽고 버린다.
  * 원자 SWC 종류(APPLICATION, SENSOR-ACTUATOR, SERVICE, COMPLEX-DEVICE-DRIVER, ECU-ABSTRACTION ...)
    - Runnable(SHORT-NAME, SYMBOL=코드 함수 이름), 주기(TIMING-EVENT), 포트(P/R/PR), 데이터 송·수신
  * 컴포지션(COMPOSITION-SW-COMPONENT-TYPE): 포함한 SWC 종류(기능을 더 크게 묶을 때)
  * SWC 구현(SWC-IMPLEMENTATION): 동작(BEHAVIOR-REF)과 코드 파일(SHORT-LABEL) — 도구가 채운 경우만
"""
from __future__ import annotations

import fnmatch
import glob
import os
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

_SKIP_DIRS = {".git", ".svn", "__pycache__", "out", "build", "debug", "release", "obj", "bin"}
MAX_BYTES = 400 * 1024 * 1024


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(el, name: str):
    return [c for c in el if _local(c.tag) == name]


def _child_text(el, name: str) -> Optional[str]:
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip() or None
    return None


def _iter(el, name: str):
    for x in el.iter():
        if _local(x.tag) == name:
            yield x


def _last(ref: Optional[str], n: int = 1) -> Optional[str]:
    if not ref:
        return None
    parts = [p for p in ref.strip().split("/") if p]
    return parts[-n] if len(parts) >= n else None


def find_arxml(roots: List[str], patterns: Optional[List[str]] = None, limit: int = 3000) -> List[str]:
    """ARXML 파일 목록. patterns(글롭)가 있으면 그것만, 없으면 roots 아래를 찾는다(숨김·빌드 산출물 폴더 제외)."""
    found: List[str] = []
    if patterns:
        for p in patterns:
            if os.path.isdir(p):
                p = os.path.join(p, "**", "*.arxml")
            found += [x for x in glob.glob(p, recursive=True) if x.lower().endswith(".arxml")]
    else:
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            for d, dirs, files in os.walk(root):
                dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x.lower() not in _SKIP_DIRS)
                found += [os.path.join(d, f) for f in sorted(files) if fnmatch.fnmatch(f.lower(), "*.arxml")]
                if len(found) >= limit:
                    break
    seen, out = set(), []
    for f in found:
        k = os.path.normcase(os.path.abspath(f))
        if k not in seen:
            seen.add(k)
            out.append(os.path.abspath(f))
    return sorted(out)[:limit]


def _swc(el, tag: str, source: str) -> dict:
    name = _child_text(el, "SHORT-NAME")
    kind = tag.replace("-SW-COMPONENT-TYPE", "").lower()
    periods: Dict[str, float] = {}
    for ev in _iter(el, "TIMING-EVENT"):
        ref, per = None, None
        for c in ev.iter():
            t = _local(c.tag)
            if t == "START-ON-EVENT-REF":
                ref = (c.text or "").strip()
            elif t == "PERIOD":
                per = (c.text or "").strip()
        if ref and per:
            try:
                periods[_last(ref)] = float(per) * 1000.0
            except ValueError:
                pass
    runnables = []
    for re_ in _iter(el, "RUNNABLE-ENTITY"):
        rname = _child_text(re_, "SHORT-NAME")
        writes, reads = [], []
        for tag2, dst in (("DATA-WRITE-ACCESSS", writes), ("DATA-SEND-POINTS", writes), ("DATA-READ-ACCESSS", reads),
                          ("DATA-RECEIVE-POINT-BY-ARGUMENTS", reads), ("DATA-RECEIVE-POINT-BY-VALUES", reads)):
            for grp in _iter(re_, tag2):
                for va in _iter(grp, "VARIABLE-ACCESS"):
                    port = elem = None
                    for c in va.iter():
                        t = _local(c.tag)
                        if t == "PORT-PROTOTYPE-REF":
                            port = _last(c.text)
                        elif t == "TARGET-DATA-PROTOTYPE-REF":
                            elem = _last(c.text)
                    if port and elem:
                        dst.append(f"{port}_{elem}")
        runnables.append({"name": rname, "symbol": _child_text(re_, "SYMBOL") or rname, "period_ms": periods.get(rname),
                          "writes": writes, "reads": reads})
    ports = {"p": [], "r": []}
    for pt_tag, key in (("P-PORT-PROTOTYPE", "p"), ("R-PORT-PROTOTYPE", "r"), ("PR-PORT-PROTOTYPE", "p")):
        for p in _iter(el, pt_tag):
            n = _child_text(p, "SHORT-NAME")
            if n:
                ports[key].append(n)
    return {"name": name, "kind": kind, "runnables": runnables, "ports": ports, "source": source}


def parse_arxml_file(path: str) -> dict:
    """한 파일: {'swcs': {이름: ...}, 'compositions': {이름: [SWC 종류 이름]}, 'impl_files': {SWC: [파일]}, 'errors': []}"""
    out = {"swcs": {}, "compositions": {}, "impl_files": {}, "errors": []}
    try:
        size = os.path.getsize(path)
    except OSError as e:
        out["errors"].append(f"{path}: {e}")
        return out
    if size > MAX_BYTES:
        out["errors"].append(f"{path}: 파일이 너무 큼({size // (1024 * 1024)}MB) — 건너뜀")
        return out
    src = os.path.basename(path)
    stack: List[str] = []
    try:
        for ev, el in ET.iterparse(path, events=("start", "end")):
            tag = _local(el.tag)
            if ev == "start":
                stack.append(tag)
                continue
            stack.pop()
            if not stack or stack[-1] != "ELEMENTS":
                continue
            if tag == "COMPOSITION-SW-COMPONENT-TYPE":
                name = _child_text(el, "SHORT-NAME")
                comps = [_last(t.text) for p in _iter(el, "SW-COMPONENT-PROTOTYPE") for t in _iter(p, "TYPE-TREF")]
                if name:
                    out["compositions"][name] = sorted({c for c in comps if c})
            elif tag.endswith("-SW-COMPONENT-TYPE") and tag != "PARAMETER-SW-COMPONENT-TYPE":
                s = _swc(el, tag, src)
                if s["name"]:
                    out["swcs"][s["name"]] = s
            elif tag == "SWC-IMPLEMENTATION":
                beh = None
                for c in _iter(el, "BEHAVIOR-REF"):
                    beh = (c.text or "").strip()
                swc = _last(beh, 2)
                files = []
                for eo in _iter(el, "AUTOSAR-ENGINEERING-OBJECT"):
                    lab, cat = _child_text(eo, "SHORT-LABEL"), (_child_text(eo, "CATEGORY") or "").upper()
                    if lab and cat in ("", "SWSRC", "SWHDR", "SWOBJ"):
                        files.append(lab)
                if swc and files:
                    out["impl_files"].setdefault(swc, []).extend(files)
            el.clear()
    except ET.ParseError as e:
        out["errors"].append(f"{os.path.basename(path)}: XML 오류 {e}")
    except OSError as e:
        out["errors"].append(f"{path}: {e}")
    return out


def vehicle_model_arxml(cfg, version: str) -> List[str]:
    """차량 모델 파일에 적힌 ARXML(arxml: [...]) 경로."""
    from .config import load_structured
    out: List[str] = []
    for p in cfg.vehicle_model_for(version) or []:
        try:
            raw = load_structured(p) or {}
        except Exception:
            continue
        base = os.path.dirname(os.path.abspath(p))
        for x in raw.get("arxml") or []:
            xp = x if os.path.isabs(x) else os.path.join(base, x)
            if os.path.isfile(xp):
                out.append(os.path.abspath(xp))
    return out


def _arxml_sources(cfg, version: str):
    v = cfg.version(version)
    found = find_arxml([v.root], (v.arxml or cfg.arxml) or None)
    seen = {os.path.normcase(x) for x in found}
    extra = []
    for x in vehicle_model_arxml(cfg, version):
        if os.path.normcase(x) not in seen:
            seen.add(os.path.normcase(x))
            extra.append(x)
    return found, extra


def arxml_files_for_version(cfg, version: str) -> List[str]:
    """versions.<버전>.arxml > arxml > 버전 root 아래 자동 탐색, 그리고 차량 모델에 적힌 ARXML."""
    found, extra = _arxml_sources(cfg, version)
    return found + extra


def arxml_for_version(cfg, version: str) -> Optional[dict]:
    """설정의 버전에 해당하는 ARXML을 읽은 결과. 없으면 None."""
    v = cfg.version(version)
    found, extra = _arxml_sources(cfg, version)
    if not found and not extra:
        return None
    res = load_arxml(found + extra)
    parts = ["설정 arxml" if (v.arxml or cfg.arxml) else f"자동 탐색 {v.root}"] if found else []
    res["search"] = " + ".join(parts + ([f"차량 모델 {len(extra)}개"] if extra else []))
    return res


def load_arxml(paths: List[str]) -> dict:
    """여러 ARXML을 합친다. SWC 이름이 같으면 Runnable을 합친다(분할 기술 파일)."""
    res = {"files": [], "swcs": {}, "compositions": {}, "impl_files": {}, "errors": []}
    for p in paths:
        r = parse_arxml_file(p)
        res["files"].append({"path": p, "swcs": len(r["swcs"]), "runnables": sum(len(s["runnables"]) for s in r["swcs"].values()),
                             "compositions": len(r["compositions"])})
        res["errors"] += r["errors"]
        for n, s in r["swcs"].items():
            e = res["swcs"].setdefault(n, {"name": n, "kind": s["kind"], "runnables": [], "ports": {"p": [], "r": []},
                                           "sources": []})
            known = {x["name"] for x in e["runnables"]}
            e["runnables"] += [x for x in s["runnables"] if x["name"] not in known]
            for k in ("p", "r"):
                e["ports"][k] = sorted(set(e["ports"][k]) | set(s["ports"][k]))
            e["sources"].append(s["source"])
        for n, c in r["compositions"].items():
            res["compositions"][n] = sorted(set(res["compositions"].get(n, [])) | set(c))
        for n, f in r["impl_files"].items():
            res["impl_files"][n] = sorted(set(res["impl_files"].get(n, [])) | set(f))
    # SWC -> 들어 있는 가장 바깥 컴포지션(기능을 크게 묶을 때)
    parent: Dict[str, str] = {}
    for comp, members in res["compositions"].items():
        for m in members:
            parent.setdefault(m, comp)
    top: Dict[str, str] = {}
    for s in res["swcs"]:
        x, guard = s, 0
        while x in parent and guard < 20:
            x, guard = parent[x], guard + 1
        if x != s:
            top[s] = x
    res["top_composition"] = top
    return res
