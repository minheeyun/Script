"""기능 나누는 기준: ARXML 자동 탐색·분석, 폴더 구조(모듈 폴더), 기준 자동 판단(auto/arxml/composition/folder/file)."""
import json
import os
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from vfia import portable as pt  # noqa: E402
from vfia.arxml import find_arxml, load_arxml, parse_arxml_file  # noqa: E402
from vfia.feature_impact import FeatureMapping, load_feature_map, module_keys  # noqa: E402

EX = os.path.join(ROOT, "examples", "autosar_ecu")
CFG = os.path.join(EX, "analysis.yaml")


def _groups(m):
    out = {}
    for f, a in m.func.items():
        out.setdefault(a["feature"], set()).add(m.g.node(f)["name"])
    return out


@pytest.fixture(scope="module")
def after():
    from vfia.arxml import arxml_for_version
    from vfia.config import load_config
    from vfia.pipeline import get_graph
    cfg = load_config(CFG)
    return get_graph(cfg, "after", log=lambda *a: None), arxml_for_version(cfg, "after")


def test_arxml_parse_and_find(tmp_path):
    files = find_arxml([os.path.join(EX, "v2")])
    assert [os.path.basename(x) for x in files] == ["LssEcu.arxml"]
    r = load_arxml(files)
    assert r["swcs"]["SWC_Speed"]["kind"] == "sensor-actuator"
    assert [x["symbol"] for x in r["swcs"]["SWC_Pwm"]["runnables"]] == ["Pwm_MainFunction"]
    assert r["swcs"]["SWC_Pwm"]["runnables"][0]["period_ms"] == 10.0
    assert r["swcs"]["SWC_Pwm"]["ports"] == {"p": ["PpPwmDuty"], "r": ["RpDiagFault", "RpVehicleSpeed"]}
    assert r["top_composition"]["SWC_Speed"] == "Comp_Chassis" and r["impl_files"]["SWC_Diag"] == ["diag.c"]
    bad = tmp_path / "bad.arxml"
    bad.write_text("<AUTOSAR><AR-PACKAGES>", encoding="utf-8")
    assert parse_arxml_file(str(bad))["errors"]                       # 깨진 파일은 오류로 기록하고 계속
    # 글롭 지정·빌드 산출물 폴더 제외
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x.arxml").write_text("<AUTOSAR/>", encoding="utf-8")
    assert find_arxml([str(tmp_path)]) == [str(bad)]
    assert find_arxml([], [str(tmp_path / "**" / "*.arxml")]) == sorted([str(bad), str(tmp_path / "out" / "x.arxml")])


def test_module_keys():
    k = module_keys(["AP/Function/PWM/src/sub/a.c", "AP/Function/PWM/src/b.c", "BSW/Mcal/Pwm/src/Pwm.c",
                     "Common/Util/util.c", "AP/Function/DIAG/diag.c"])
    assert k["AP/Function/PWM/src/sub/a.c"] == ("Function/PWM", "structure", "AP/Function/PWM")   # src 아래 하위 폴더도 PWM
    assert k["BSW/Mcal/Pwm/src/Pwm.c"][0] == "Mcal/Pwm" and k["Common/Util/util.c"][0] == "Util"
    assert k["AP/Function/DIAG/diag.c"] == ("DIAG", "structure", "AP/Function/DIAG")
    flat = module_keys(["src/a.c", "src/b.c"])                       # 한 폴더에 모여 있으면 파일 이름
    assert flat["src/a.c"] == ("a", "file", "a.c")
    assert module_keys(["src/app/x.c", "src/drv/y.c"])["src/app/x.c"][0] == "app"


def test_basis_decision(after):
    g, ax = after
    spec = load_feature_map(None)
    auto = FeatureMapping(g, spec, ax, "auto")
    assert auto.basis == "arxml" and auto.decision["arxml"]["state"] == "사용 가능"
    assert auto.decision["arxml"]["linked"] == 3 and auto.decision["arxml"]["runnables"] == 4
    assert auto.decision["arxml"]["unlinked"] == [{"swc": "SWC_Lamp", "runnable": "RE_LampMain", "symbol": "Lamp_MainFunction"}]
    gr = _groups(auto)
    assert gr["SWC_Pwm"] == {"Pwm_MainFunction", "Pwm_CalcDuty"} and gr["SWC_Speed"] == {"Spd_MainFunction", "Spd_Filter"}
    # 다른 모듈 폴더(BSW 드라이버)와 여러 SWC가 쓰는 공용 함수는 폴더 구조
    assert gr["Mcal/Pwm"] == {"Pwm_SetDutyCycle"} and gr["Util"] == {"Util_Clamp"} and gr["Os"] == {"Task_10ms"}
    comp = _groups(FeatureMapping(g, spec, ax, "composition"))
    assert comp["Comp_Chassis"] == {"Pwm_MainFunction", "Pwm_CalcDuty", "Spd_MainFunction", "Spd_Filter"}
    folder = _groups(FeatureMapping(g, spec, ax, "folder"))
    assert folder["SPEED"] == {"Spd_MainFunction", "Spd_Filter"} and folder["Function/PWM"] == {"Pwm_MainFunction", "Pwm_CalcDuty"}
    assert "util" in _groups(FeatureMapping(g, spec, ax, "file"))
    # ARXML이 없으면 폴더 구조, 심벌이 코드와 연결되지 않으면 '사용 불가'로 폴더 구조
    assert FeatureMapping(g, spec, None, "auto").basis == "folder"
    bad = {"swcs": {"X": {"kind": "application", "runnables": [{"name": "R1", "symbol": "Nope"}]}}, "files": [1]}
    m = FeatureMapping(g, spec, bad, "auto")
    assert m.basis == "folder" and m.decision["arxml"]["state"] == "사용 불가" and "Nope" in m.decision["recommendations"][0]
    # 매핑 파일이 먼저: 경로 규칙이 SWC보다 앞선다
    sp = {"features": [{"id": "PWM_APP", "name": "PWM", "functions": [], "paths": ["AP/Function/PWM/**"], "swcs": [], "tests": []}],
          "structure": [], "ignore": [], "overrides": {"Util_Clamp": "LIB"}, "source": None}
    gm = _groups(FeatureMapping(g, sp, ax, "auto"))
    assert gm["PWM_APP"] == {"Pwm_MainFunction", "Pwm_CalcDuty"} and gm["LIB"] == {"Util_Clamp"}


def test_quick_diagnose_and_config_checks(tmp_path):
    from vfia.config import load_config
    from vfia.config_check import check_config
    from vfia.feature_basis import quick_for_config
    d = quick_for_config(load_config(CFG), "after")
    assert d["arxml_state"] == "사용 가능" and (d["linked"], d["runnables"]) == (3, 4) and d["basis"] == "arxml"
    assert "AP/Function/PWM" in d["swc_home_folders"] and d["modules"] == 6
    from vfia.arxml import arxml_for_version
    tcfg = load_config(os.path.join(ROOT, "examples", "torque_ecu", "analysis.yaml"))
    t = quick_for_config(tcfg, "after")
    assert t["arxml_files"] == ["swc_torque.arxml"]                  # 차량 모델에 적힌 ARXML도 본다
    ax = arxml_for_version(tcfg, "after")                             # 분석도 같은 파일을 쓴다
    assert ax["search"] == "차량 모델 1개" and list(ax["swcs"]) == ["SWC_Torque"]
    cfg = tmp_path / "a.yaml"
    cfg.write_text(f"versions:\n  before: {{root: '{EX}/v1', compile_commands: '{EX}/v1/compile_commands.json'}}\n"
                   f"  after: {{root: '{EX}/v2', compile_commands: '{EX}/v2/compile_commands.json'}}\n"
                   "feature_basis: swc\narxml: ['nowhere/**/*.arxml']\n", encoding="utf-8")
    errors, warns = check_config(load_config(str(cfg)))
    assert any("feature_basis 값이 잘못됨" in e for e in errors) and any("맞는 ARXML 파일이 없음" in w for w in warns)


def _run(args):
    return subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=600, env=pt.subprocess_env_utf8())


def test_feature_basis_tool(tmp_path):
    tool = os.path.join(ROOT, "tools", "feature_basis.py")
    p = _run([tool, "--config", CFG, "--quick"])
    assert p.returncode == 0 and "→ 기능 나누는 기준: ARXML(SWC) + 폴더 구조" in p.stdout
    out = tmp_path / "fm.yaml"
    p = _run([tool, "--config", CFG, "--basis", "composition", "--write-map", str(out)])
    assert p.returncode == 0 and "Comp_Chassis" in p.stdout, p.stdout + p.stderr
    spec = load_feature_map(str(out))
    assert {f["id"] for f in spec["features"]} >= {"Comp_Chassis", "Mcal/Pwm"}
    assert next(f for f in spec["features"] if f["id"] == "Comp_Chassis")["swcs"] == ["SWC_Pwm", "SWC_Speed"]


def test_autosar_example_feature_impact():
    out = tempfile.mkdtemp(prefix="vfia_ax_")
    p = _run([os.path.join(ROOT, "analyze_impact.py"), "--config", CFG, "--out", out, "--no-behavior", "--quiet"])
    assert p.returncode == 0, p.stderr
    with open(os.path.join(out, "impact.json"), encoding="utf-8") as f:
        d = json.load(f)
    fi = d["feature_impact"]
    assert fi["basis"]["new"]["basis"] == "arxml"
    types = {c["id"]: c["type"] for c in d["changes"]}
    util = next(c for c in fi["changes"] if types[c["change"]] == "comparison_operator_change")
    st = {fe["feature"]: fe["stage"] for fe in util["features"]}
    assert st == {"Util": 0, "SWC_Pwm": 1, "SWC_Speed": 1, "Mcal/Pwm": 2, "SWC_Diag": 2}
    diag_in = [e["label"] for fe in util["features"] if fe["feature"] == "SWC_Diag" for i in fe["incoming"] for e in i["evidence"]]
    assert any(x.startswith("g_vehicle_speed") for x in diag_in)
    outs = {o["name"]: o["kind"] for o in util["outputs"]}
    assert outs == {"PWM0_DUTY_REG": "레지스터", "PpDiagFault_Value": "CAN 신호", "PpPwmDuty_Value": "CAN 신호"}
    with open(os.path.join(out, "impact.html"), encoding="utf-8") as f:
        html = f.read()
    assert "id='fi-basis'" in html and "기능 나누는 기준: ARXML(SWC) + 폴더 구조" in html and "RE_LampMain" in html
