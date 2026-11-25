"""기능(Feature) 단위 영향: 함수 단위 영향을 SW 기능 묶음으로 모아 '이 변경이 어떤 기능에 영향을 주는가'를 보인다.

1. 함수 → 기능 매핑(앞의 것이 우선, 함수마다 근거를 남긴다)
     개별 지정(매핑 파일 overrides)  >  매핑 파일 규칙(함수 이름 → 경로 → SWC)  >  폴더 구조 규칙('AP/Function/{feature}/**')
     >  AUTOSAR(그 SWC의 Runnable에서만 호출되는 함수 = SWC 전용)  >  자동 폴더 구조('AP/Function/PWM/src' → PWM)
     >  파일 이름
   변수(메모리 위치)는 정의된 파일 기준으로 같은 규칙을 쓰고, 파일을 모르면 가장 많이 쓰는 함수의 기능.
2. 기능 간 의존: 함수 호출, 공유 변수(쓰는 기능 → 읽는 기능), RTE·CAN 신호(보내는 기능 → 받는 기능)를 기능 쌍으로
   모은 행렬. 변경 전·후를 비교한다.
3. 변경 영향 전파: 변경의 전방 슬라이스(함수·변수·신호 단위)를 기능 경계를 넘을 때마다 한 단계로 센다.
     단계 0 = 변경 기능, 1 = 직접 영향(변경 기능과 바로 연결), 2 이상 = 간접 영향, 끝에 외부 출력(신호·레지스터·관찰 대상)
   단계마다 연결 근거(변수·신호·호출 이름)를 함께 남긴다.
4. 회귀 시험 범위: 영향받는 기능마다 구분(변경/직접/간접), 이유(전파 경로), 시험 대상(변경·영향 함수, 입력, 관찰할 출력,
   매핑 파일에 적은 시험 케이스, 동작 차이 조건)을 제안한다.
"""
from __future__ import annotations

import csv
import html
import os
import posixpath
import re
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

from .config import load_structured
from .impact_graph import (CALL, OBSERVE, SHARED, SIGNAL, Abstraction, ProgramGraph, _impact_side, change_centers)
from .model import EK, NK, AnalysisGraph, Status, loc_display

GENERIC_DIRS = {"src", "source", "sources", "inc", "include", "includes", "cfg", "config", "code", "c", "impl",
                "implementation", "private", "public", "api", "lib", "core", "common"}
TIER = {0: "변경 기능", 1: "직접 영향", 2: "간접 영향"}
SOURCE_LABEL = {"override": "매핑 파일(개별 지정)", "map_function": "매핑 파일(함수 이름)", "map_path": "매핑 파일(경로)",
                "map_swc": "매핑 파일(SWC)", "structure_rule": "폴더 구조 규칙", "autosar": "AUTOSAR(SWC 전용 함수)",
                "structure": "폴더 구조", "file": "파일 이름", "writer": "쓰는 함수의 기능"}
_DIFF_STATUS = {Status.EXEC_DIFF, Status.MODEL_DIFF}


def tier_of(stage: int) -> int:
    return 0 if stage <= 0 else (1 if stage == 1 else 2)


# ---------------------------------------------------------------------------
# 매핑 파일
# ---------------------------------------------------------------------------
def glob_to_regex(pat: str) -> "re.Pattern":
    """경로 글롭('AP/Function/PWM/**', '**/Pwm*.c', 'src/a.c') -> 정규식(대소문자 무시, '/'·'\\\\' 모두)."""
    p = pat.replace("\\", "/").strip()
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif p.startswith("**", i):
            out += ".*"
            i += 2
        elif p[i] == "*":
            out += "[^/]*"
            i += 1
        elif p[i] == "?":
            out += "[^/]"
            i += 1
        elif p.startswith("{feature}", i):
            out += "(?P<feature>[^/]+)"
            i += len("{feature}")
        else:
            out += re.escape(p[i])
            i += 1
    if not p.endswith(("*", "}")) and "." not in posixpath.basename(p):
        out += "(?:/.*)?"            # 폴더 이름만 주면 그 아래 전부
    return re.compile(f"^(?:\\./)?{out}$", re.I)


def _list(v) -> List[str]:
    """YAML 값 하나 또는 목록 -> 문자열 목록('paths: src/a.c'처럼 하나만 써도 됨)."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


def load_feature_map(path: Optional[str]) -> dict:
    """매핑 파일(YAML/JSON)을 읽어 정규화. 없으면 빈 명세(자동 매핑만)."""
    spec = {"features": [], "structure": [], "ignore": [], "overrides": {}, "source": None}
    if not path:
        return spec
    raw = load_structured(path) or {}
    spec["source"] = path
    for f in raw.get("features") or []:
        if not isinstance(f, dict) or not f.get("id"):
            raise ValueError(f"매핑 파일 {path}: features 항목마다 id가 필요합니다: {f}")
        spec["features"].append({
            "id": str(f["id"]), "name": str(f.get("name") or f["id"]),
            "functions": _list(f.get("functions")), "paths": _list(f.get("paths")),
            "swcs": _list(f.get("swcs")), "tests": _list(f.get("tests")),
            "description": f.get("description"),
        })
    spec["structure"] = _list(raw.get("structure"))
    spec["ignore"] = _list(raw.get("ignore"))
    spec["overrides"] = {str(k): str(v) for k, v in (raw.get("overrides") or {}).items()}
    return spec


# ---------------------------------------------------------------------------
# 함수 → 기능
# ---------------------------------------------------------------------------
class FeatureMapping:
    """한 버전 그래프의 함수·변수 → 기능 매핑(근거 포함)."""

    def __init__(self, g: AnalysisGraph, spec: dict):
        self.g = g
        self.spec = spec
        self.func: Dict[str, dict] = {}      # fn 키 -> {feature, source, evidence}
        self.var: Dict[str, str] = {}        # loc 키 -> feature
        self.ignored: Set[str] = set()
        self.names: Dict[str, str] = {f["id"]: f["name"] for f in spec["features"]}
        self.notes: List[str] = []
        self._fn_rx = [(f["id"], pat, re.compile(_fn_glob(pat))) for f in spec["features"] for pat in f["functions"]]
        self._path_rx = [(f["id"], pat, glob_to_regex(pat)) for f in spec["features"] for pat in f["paths"]]
        self._swc_feat = {s: f["id"] for f in spec["features"] for s in f["swcs"]}
        self._struct_rx = [(pat, glob_to_regex(pat)) for pat in spec["structure"]]
        self._ignore_rx = [glob_to_regex(p) for p in spec["ignore"]]
        self.functions = [f for f in g.nodes_of(NK.FUNCTION) if not g.node(f).get("external")]
        self._autosar()
        self._auto_structure()
        for f in self.functions:
            self._assign(f)
        self._assign_variables()

    # ---- AUTOSAR: SWC 전용 함수 ----
    def _autosar(self) -> None:
        g = self.g
        self.swc_reach: Dict[str, Set[str]] = {}
        self.swc_entries: Dict[str, Dict[str, str]] = defaultdict(dict)   # swc -> {entry fn: runnable}
        for t in g.nodes_of(NK.TASK):
            d = g.node(t)
            if d.get("ctx_kind") != "runnable" or not d.get("swc"):
                continue
            for e in g.succ(t, [EK.ACTIVATES]):
                self.swc_entries[d["swc"]][e] = d.get("name")
        for swc, entries in self.swc_entries.items():
            seen, q = set(), deque(entries)
            while q:
                f = q.popleft()
                if f in seen:
                    continue
                seen.add(f)
                q.extend(c for c in g.succ(f, [EK.CALLS]) if g.has_node(c) and not g.node(c).get("external"))
            self.swc_reach[swc] = seen
        self.swc_only: Dict[str, Tuple[str, str]] = {}   # fn -> (swc, 근거)
        for f in self.functions:
            owners = [s for s, r in self.swc_reach.items() if f in r]
            if len(owners) != 1:
                continue
            s = owners[0]
            callers = [c for c in g.pred(f, [EK.CALLS]) if g.kind(c) == NK.FUNCTION]
            if f in self.swc_entries[s]:
                self.swc_only[f] = (s, f"Runnable {self.swc_entries[s][f]}의 진입 함수")
            elif callers and all(c in self.swc_reach[s] for c in callers):
                runs = sorted(set(self.swc_entries[s].values()))
                self.swc_only[f] = (s, f"{s}의 Runnable({', '.join(runs[:3])})에서만 호출")

    # ---- 자동 폴더 구조 ----
    def _auto_structure(self) -> None:
        files = sorted({self._file(f) for f in self.functions if self._file(f)})
        dirs = [x.split("/")[:-1] for x in files]
        common: List[str] = []
        if dirs:
            for parts in zip(*dirs):
                if len(set(p.lower() for p in parts)) == 1:
                    common.append(parts[0])
                else:
                    break
        self._common = len(common)
        keys: Dict[str, Tuple[str, str, str]] = {}
        by_name: Dict[str, Set[str]] = defaultdict(set)
        for x in files:
            parts = x.split("/")
            rest = parts[self._common:-1]
            while rest and rest[-1].lower() in GENERIC_DIRS:
                rest = rest[:-1]
            if not rest:
                stem = posixpath.splitext(parts[-1])[0]
                keys[x] = (stem, "file", parts[-1])
            else:
                d = "/".join(parts[:self._common + len(rest)])
                keys[x] = (rest[-1], "structure", d)
                by_name[rest[-1].lower()].add(d)
        # 이름이 같은 다른 폴더('AP/Function/PWM'와 'BSW/Mcal/Pwm')는 위 폴더 이름을 붙여 구분
        for x, (name, src, ev) in list(keys.items()):
            if src == "structure" and len(by_name[name.lower()]) > 1:
                parts = ev.split("/")
                keys[x] = ("/".join(parts[-2:]) if len(parts) >= 2 else name, src, ev)
        self._auto_keys = keys

    def _file(self, f: str) -> Optional[str]:
        return (self.g.node(f).get("file") or "").replace("\\", "/") or None

    def _by_path(self, path: str) -> Optional[Tuple[str, str, str]]:
        for fid, pat, rx in self._path_rx:
            if rx.match(path):
                return fid, "map_path", pat
        for pat, rx in self._struct_rx:
            m = rx.match(path)
            if m and m.groupdict().get("feature"):
                return m.group("feature"), "structure_rule", pat
        return None

    def _assign(self, f: str) -> None:
        g = self.g
        name = g.node(f).get("name") or f.split(":")[-1]
        path = self._file(f) or ""
        if name in self.spec["overrides"]:
            self.func[f] = {"feature": self.spec["overrides"][name], "source": "override", "evidence": name}
            return
        if path and any(rx.match(path) for rx in self._ignore_rx):
            self.ignored.add(f)
            return
        for fid, pat, rx in self._fn_rx:
            if rx.match(name):
                self.func[f] = {"feature": fid, "source": "map_function", "evidence": pat}
                return
        hit = self._by_path(path) if path else None
        if hit and hit[1] == "map_path":
            self.func[f] = {"feature": hit[0], "source": "map_path", "evidence": hit[2]}
            return
        if f in self.swc_only and self.swc_only[f][0] in self._swc_feat:
            s, why = self.swc_only[f]
            self.func[f] = {"feature": self._swc_feat[s], "source": "map_swc", "evidence": f"{s}: {why}"}
            return
        if hit:
            self.func[f] = {"feature": hit[0], "source": "structure_rule", "evidence": hit[2]}
            return
        if f in self.swc_only:
            s, why = self.swc_only[f]
            self.func[f] = {"feature": s, "source": "autosar", "evidence": why}
            return
        if path in self._auto_keys:
            key, src, ev = self._auto_keys[path]
            self.func[f] = {"feature": key, "source": src, "evidence": ev}
            return
        self.func[f] = {"feature": "(위치 모름)", "source": "file", "evidence": "파일 정보 없음"}

    def feature_of_path(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        path = path.replace("\\", "/")
        if any(rx.match(path) for rx in self._ignore_rx):
            return None
        hit = self._by_path(path)
        if hit:
            return hit[0]
        if path in self._auto_keys:
            return self._auto_keys[path][0]
        return None

    def _assign_variables(self) -> None:
        g = self.g
        # 파일 -> 그 파일에 정의된 함수들의 기능(가장 많은 것): 같은 파일의 전역 변수는 함수와 같은 기능으로
        per_file: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for f, a in self.func.items():
            fp = self._file(f)
            if fp:
                per_file[fp][a["feature"]] += 1
        file_feat = {fp: max(sorted(c), key=lambda k: c[k]) for fp, c in per_file.items()}
        for l in g.nodes_of(NK.LOCATION):
            d = g.node(l)
            feat = None
            root = d.get("root") or ""
            gd = g.node(root) if g.has_node(root) else {}
            path = gd.get("file") or d.get("file")
            # 함수 안 static 변수(gv:<파일>:<함수>/<이름>)는 그 함수의 기능
            if root.startswith("gv:") and root.count(":") >= 2 and "/" in root.rsplit(":", 1)[1]:
                file_part, tail = root[3:].rsplit(":", 1)
                fn_name = tail.split("/")[0]
                for owner_fn in (f"fn:{file_part}:{fn_name}", f"fn:{fn_name}"):
                    if owner_fn in self.func:
                        feat = self.func[owner_fn]["feature"]
                        break
            if feat is None and path:
                p = path.replace("\\", "/")
                hit = self._by_path(p)
                feat = hit[0] if hit and hit[1] == "map_path" else file_feat.get(p) or self.feature_of_path(p)
            if feat is None:
                cnt: Dict[str, int] = defaultdict(int)
                for w in g.pred(l, [EK.GLOBAL_WRITE]):
                    fw = self.func.get(g.func_of(w) or "")
                    if fw:
                        cnt[fw["feature"]] += 1
                if cnt:
                    feat = max(sorted(cnt), key=lambda k: cnt[k])
            if feat:
                self.var[l] = feat

    def of(self, a: Optional[str]) -> Optional[str]:
        """표시 단위 노드(함수·변수) -> 기능."""
        if not a:
            return None
        if a in self.func:
            return self.func[a]["feature"]
        return self.var.get(a)


def _fn_glob(pat: str) -> str:
    return "^" + re.escape(pat).replace("\\*", ".*").replace("\\?", ".") + "$"


# ---------------------------------------------------------------------------
# 기능 간 의존
# ---------------------------------------------------------------------------
def _name(g: AnalysisGraph, a: str) -> str:
    if not g.has_node(a):
        return a.split(":")[-1]
    d = g.node(a)
    if d.get("kind") == NK.LOCATION:
        return loc_display(d.get("loc") or a[4:]) if not d.get("mmio") else (d.get("name") or a)
    return d.get("name") or a.split(":")[-1]


def _is_output_signal(g: AnalysisGraph, sig: str) -> bool:
    d = g.node(sig) if g.has_node(sig) else {}
    return d.get("direction") == "tx" or bool(d.get("consumers"))


def dependencies(P: ProgramGraph, M: FeatureMapping) -> dict:
    """기능 쌍 (A,B) -> 호출·공유 변수·신호 근거, 기능별 외부 출력."""
    g = P.g
    pairs: Dict[Tuple[str, str], dict] = defaultdict(lambda: {"calls": set(), "vars": set(), "sigs": set()})
    outputs: Dict[str, Dict[str, dict]] = defaultdict(dict)
    w_loc: Dict[str, Set[str]] = defaultdict(set)
    r_loc: Dict[str, Set[str]] = defaultdict(set)
    w_sig: Dict[str, Set[str]] = defaultdict(set)
    r_sig: Dict[str, Set[str]] = defaultdict(set)
    observed: List[Tuple[str, str]] = []
    for (s, t, k), _d in P.edges.items():
        if k == CALL:
            A = M.of(s)
            if not A:
                continue
            if g.has_node(t) and g.node(t).get("external"):
                outputs[A][t] = {"kind": "외부 함수 호출", "name": _name(g, t)}
                continue
            B = M.of(t)
            if B and A != B:
                pairs[(A, B)]["calls"].add(f"{_name(g, s)}→{_name(g, t)}")
        elif k == SHARED:
            if g.has_node(s) and g.kind(s) == NK.FUNCTION:
                w_loc[t].add(s)
            elif g.has_node(t) and g.kind(t) == NK.FUNCTION:
                r_loc[s].add(t)
        elif k == SIGNAL:
            if g.has_node(s) and g.kind(s) == NK.SIGNAL:
                r_sig[s].add(t)
            else:
                w_sig[t].add(s)
        elif k == OBSERVE:
            observed.append((s, t))
    for loc, ws in w_loc.items():
        mmio = g.has_node(loc) and (g.node(loc).get("mmio") or (g.node(loc).get("root") or "").startswith("gv:mmio:"))
        for w in ws:
            A = M.of(w)
            if not A:
                continue
            if mmio:
                outputs[A][loc] = {"kind": "레지스터", "name": _name(g, loc)}
            for r in r_loc.get(loc, ()):
                B = M.of(r)
                if B and A != B:
                    pairs[(A, B)]["vars"].add(_name(g, loc))
    for sig, ws in w_sig.items():
        for w in ws:
            A = M.of(w)
            if not A:
                continue
            if _is_output_signal(g, sig) or not r_sig.get(sig):
                d = g.node(sig) if g.has_node(sig) else {}
                outputs[A][sig] = {"kind": "CAN 신호" if d.get("can_id") is not None else "RTE 신호", "name": _name(g, sig)}
            for r in r_sig.get(sig, ()):
                B = M.of(r)
                if B and A != B:
                    pairs[(A, B)]["sigs"].add(_name(g, sig))
    # 차량 기능 관찰 대상(함수 반환값·전역·신호에 붙은 출력)
    for anchor, obs in observed:
        d = g.node(obs) if g.has_node(obs) else {}
        label = {"kind": "관찰 대상", "name": f"{d.get('name')} ({d.get('feature')})"}
        if g.has_node(anchor) and g.kind(anchor) == NK.FUNCTION and M.of(anchor):
            outputs[M.of(anchor)][obs] = label
        for w in (w_loc.get(anchor, set()) | w_sig.get(anchor, set())):
            if M.of(w):
                outputs[M.of(w)][obs] = label
    return {"pairs": {k: {kk: sorted(v) for kk, v in d.items()} for k, d in pairs.items()},
            "outputs": {k: dict(v) for k, v in outputs.items()}}


# ---------------------------------------------------------------------------
# 변경 영향 전파
# ---------------------------------------------------------------------------
def _propagate(ch: dict, ia, go: AnalysisGraph, gn: AnalysisGraph, Ao: Abstraction, An: Abstraction,
               Mo: FeatureMapping, Mn: FeatureMapping, behavior_by_change: Dict[str, List[dict]]) -> dict:
    cid = ch["id"]
    sl = ia.slices.get(cid) or {}
    chops = ia.chops.get(cid) or {}
    nodes: Dict[str, dict] = {}
    edges: Dict[Tuple[str, str, str], Set[str]] = defaultdict(set)   # (s, t, 종류) -> 라벨(반환값·인자 ...)
    seeds_abs: Set[str] = set()
    for side, g, A in (("old", go, Ao), ("new", gn, An)):
        seeds = (sl.get("seeds") or {}).get(side) or []
        side_chops = {f: c for (sd, f), c in chops.items() if sd == side}
        if not seeds and not side_chops:
            continue
        seeds_abs |= {A.of(s) for s in seeds if A.of(s)}
        ns, es = _impact_side(g, A, sl.get(side), side_chops, seeds, cid)
        for a, d in ns.items():
            e = nodes.setdefault(a, {"on_path": False})
            e["on_path"] |= d["on_path"]
        for key, d in es.items():
            edges[key] |= d["labels"]
    centers = change_centers(ch, go, gn, seeds_abs)

    def feat(a: str) -> Optional[str]:
        return Mn.of(a) or Mo.of(a)

    def gof(a: str) -> AnalysisGraph:
        return gn if gn.has_node(a) else go

    def kind(a: str) -> str:
        g = gof(a)
        if a.startswith("chg:"):
            return "change"
        if not g.has_node(a):
            return "?"
        k = g.kind(a)
        if k == NK.LOCATION:
            d = g.node(a)
            return "register" if d.get("mmio") or (d.get("root") or "").startswith("gv:mmio:") else "variable"
        return {NK.FUNCTION: "function", NK.SIGNAL: "signal", NK.OBSERVATION: "observation"}.get(k, k)

    changed = sorted({feat(c) for c in centers if feat(c)})
    # 기능 경계를 넘는 연결(근거 포함)
    hop: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    w_conn: Dict[str, Set[str]] = defaultdict(set)
    r_conn: Dict[str, Set[str]] = defaultdict(set)
    outputs: Dict[str, dict] = {}
    for (s, t, k), labs in sorted(edges.items()):
        ks, kt = kind(s), kind(t)
        if ks == "change":
            continue
        if ks == "function" and kt == "function":
            A, B = feat(s), feat(t)
            if A and B and A != B:
                how = "반환값" if "반환값" in labs else ("인자 전달" if any(x.startswith("인자") for x in labs) else "호출")
                hop[(A, B)].append({"kind": how, "label": f"{_name(gof(s), s)} → {_name(gof(t), t)}"})
        elif ks == "function" and kt in ("variable", "signal", "register"):
            w_conn[t].add(s)
            if kt == "register":
                outputs[t] = {"kind": "레지스터", "name": _name(gof(t), t), "by": set()}
        elif ks in ("variable", "signal") and kt == "function":
            r_conn[s].add(t)
        elif kt == "observation":
            d = gof(t).node(t)
            outputs[t] = {"kind": "관찰 대상", "name": f"{d.get('name')} ({d.get('feature')})", "by": set(), "anchor": s}
    for c, ws in w_conn.items():
        kc = kind(c)
        label = _name(gof(c), c)
        for w in ws:
            A = feat(w)
            if not A:
                continue
            if c in outputs:
                outputs[c]["by"].add(A)
            if kc == "signal" and (_is_output_signal(gof(c), c) or not r_conn.get(c)):
                d = gof(c).node(c)
                o = outputs.setdefault(c, {"kind": "CAN 신호" if d.get("can_id") is not None else "RTE 신호",
                                           "name": _name(gof(c), c), "by": set()})
                o["by"].add(A)
            for r in r_conn.get(c, ()):
                B = feat(r)
                if B and A != B:
                    hop[(A, B)].append({"kind": "변수" if kc == "variable" else "신호",
                                        "label": f"{label} ({_name(gof(w), w)} → {_name(gof(r), r)})"})
    for o in outputs.values():
        if not o["by"] and o.get("anchor"):
            a = o["anchor"]
            if kind(a) == "function" and feat(a):
                o["by"].add(feat(a))
            for w in w_conn.get(a, ()):
                if feat(w):
                    o["by"].add(feat(w))
    # 실행 문맥(태스크·ISR·Runnable) 설정 변경: 그 문맥에서 실행되는 함수의 기능은 타이밍 영향(직접)
    for u in {ch.get("unit_old"), ch.get("unit_new")} - {None}:
        ctx = [g.node(u) for g in (go, gn) if g.has_node(u) and g.kind(u) == NK.TASK]
        if not ctx:
            continue
        diffs = []
        for key, label in (("period_ms", "주기"), ("priority", "우선순위"), ("core", "코어"), ("preemptive", "선점")):
            vals = [c.get(key) for c in ctx]
            if len(vals) == 2 and vals[0] != vals[1]:
                diffs.append(f"{label} {vals[0]}→{vals[1]}" + ("ms" if key == "period_ms" else ""))
        why = f"실행 문맥 {ctx[-1].get('name')}({ctx[-1].get('ctx_kind')}) " + (", ".join(diffs) if diffs else "설정 변경")
        for g in (go, gn):
            if not g.has_node(u) or g.kind(u) != NK.TASK:
                continue
            for f in g.pred(u, [EK.RUNS_IN]):
                B = feat(f)
                for A in changed:
                    if B and B != A and len(hop[(A, B)]) < 8:
                        if not any(x["label"].startswith(_name(g, f) + " ") for x in hop[(A, B)]):
                            hop[(A, B)].append({"kind": "실행 문맥", "label": f"{_name(g, f)} ({why})"})
                if B:
                    funcs_timing = nodes.setdefault(f, {"on_path": False})
                    funcs_timing.setdefault("timing", True)
    # 단계: 변경 기능에서 기능 경계를 넘은 횟수
    stage: Dict[str, int] = {c: 0 for c in changed}
    parent: Dict[str, Tuple[str, List[dict]]] = {}
    adj: Dict[str, List[str]] = defaultdict(list)
    for (A, B) in sorted(hop):
        adj[A].append(B)
    q = deque(changed)
    while q:
        A = q.popleft()
        for B in adj[A]:
            if B not in stage:
                stage[B] = stage[A] + 1
                parent[B] = (A, hop[(A, B)])
                q.append(B)
    # 슬라이스 안에 있지만 경계 연결로 닿지 않은 기능(예: 같은 함수가 두 기능에 걸친 경우)도 영향으로 기록
    funcs_by_feat: Dict[str, Set[str]] = defaultdict(set)
    onpath_feat: Set[str] = set()
    for a, d in nodes.items():
        if kind(a) == "function" and feat(a):
            funcs_by_feat[feat(a)].add(a)
            if d["on_path"]:
                onpath_feat.add(feat(a))
    for F in funcs_by_feat:
        if F not in stage:
            stage[F] = 1 + min((stage[x] for x in changed), default=0)
            parent[F] = (changed[0] if changed else "?", [{"kind": "슬라이스", "label": "전방 슬라이스 안(경계 연결 근거 없음)"}])

    def chain(F: str) -> List[dict]:
        out, x, guard = [], F, 0
        while x in parent and guard < 50:
            guard += 1
            A, ev = parent[x]
            out.append({"from": A, "to": x, "evidence": ev[:4], "more": max(0, len(ev) - 4)})
            x = A
        return list(reversed(out))

    behav = behavior_by_change.get(cid) or []
    feats_out = []
    for F in sorted(stage, key=lambda x: (stage[x], x)):
        incoming = []
        for (A, B), ev in hop.items():
            if B == F and A in stage and stage[A] < stage[F]:
                incoming.append({"from": A, "evidence": ev[:6], "more": max(0, len(ev) - 6)})
        outs = [{"id": o_id, "kind": o["kind"], "name": o["name"]} for o_id, o in sorted(outputs.items()) if F in o["by"]]
        fn_names = sorted({_name(gof(f), f) for f in funcs_by_feat.get(F, ())})
        changed_fns = sorted({_name(gof(c), c) for c in centers if feat(c) == F})
        feats_out.append({
            "feature": F, "stage": stage[F], "tier": tier_of(stage[F]), "chain": chain(F), "incoming": incoming,
            "functions": fn_names, "changed_functions": changed_fns, "outputs": outs, "on_path": F in onpath_feat,
            "behavior": [b for b in behav if any(o["kind"] == "관찰 대상" and o["name"].split(" (")[0] == b["observation"]
                                                 for o in outs)],
        })
    out_list = [{"id": o_id, "kind": o["kind"], "name": o["name"], "features": sorted(o["by"]),
                 "stage": min((stage.get(x, 99) for x in o["by"]), default=None)}
                for o_id, o in sorted(outputs.items())]
    return {"change": cid, "changed_features": changed, "features": feats_out, "outputs": out_list,
            "hops": [{"from": A, "to": B, "evidence": ev[:8], "count": len(ev)} for (A, B), ev in sorted(hop.items())],
            "max_stage": max(stage.values(), default=0)}


# ---------------------------------------------------------------------------
# 전체
# ---------------------------------------------------------------------------
def build_feature_impact(go: AnalysisGraph, gn: AnalysisGraph, results: List[dict], ia, behavior: List[dict],
                         spec: dict) -> dict:
    Mo, Mn = FeatureMapping(go, spec), FeatureMapping(gn, spec)
    Ao, An = Abstraction(go), Abstraction(gn)
    Po, Pn = ProgramGraph(go, Ao), ProgramGraph(gn, An)
    Do, Dn = dependencies(Po, Mo), dependencies(Pn, Mn)

    # 동작 차이 결과(변경별 문장) — 관찰 대상 출력이 있는 기능의 시험 조건으로 쓴다
    behavior_by_change: Dict[str, List[dict]] = defaultdict(list)
    for r in behavior or []:
        for cid, pc in (r.get("per_change") or {}).items():
            if pc.get("status") in _DIFF_STATUS:
                cond = ((pc.get("isolated") or {}).get("condition") or pc.get("divergence_condition") or {})
                behavior_by_change[cid].append({"observation": r["observation"], "status": pc.get("status"),
                                                "sentence": (pc.get("sentences") or [None])[0], "condition": cond.get("text")})
    changes = [_propagate(ch, ia, go, gn, Ao, An, Mo, Mn, behavior_by_change) for ch in results]

    # 기능 목록
    feats: Dict[str, dict] = {}
    for side, M in (("old", Mo), ("new", Mn)):
        for f, a in M.func.items():
            e = feats.setdefault(a["feature"], {"id": a["feature"], "name": M.names.get(a["feature"], a["feature"]),
                                                "functions_old": set(), "functions_new": set(), "files": set(),
                                                "sources": defaultdict(int)})
            e[f"functions_{side}"].add(_name(M.g, f))
            if M.g.node(f).get("file"):
                e["files"].add(M.g.node(f)["file"])
            if side == "new" or f not in Mn.func:
                e["sources"][a["source"]] += 1
    tests = {f["id"]: f["tests"] for f in spec["features"]}
    for F in list(feats):
        e = feats[F]
        e["tests"] = tests.get(F, [])
        e["functions_old"], e["functions_new"] = sorted(e["functions_old"]), sorted(e["functions_new"])
        e["files"] = sorted(e["files"])
        e["sources"] = dict(e["sources"])
    for f in spec["features"]:
        if f["id"] not in feats:
            feats[f["id"]] = {"id": f["id"], "name": f["name"], "functions_old": [], "functions_new": [], "files": [],
                              "sources": {}, "tests": f["tests"], "unused": True}

    # 의존 행렬(전후)
    ids = sorted(feats)
    cells = []
    for A in ids:
        for B in ids:
            o, n = Do["pairs"].get((A, B)), Dn["pairs"].get((A, B))
            if not o and not n:
                continue
            o = o or {"calls": [], "vars": [], "sigs": []}
            n = n or {"calls": [], "vars": [], "sigs": []}
            st = "added" if not any(o.values()) else ("removed" if not any(n.values()) else
                                                      ("changed" if o != n else "same"))
            cells.append({"from": A, "to": B, "calls": [len(o["calls"]), len(n["calls"])],
                          "vars": [len(o["vars"]), len(n["vars"])], "sigs": [len(o["sigs"]), len(n["sigs"])],
                          "status": st, "evidence": {"calls": n["calls"][:20] or o["calls"][:20],
                                                     "vars": n["vars"][:20] or o["vars"][:20],
                                                     "sigs": n["sigs"][:20] or o["sigs"][:20]},
                          "added": {k: sorted(set(n[k]) - set(o[k]))[:10] for k in ("calls", "vars", "sigs")},
                          "removed": {k: sorted(set(o[k]) - set(n[k]))[:10] for k in ("calls", "vars", "sigs")}})
    outputs = {F: sorted({v["kind"] + " " + v["name"] for v in d.values()}) for F, d in Dn["outputs"].items()}

    # 전체 요약·회귀 시험 범위(변경 합집합)
    overall: Dict[str, dict] = {}
    for c in changes:
        for fe in c["features"]:
            e = overall.setdefault(fe["feature"], {"feature": fe["feature"], "stage": fe["stage"], "changes": [],
                                                   "reasons": [], "changed_functions": set(), "functions": set(),
                                                   "inputs": set(), "outputs": set(), "behavior": [], "on_path": False})
            e["stage"] = min(e["stage"], fe["stage"])
            e["changes"].append(c["change"])
            e["on_path"] |= fe["on_path"]
            e["changed_functions"] |= set(fe["changed_functions"])
            e["functions"] |= set(fe["functions"])
            for inc in fe["incoming"]:
                for ev in inc["evidence"]:
                    if ev["kind"] in ("변수", "신호"):
                        e["inputs"].add(f"{ev['kind']} {ev['label'].split(' (')[0]} ← {inc['from']}")
            e["outputs"] |= {o["kind"] + " " + o["name"] for o in fe["outputs"]}
            if fe["chain"]:
                e["reasons"].append(c["change"] + ": " + " → ".join(
                    [fe["chain"][0]["from"]] + [f"[{_ev_text(h['evidence'])}] {h['to']}" for h in fe["chain"]]))
            else:
                e["reasons"].append(c["change"] + ": 변경이 이 기능 안에서 일어남")
            for b in fe["behavior"]:
                e["behavior"].append(f"{c['change']} {b['observation']}: {b['sentence'] or b['condition'] or b['status']}")
    regression = []
    for F, e in sorted(overall.items(), key=lambda x: (x[1]["stage"], x[0])):
        interface = _interface_functions(F, Mn, gn) & e["functions"]
        fn_list = sorted(e["changed_functions"]) + sorted(interface - e["changed_functions"]) + \
            sorted(e["functions"] - interface - e["changed_functions"])
        regression.append({
            "feature": F, "name": feats.get(F, {}).get("name", F), "tier": tier_of(e["stage"]),
            "tier_label": TIER[tier_of(e["stage"])], "stage": e["stage"], "changes": sorted(set(e["changes"])),
            "reasons": e["reasons"][:6], "changed_functions": sorted(e["changed_functions"]),
            "functions": fn_list[:25], "functions_total": len(e["functions"]), "interface": sorted(interface)[:15],
            "inputs": sorted(e["inputs"])[:12], "outputs": sorted(e["outputs"])[:12],
            "tests": feats.get(F, {}).get("tests", []), "behavior": e["behavior"][:6], "on_path": e["on_path"],
        })
    affected = [r for r in regression]
    summary = {"affected": len(affected), "changed": sum(1 for r in affected if r["tier"] == 0),
               "direct": sum(1 for r in affected if r["tier"] == 1),
               "indirect": sum(1 for r in affected if r["tier"] == 2),
               "outputs": len({o["id"] for c in changes for o in c["outputs"]}), "features_total": len(feats)}
    mapping_sources: Dict[str, int] = defaultdict(int)
    for f, a in Mn.func.items():
        mapping_sources[a["source"]] += 1
    mapping = {}
    for side, M in (("old", Mo), ("new", Mn)):
        for f, a in M.func.items():
            if side == "old" and f in Mn.func:
                continue
            mapping[_name(M.g, f)] = {"feature": a["feature"], "source": a["source"], "evidence": a["evidence"],
                                      "file": M.g.node(f).get("file"), "removed": side == "old"}
    ambiguous_swc = sorted(_name(gn, f) for f in Mn.functions
                           if sum(1 for r in Mn.swc_reach.values() if f in r) > 1)
    moved = sorted({(_name(gn, f), Mo.func[f]["feature"], a["feature"]) for f, a in Mn.func.items()
                    if f in Mo.func and Mo.func[f]["feature"] != a["feature"]})
    return {
        "summary": summary, "features": [feats[k] for k in ids], "changes": changes, "regression": regression,
        "dependency": {"features": ids, "cells": cells}, "outputs": outputs,
        "mapping": {"functions": mapping, "sources": dict(mapping_sources), "ignored": sorted(_name(gn, f) for f in Mn.ignored),
                    "map_file": spec.get("source"), "shared_across_swcs": ambiguous_swc[:50],
                    "moved": [{"function": n, "old": o, "new": nw} for n, o, nw in moved],
                    "swcs": sorted(Mn.swc_reach)},
        "suggested_map": suggested_map_yaml(feats, Mn, spec),
    }


def _ev_text(evs: List[dict]) -> str:
    if not evs:
        return ""
    x = evs[0]
    lab = x["kind"] + " " + x["label"].split(" (")[0] if x["kind"] in ("변수", "신호") else x["label"]
    return lab + (f" 외 {len(evs) - 1}" if len(evs) > 1 else "")


def _interface_functions(F: str, M: FeatureMapping, g: AnalysisGraph) -> Set[str]:
    """기능 F의 경계 함수: 다른 기능에서 호출되거나 실행 문맥(태스크·ISR·Runnable)의 진입 함수."""
    out = set()
    entries = {e for t in g.nodes_of(NK.TASK) for e in g.succ(t, [EK.ACTIVATES])}
    for f, a in M.func.items():
        if a["feature"] != F:
            continue
        callers = [c for c in g.pred(f, [EK.CALLS]) if g.kind(c) == NK.FUNCTION]
        if f in entries or any(M.of(c) not in (None, F) for c in callers):
            out.add(_name(g, f))
    return out


def suggested_map_yaml(feats: Dict[str, dict], M: FeatureMapping, spec: dict) -> str:
    """지금 쓰인 매핑을 검증자가 고칠 수 있는 매핑 파일 형식으로(제안)."""
    lines = ["# 기능 매핑 제안 — 분석기가 자동으로 정한 매핑. 고쳐서 설정의 feature_map으로 지정하면 그대로 쓴다.",
             "# 우선순위: overrides > functions(이름) > paths(경로) > swcs > structure 규칙 > AUTOSAR 자동 > 폴더 구조 > 파일 이름",
             "features:"]
    by_feat_files: Dict[str, Set[str]] = defaultdict(set)
    by_feat_src: Dict[str, Set[str]] = defaultdict(set)
    for f, a in M.func.items():
        by_feat_src[a["feature"]].add(a["source"])
        fp = M.g.node(f).get("file")
        if fp:
            by_feat_files[a["feature"]].add(posixpath.dirname(fp) + "/**" if a["source"] == "structure" else fp)
    defined = {f["id"]: f for f in spec["features"]}
    for F in sorted(feats):
        e = feats[F]
        fns = e["functions_new"] or e["functions_old"]
        d = defined.get(F)
        lines.append(f"  - id: {_y(F)}")
        lines.append(f"    name: {_y(e['name'])}")
        srcs = ", ".join(SOURCE_LABEL.get(s, s) for s in sorted(by_feat_src.get(F, ())))
        if d:
            for key in ("functions", "paths", "swcs", "tests"):
                if d[key]:
                    lines.append(f"    {key}: [{', '.join(_y(x) for x in d[key])}]")
        else:
            paths = sorted(by_feat_files.get(F, ()))
            if F in M.swc_reach:
                lines.append(f"    swcs: [{_y(F)}]")
            if paths:
                lines.append(f"    paths: [{', '.join(_y(x) for x in paths[:10])}]")
            lines.append("    tests: []          # 이 기능의 회귀 시험 케이스(ID·이름)")
        lines.append(f"    # 근거: {srcs or '-'} · 함수 {len(fns)}개: {', '.join(fns[:12])}{' …' if len(fns) > 12 else ''}")
    lines += ["# structure: ['AP/Function/{feature}/**']   # 폴더 구조 규칙", "# ignore: ['**/Test/**']",
              "# overrides: {Some_Function: PWM}            # 함수 하나씩 바로잡기", ""]
    return "\n".join(lines)


def _y(s: str) -> str:
    s = str(s)
    return s if re.fullmatch(r"[A-Za-z0-9_./*-]+", s) and not s.startswith(("*", "-")) else "'" + s.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# 내보내기(CSV, Excel에서 바로 열리도록 UTF-8 BOM)
# ---------------------------------------------------------------------------
def write_csvs(fi: dict, out_dir: str) -> List[str]:
    os.makedirs(out_dir, exist_ok=True)
    written = []
    p = os.path.join(out_dir, "feature_regression_scope.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["기능", "이름", "구분", "단계", "관련 변경", "영향 이유", "변경 함수", "시험 대상 함수(경계 우선)",
                    "입력(자극)", "관찰할 출력", "지정 시험 케이스", "동작 차이 조건"])
        for r in fi["regression"]:
            w.writerow([r["feature"], r["name"], r["tier_label"], r["stage"], " ".join(r["changes"]), " | ".join(r["reasons"]),
                        ", ".join(r["changed_functions"]), ", ".join(r["functions"]), " | ".join(r["inputs"]),
                        " | ".join(r["outputs"]), " | ".join(r["tests"]), " | ".join(r["behavior"])])
    written.append(p)
    p = os.path.join(out_dir, "feature_dependency.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["보내는 기능", "받는 기능", "호출(전→후)", "공유 변수(전→후)", "신호(전→후)", "전후 비교",
                    "호출 근거", "변수 근거", "신호 근거"])
        for c in fi["dependency"]["cells"]:
            w.writerow([c["from"], c["to"], "%d→%d" % tuple(c["calls"]), "%d→%d" % tuple(c["vars"]),
                        "%d→%d" % tuple(c["sigs"]), c["status"], ", ".join(c["evidence"]["calls"]),
                        ", ".join(c["evidence"]["vars"]), ", ".join(c["evidence"]["sigs"])])
    written.append(p)
    p = os.path.join(out_dir, "feature_map_suggested.yaml")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(fi["suggested_map"])
    written.append(p)
    return written


# ---------------------------------------------------------------------------
# 그래프 탭용 데이터(영향 그래프 탭 '4. 기능 의존·영향')
# ---------------------------------------------------------------------------
def graph_payload(fi: dict) -> dict:
    nodes = [{"id": f["id"], "name": f["name"], "n_old": len(f["functions_old"]), "n_new": len(f["functions_new"])}
             for f in fi["features"]]
    edges = []
    for c in fi["dependency"]["cells"]:
        edges.append([c["from"], c["to"], c["calls"], c["vars"], c["sigs"], c["status"],
                      ", ".join((c["evidence"]["vars"] + c["evidence"]["sigs"])[:4])])
    per_change = {}
    for c in fi["changes"]:
        per_change[c["change"]] = {
            "stage": {fe["feature"]: fe["stage"] for fe in c["features"]},
            "hops": [[h["from"], h["to"], _ev_text(h["evidence"]), h["count"]] for h in c["hops"]],
            "outputs": [[o["id"], o["kind"], o["name"], o["features"]] for o in c["outputs"]],
        }
    return {"nodes": nodes, "edges": edges, "changes": per_change, "outputs": fi["outputs"]}


# ---------------------------------------------------------------------------
# 보고서 '기능 영향' 탭
# ---------------------------------------------------------------------------
def _e(x) -> str:
    return html.escape("" if x is None else str(x))


FEATURE_CSS = """
.fi-cards{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}.fi-cards .card{flex:1 1 180px;margin:0}
.fi-flow{display:flex;gap:8px;align-items:stretch;overflow-x:auto;padding:4px 0 8px}
.fi-col{min-width:200px;flex:1 1 0;border:1px solid var(--bd);border-radius:8px;padding:8px;background:var(--card)}
.fi-col h5{margin:0 0 6px;font-size:12.5px;color:var(--mut)}
.fi-arrow{align-self:center;color:var(--mut);font-size:18px}
.fi-feat{border-radius:6px;padding:6px 8px;margin:0 0 6px;border:2px solid var(--bd)}
.fi-t0{border-color:#c62828;background:rgba(198,40,40,.08)}.fi-t1{border-color:#e65100}.fi-t2{border-color:#f2a900}
.fi-out{border-color:#1565c0;border-style:dashed}
.fi-ev{font-size:12px;color:var(--mut);margin-top:3px}.fi-ev code{font-size:11.5px}
.fi-tier{display:inline-block;font-size:11.5px;font-weight:700;padding:0 7px;border-radius:999px;border:1px solid currentColor}
.fi-tier.t0{color:#c62828}.fi-tier.t1{color:#e65100}.fi-tier.t2{color:#a87900}
.fi-matrix{overflow:auto;max-height:70vh;border:1px solid var(--bd);border-radius:8px}
.fi-matrix table{width:auto;margin:0;font-size:12px}.fi-matrix th,.fi-matrix td{border:1px solid var(--bd);text-align:center;
 padding:3px 6px;white-space:nowrap}.fi-matrix th{position:sticky;top:0;background:var(--card);z-index:1}
.fi-matrix th.row{position:sticky;left:0;text-align:left;z-index:2}
.fi-matrix td.add{outline:2px solid #2e8b57;outline-offset:-2px}.fi-matrix td.rem{outline:2px solid #c62828;outline-offset:-2px}
.fi-matrix td.chg{outline:2px dashed #2e8b57;outline-offset:-2px}
.fi-hl{font-weight:700}.fi-small{font-size:12px}
"""


def feature_tab_html(fi: dict, files_rel: Optional[List[str]] = None) -> str:
    s = fi["summary"]
    b = [f"<style>{FEATURE_CSS}</style>"]
    b.append("<p class='mut'>함수 단위 영향(전방 슬라이스·근거 경로)을 SW 기능 묶음으로 모은 결과. 단계는 변경 기능에서 기능 경계를 "
             "넘은 횟수: <b>변경 기능</b>(단계 0) → <b>직접 영향</b>(1단계, 변경 기능과 바로 연결) → <b>간접 영향</b>(2단계 이상).</p>")
    b.append("<div class='fi-cards'>"
             f"<div class='card'><div class='mut'>영향받는 기능</div><div class='big'>{s['affected']}</div>"
             f"<div class='mut'>전체 기능 {s['features_total']}개 중</div></div>"
             f"<div class='card'><div class='mut'>변경 기능</div><div class='big' style='color:#c62828'>{s['changed']}</div></div>"
             f"<div class='card'><div class='mut'>직접 영향</div><div class='big' style='color:#e65100'>{s['direct']}</div></div>"
             f"<div class='card'><div class='mut'>간접 영향</div><div class='big' style='color:#a87900'>{s['indirect']}</div></div>"
             f"<div class='card'><div class='mut'>영향받는 외부 출력</div><div class='big'>{s['outputs']}</div>"
             f"<div class='mut'>CAN·RTE 신호, 레지스터, 관찰 대상</div></div></div>")
    srcs = fi["mapping"]["sources"]
    b.append("<div class='card fi-small'>함수 → 기능 매핑: " + " · ".join(
        f"{_e(SOURCE_LABEL.get(k, k))} {v}" for k, v in sorted(srcs.items(), key=lambda x: -x[1]))
        + (f" · 제외 {len(fi['mapping']['ignored'])}" if fi["mapping"]["ignored"] else "")
        + (f"<br>매핑 파일: <code>{_e(fi['mapping']['map_file'])}</code>" if fi["mapping"]["map_file"] else
           "<br>매핑 파일 없음(자동 매핑). 아래 '기능 매핑'과 <code>feature_map_suggested.yaml</code>을 고쳐 설정의 "
           "<code>feature_map</code>으로 지정하면 검증자가 정한 대로 묶인다.") + "</div>")

    # 1. 변경별 전파
    b.append("<h2>1. 변경 → 영향받는 기능 → 외부 출력</h2>")
    for c in fi["changes"]:
        cid = c["change"]
        b.append(f"<div class='card' id='fi-{_e(cid)}'><b><a href='#{_e(cid)}'>{_e(cid)}</a></b> "
                 f"<span class='mut'>변경 기능 {_e(', '.join(c['changed_features']) or '-')}</span>"
                 f"<a class='ig-link' href='#graph:{_e(cid)}:feature'>기능 그래프에서 보기</a>")
        if not c["features"]:
            b.append("<div class='mut'>영향받는 기능 없음(전방 슬라이스가 비었거나 매핑된 함수 없음)</div></div>")
            continue
        cols: Dict[int, List[dict]] = defaultdict(list)
        for fe in c["features"]:
            cols[min(fe["stage"], 9)].append(fe)
        b.append("<div class='fi-flow'>")
        for i, st in enumerate(sorted(cols)):
            if i:
                b.append("<div class='fi-arrow'>→</div>")
            title = TIER[0] if st == 0 else (f"{st}단계 · " + TIER[tier_of(st)])
            b.append(f"<div class='fi-col'><h5>{_e(title)}</h5>")
            for fe in cols[st]:
                b.append(f"<div class='fi-feat fi-t{fe['tier']}'><b>{_e(fe['feature'])}</b>"
                         + (" <span class='mut fi-small'>(출력 경로 위)</span>" if fe["on_path"] else ""))
                if fe["changed_functions"]:
                    b.append(f"<div class='fi-ev'>변경: {', '.join('<code>' + _e(x) + '</code>' for x in fe['changed_functions'][:6])}</div>")
                for inc in fe["incoming"][:3]:
                    b.append(f"<div class='fi-ev'>← {_e(inc['from'])}: " + ", ".join(
                        f"{_e(ev['kind'])} <code>{_e(ev['label'])}</code>" for ev in inc["evidence"][:3])
                        + (f" 외 {inc['more'] + max(0, len(inc['evidence']) - 3)}" if inc["more"] or len(inc["evidence"]) > 3 else "") + "</div>")
                b.append(f"<div class='fi-ev'>영향 함수 {len(fe['functions'])}개: {_e(', '.join(fe['functions'][:6]))}"
                         f"{' …' if len(fe['functions']) > 6 else ''}</div></div>")
            b.append("</div>")
        if c["outputs"]:
            b.append("<div class='fi-arrow'>→</div><div class='fi-col'><h5>외부 출력</h5>")
            for o in c["outputs"]:
                b.append(f"<div class='fi-feat fi-out'><b>{_e(o['name'])}</b><div class='fi-ev'>{_e(o['kind'])} · "
                         f"보내는 기능 {_e(', '.join(o['features']) or '-')}</div></div>")
            b.append("</div>")
        b.append("</div></div>")

    # 2. 회귀 시험 범위
    csv_links = ", ".join("<a href='" + _e(x) + "' download>" + _e(x) + "</a>" for x in files_rel or [] if x.endswith(".csv"))
    b.append("<h2>2. 회귀 시험 범위 제안</h2><p class='mut'>변경 합집합 기준. 시험 대상 함수는 변경 함수 → 경계 함수(다른 기능에서 "
             "호출되거나 태스크·Runnable 진입 함수, 굵게) → 나머지 순. 입력은 이 기능으로 들어오는 영향 경로의 변수·신호, 출력은 관찰할 "
             "외부 출력." + (" CSV(Excel): " + csv_links if csv_links else "") + "</p>")
    b.append("<table><tr><th>기능</th><th>구분</th><th>관련 변경</th><th>영향 이유(전파 경로)</th><th>시험 대상</th></tr>")
    for r in fi["regression"]:
        tgt = []
        if r["tests"]:
            tgt.append("<b>지정 시험</b>: " + _e(", ".join(r["tests"])))
        if r["changed_functions"]:
            tgt.append("<b>변경 함수</b>: " + ", ".join("<code>" + _e(x) + "</code>" for x in r["changed_functions"][:8]))
        rest = [x for x in r["functions"] if x not in r["changed_functions"]]
        if rest:
            tgt.append(f"<b>함수</b>({r['functions_total']}개 중): " + ", ".join(
                ("<code class='fi-hl'>" if x in r["interface"] else "<code>") + _e(x) + "</code>" for x in rest[:10])
                + (" …" if len(rest) > 10 else ""))
        if r["inputs"]:
            tgt.append("<b>입력</b>: " + _e(" · ".join(r["inputs"][:6])))
        if r["outputs"]:
            tgt.append("<b>관찰할 출력</b>: " + _e(" · ".join(r["outputs"][:6])))
        if r["behavior"]:
            tgt.append("<b>동작 차이 조건</b>: " + _e(" / ".join(r["behavior"][:3])))
        b.append(f"<tr><td><b>{_e(r['feature'])}</b>" + (f"<br><span class='mut'>{_e(r['name'])}</span>" if r["name"] != r["feature"] else "")
                 + f"</td><td><span class='fi-tier t{r['tier']}'>{_e(r['tier_label'])}</span><br><span class='mut'>단계 {r['stage']}</span></td>"
                 + "<td>" + " ".join("<a href='#fi-" + _e(x) + "'>" + _e(x) + "</a>" for x in r["changes"]) + "</td>"
                 f"<td class='fi-small'>{'<br>'.join(_e(x) for x in r['reasons'][:4])}</td><td class='fi-small'>{'<br>'.join(tgt)}</td></tr>")
    b.append("</table>")

    # 3. 의존 행렬
    ids = fi["dependency"]["features"]
    affected = {r["feature"]: r["tier"] for r in fi["regression"]}
    cells = {(c["from"], c["to"]): c for c in fi["dependency"]["cells"]}
    b.append("<h2>3. 기능 간 의존 행렬</h2><p class='mut'>행 = 보내는 기능, 열 = 받는 기능. 칸 = 호출 수 / 공유 변수 수 / 신호 수(변경 후). "
             "초록 테두리 = 새로 생긴 의존, 빨강 = 사라진 의존, 점선 = 수가 바뀜. 칸에 마우스를 올리면 근거 이름. "
             "<a href='#graph:*:feature'>그래프로 보기</a></p>")
    if ids:
        mx = max([c["calls"][1] + c["vars"][1] + c["sigs"][1] for c in fi["dependency"]["cells"]] or [1]) or 1
        b.append("<div class='fi-matrix'><table><tr><th class='row'>보냄 \\ 받음</th>" + "".join(
            f"<th class='{'fi-hl' if x in affected else ''}'>{_e(x)}</th>" for x in ids) + "</tr>")
        for A in ids:
            row = [f"<th class='row {'fi-hl' if A in affected else ''}'>{_e(A)}"
                   + (f" <span class='fi-tier t{affected[A]}'>{_e(TIER[affected[A]])}</span>" if A in affected else "") + "</th>"]
            for B in ids:
                c = cells.get((A, B))
                if not c:
                    row.append("<td></td>")
                    continue
                n = c["calls"][1] + c["vars"][1] + c["sigs"][1]
                alpha = 0.08 + 0.5 * (n / mx)
                cls = {"added": "add", "removed": "rem", "changed": "chg"}.get(c["status"], "")
                tip = "; ".join(f"{k}: {', '.join(v)}" for k, v in (("호출", c["evidence"]["calls"]), ("변수", c["evidence"]["vars"]),
                                                                     ("신호", c["evidence"]["sigs"])) if v)
                if c["status"] != "same":
                    diff = [f"+{k} {', '.join(v)}" for k, v in c["added"].items() if v] + \
                           [f"-{k} {', '.join(v)}" for k, v in c["removed"].items() if v]
                    tip += " | 변경: " + "; ".join(diff)
                val = f"{c['calls'][1]}/{c['vars'][1]}/{c['sigs'][1]}" if c["status"] != "removed" else \
                    f"<s>{c['calls'][0]}/{c['vars'][0]}/{c['sigs'][0]}</s>"
                row.append(f"<td class='{cls}' style='background:rgba(47,93,138,{alpha:.2f})' title='{_e(tip)}'>{val}</td>")
            b.append("<tr>" + "".join(row) + "</tr>")
        b.append("</table></div>")

    # 4. 매핑
    b.append("<h2>4. 기능 매핑(함수 → 기능)</h2><p class='mut'>근거: 매핑 파일(개별 지정·함수 이름·경로·SWC) > 폴더 구조 규칙 > "
             "AUTOSAR(SWC 전용 함수) > 자동 폴더 구조 > 파일 이름. 고칠 때는 <code>feature_map_suggested.yaml</code>을 복사해 "
             "설정의 <code>feature_map</code>으로 지정한다.</p>")
    b.append("<table><tr><th>기능</th><th>함수 수(전→후)</th><th>근거</th><th>파일</th><th>외부 출력</th><th>지정 시험</th></tr>")
    for f in fi["features"]:
        b.append(f"<tr><td><b>{_e(f['id'])}</b>" + (f"<br><span class='mut'>{_e(f['name'])}</span>" if f["name"] != f["id"] else "")
                 + f"</td><td>{len(f['functions_old'])} → {len(f['functions_new'])}"
                 + ("<br><span class='mut'>매핑 파일에 있으나 함수 없음</span>" if f.get("unused") else "") + "</td>"
                 f"<td class='fi-small'>{_e(', '.join(SOURCE_LABEL.get(k, k) + ' ' + str(v) for k, v in f['sources'].items()))}</td>"
                 f"<td class='fi-small'>{_e(', '.join(f['files'][:6]))}{' …' if len(f['files']) > 6 else ''}</td>"
                 f"<td class='fi-small'>{_e(', '.join(fi['outputs'].get(f['id'], [])[:6]))}</td>"
                 f"<td class='fi-small'>{_e(', '.join(f['tests']))}</td></tr>")
    b.append("</table>")
    if fi["mapping"].get("moved"):
        b.append("<div class='card fi-small'><b>변경 전·후에 기능이 바뀐 함수</b>(호출 관계가 바뀌어 AUTOSAR·구조 규칙 결과가 달라짐. "
                 "의도와 다르면 매핑 파일 overrides로 고정): " + ", ".join(
                     f"<code>{_e(m['function'])}</code> {_e(m['old'])} → {_e(m['new'])}" for m in fi["mapping"]["moved"][:20]) + "</div>")
    if fi["mapping"]["shared_across_swcs"]:
        b.append("<div class='mut fi-small'>여러 SWC에서 함께 쓰는 함수(SWC로 묶지 않고 폴더 구조로 매핑): "
                 + _e(", ".join(fi["mapping"]["shared_across_swcs"][:30])) + "</div>")
    b.append("<details><summary>함수별 매핑 근거 전체</summary><table><tr><th>함수</th><th>기능</th><th>근거</th><th>파일</th></tr>"
             + "".join(f"<tr><td><code>{_e(n)}</code>{' (삭제됨)' if a['removed'] else ''}</td><td>{_e(a['feature'])}</td>"
                       f"<td class='fi-small'>{_e(SOURCE_LABEL.get(a['source'], a['source']))}: {_e(a['evidence'])}</td>"
                       f"<td class='fi-small'>{_e(a['file'])}</td></tr>"
                       for n, a in sorted(fi["mapping"]["functions"].items()))
             + "</table></details>")
    return "".join(b)
