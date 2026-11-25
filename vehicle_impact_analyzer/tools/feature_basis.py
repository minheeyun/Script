#!/usr/bin/env python3
"""기능 나누는 기준 진단: 폴더 구조·ARXML(SWC/Runnable)·매핑 파일 중 무엇을 쓸지, ARXML을 쓸 수 있는지 보여 준다.

  python tools/feature_basis.py --config my_project/analysis.yaml            # 분석 그래프 기준(정확, 처음엔 파싱 시간)
  python tools/feature_basis.py --config my_project/analysis.yaml --quick    # 파일만 보고 빠르게
  python tools/feature_basis.py --config my_project/analysis.yaml --basis folder --write-map my_project/feature_map.yaml

--basis로 다른 기준(auto | arxml | composition | folder | file)을 넣어 결과를 비교할 수 있다. 정한 기준은 설정의
feature_basis에 적는다. --write-map은 지금 매핑을 검증자가 고칠 매핑 파일 형식으로 쓴다.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vfia import portable as pt  # noqa: E402
from vfia.config_check import ConfigError, load_config_checked  # noqa: E402
from vfia.feature_basis import full_diagnose, quick_for_config, quick_text  # noqa: E402
from vfia.feature_impact import BASIS, SOURCE_LABEL  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="기능 나누는 기준 진단")
    ap.add_argument("--config", required=True)
    ap.add_argument("--version", default=None, help="진단할 버전(기본: 설정의 마지막 버전)")
    ap.add_argument("--basis", default=None, choices=list(BASIS), help="이 기준으로 나눴을 때를 보여 줌(기본: 설정 feature_basis)")
    ap.add_argument("--quick", action="store_true", help="코드 파싱 없이 파일만 보고 판단")
    ap.add_argument("--write-map", default=None, help="지금 매핑을 매핑 파일(YAML) 형식으로 저장")
    a = ap.parse_args(argv)
    pt.setup_console()
    try:
        cfg = load_config_checked(a.config)
    except ConfigError as e:
        print(f"설정 오류: {e}")
        return 2
    version = a.version or list(cfg.versions)[-1]
    basis = a.basis or cfg.feature_basis
    print(f"[{version}] 기능 나누는 기준 진단 — 요청 기준: {basis}" + (f", 매핑 파일 {cfg.feature_map}" if cfg.feature_map else ""))
    if a.quick:
        d = quick_for_config(cfg, version, basis)
        for line in quick_text(d):
            print("  " + line)
        return 0
    res = full_diagnose(cfg, version, basis, log=lambda *x: None)
    d = res["decision"]
    print(f"\n기준: {d['basis_label']}")
    for r in d["reasons"]:
        print(f"  - {r}")
    ax = d["arxml"]
    print(f"\nARXML: {ax['state']} — 파일 {ax['files']}개, SWC {ax['swcs']}개 {ax['swc_kinds']}, Runnable 연결 "
          f"{ax['linked']}/{ax['runnables']}({ax['ratio']:.0%}), 컴포지션 {ax['compositions']}개")
    for u in ax["unlinked"][:8]:
        print(f"  코드에 없음: {u['swc']}.{u['runnable']} (SYMBOL {u['symbol']})")
    if ax["module_swc"]:
        print("  SWC Runnable이 있는 모듈 폴더: " + ", ".join(f"{k} → {v}" for k, v in list(ax["module_swc"].items())[:8]))
    fo = d["folder"]
    print(f"폴더 구조: 모듈 폴더 {fo['modules']}개(예: {', '.join(fo['examples'][:6]) or '-'})")
    print(f"\n함수 {d['functions']}개의 근거: " + ", ".join(f"{SOURCE_LABEL.get(k, k)} {n}" for k, n in d["coverage"].items()))
    print("\n기능별 함수:")
    for F, e in sorted(res["features"].items(), key=lambda x: (-len(x[1]["functions"]), x[0])):
        fns = sorted(e["functions"])
        src = ", ".join(SOURCE_LABEL.get(k, k) for k in e["sources"])
        print(f"  {F:<28} {len(fns):>4}개  [{src}]  {', '.join(fns[:6])}{' …' if len(fns) > 6 else ''}")
    if d["recommendations"]:
        print("\n권고:")
        for r in d["recommendations"]:
            print(f"  - {r}")
    print(f"\n기준을 바꾸려면 설정에 feature_basis: auto | arxml | composition | folder | file"
          f"{'(현재 ' + cfg.feature_basis + ')' if cfg.feature_basis else ''}, ARXML 위치는 arxml: [...]")
    if a.write_map:
        from vfia.feature_impact import suggested_map_yaml
        m = res["mapping"]
        feats = {F: {"name": m.names.get(F, F), "functions_new": sorted(e["functions"]), "functions_old": []}
                 for F, e in res["features"].items()}
        if os.path.exists(a.write_map):
            print(f"이미 있음(덮어쓰지 않음): {a.write_map}")
            return 1
        with open(a.write_map, "w", encoding="utf-8", newline="\n") as f:
            f.write(suggested_map_yaml(feats, m, res["spec"]))
        print(f"매핑 파일 저장: {a.write_map} (고친 뒤 설정 feature_map으로 지정)")
    return 0


if __name__ == "__main__":
    pt.reexec_with_fixed_hash_seed()
    sys.exit(main())
