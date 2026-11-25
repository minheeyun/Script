"""기능 단위 영향: 함수 → 기능 매핑(매핑 파일·AUTOSAR·폴더 구조), 기능 의존, 변경 영향 전파, 회귀 시험 범위, 보고서 탭."""
import csv
import json
import os
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from helpers import graph_of  # noqa: E402
from vfia import portable as pt  # noqa: E402
from vfia.feature_impact import FeatureMapping, dependencies, glob_to_regex, load_feature_map  # noqa: E402
from vfia.impact_graph import Abstraction, ProgramGraph  # noqa: E402

CFG = os.path.join(ROOT, "examples", "torque_ecu", "analysis.yaml")

SRC = {
    "include/g.h": "extern int g_duty; extern int g_can_tx;\nint Can_Send(int v);\nint Pwm_HwSet(int d);\n",
    "AP/Function/PWM/src/pwm.c": '#include "g.h"\nint g_duty;\nint Pwm_Set(int d) { g_duty = d; return Pwm_HwSet(d) + Can_Send(d); }\n',
    "AP/Function/PWM/src/pwm_util.c": "int Pwm_Clamp(int d) { return d > 100 ? 100 : d; }\n",
    "AP/Function/CAN/can.c": '#include "g.h"\nint g_can_tx;\nint Can_Send(int v) { g_can_tx = v + g_duty; return 0; }\n',
    "BSW/Mcal/Pwm/Pwm.c": "int Pwm_HwSet(int d) { return d; }\n",
    "AP/Test/stub.c": "int Stub_Fn(void) { return 1; }\n",
}


def _spec(**kw):
    s = {"features": [], "structure": [], "ignore": [], "overrides": {}, "source": None}
    s.update(kw)
    return s


def _by_name(m):
    return {m.g.node(f)["name"]: (a["feature"], a["source"]) for f, a in m.func.items()}


def test_glob_to_regex():
    assert glob_to_regex("AP/Function/PWM/**").match("AP/Function/PWM/src/pwm.c")
    assert glob_to_regex("AP/Function/PWM").match("AP/Function/PWM/src/pwm.c")          # 폴더 이름만 주면 그 아래 전부
    assert not glob_to_regex("AP/Function/PWM/**").match("AP/Function/PWMX/a.c")
    assert glob_to_regex("**/pwm*.c").match("AP/Function/PWM/src/pwm_util.c")
    assert glob_to_regex("src/torque.c").match("src/torque.c") and not glob_to_regex("src/torque.c").match("src/torque.cpp")
    m = glob_to_regex("AP/Function/{feature}/**").match("ap/function/CAN/can.c")      # 대소문자 무시
    assert m and m.group("feature") == "CAN"
    assert glob_to_regex(r"AP\Function\PWM\**").match("AP/Function/PWM/a.c")             # 윈도우 구분자


def test_auto_structure_and_rules():
    g, _ = graph_of(SRC, args=["-Iinclude"])
    auto = _by_name(FeatureMapping(g, _spec()))
    # 'src' 같은 일반 폴더는 건너뛰고 기능 폴더 이름, 이름이 겹치면(PWM/Pwm) 위 폴더를 붙여 구분
    assert auto["Pwm_Set"] == ("Function/PWM", "structure") and auto["Pwm_Clamp"][0] == "Function/PWM"
    assert auto["Can_Send"] == ("CAN", "structure") and auto["Pwm_HwSet"][0] == "Mcal/Pwm"
    spec = _spec(structure=["AP/Function/{feature}/**"], ignore=["**/Test/**"], overrides={"Pwm_Clamp": "UTIL"},
                 features=[{"id": "MCAL", "name": "MCAL", "functions": ["Pwm_Hw*"], "paths": [], "swcs": [], "tests": ["T1"]}])
    m = FeatureMapping(g, spec)
    r = _by_name(m)
    assert r["Pwm_Set"] == ("PWM", "structure_rule") and r["Can_Send"] == ("CAN", "structure_rule")
    assert r["Pwm_HwSet"] == ("MCAL", "map_function") and r["Pwm_Clamp"] == ("UTIL", "override")
    assert "Stub_Fn" not in r and any(g.node(f)["name"] == "Stub_Fn" for f in m.ignored)
    # 같은 파일의 전역 변수는 그 파일 함수들의 기능
    assert m.var.get("loc:gv:g_duty") == "PWM" and m.var.get("loc:gv:g_can_tx") == "CAN"
    # 기능 간 의존: PWM -> CAN 호출, PWM이 쓰는 g_duty를 CAN이 읽음, PWM -> MCAL 호출
    dep = dependencies(ProgramGraph(g, Abstraction(g)), m)["pairs"]
    assert dep[("PWM", "CAN")]["calls"] == ["Pwm_Set→Can_Send"] and dep[("PWM", "CAN")]["vars"] == ["g_duty"]
    assert dep[("PWM", "MCAL")]["calls"] == ["Pwm_Set→Pwm_HwSet"]


def test_feature_map_file_validation(tmp_path):
    p = tmp_path / "fm.yaml"
    p.write_text("features:\n  - name: no id\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_feature_map(str(p))
    p.write_text("features:\n  - id: A\n    paths: src/a.c\nstructure: 'AP/{feature}/**'\n", encoding="utf-8")
    spec = load_feature_map(str(p))                  # 목록 대신 값 하나만 써도 됨
    assert spec["features"][0]["paths"] == ["src/a.c"] and spec["structure"] == ["AP/{feature}/**"]


def test_autosar_swc_exclusive_functions():
    from vfia.config import load_config
    from vfia.pipeline import get_graph
    g = get_graph(load_config(CFG), "after", log=lambda *a: None)
    r = _by_name(FeatureMapping(g, _spec()))
    # SWC_Torque의 Runnable(RE_TqTask=Tq_Task, RE_OutTask=Out_Task)에서만 호출되는 함수는 SWC로
    for fn in ("Tq_Task", "Out_Task", "CalcTorqueLimitPct", "ComputeTorqueCommand"):
        assert r[fn] == ("SWC_Torque", "autosar"), fn
    # SWC 밖(Spd_Process)에서도 호출되는 공용 함수는 SWC로 묶지 않음
    assert r["Flt_LowPass"][0] != "SWC_Torque" and r["Spd_Process"][0] == "speed"


@pytest.fixture(scope="module")
def example_out():
    out = tempfile.mkdtemp(prefix="vfia_fi_")
    p = subprocess.run([sys.executable, os.path.join(ROOT, "analyze_impact.py"), "--config", CFG, "--out", out,
                        "--no-behavior", "--quiet"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600, env=pt.subprocess_env_utf8())
    assert p.returncode == 0, p.stderr
    return out


def test_example_feature_impact(example_out):
    with open(os.path.join(example_out, "impact.json"), encoding="utf-8") as f:
        d = json.load(f)
    fi = d["feature_impact"]
    types = {c["id"]: c["type"] for c in d["changes"]}
    by = {c["change"]: c for c in fi["changes"]}
    thr = by[next(k for k, t in types.items() if t == "threshold_change")]
    assert thr["changed_features"] == ["TORQUE_LIMIT"]
    st = {fe["feature"]: fe for fe in thr["features"]}
    assert st["OUTPUT"]["stage"] == 1 and st["SENSOR_DIAG"]["stage"] == 1
    ev = [e["label"] for i in st["OUTPUT"]["incoming"] for e in i["evidence"]]
    assert any("g_tq_cmd" in x and "Tq_Task → Out_Task" in x for x in ev)          # 연결 근거(변수 이름)
    assert any(o["name"] == "TqCmd_Value" and o["kind"] == "RTE 신호" for o in thr["outputs"])
    # 차속 처리 변경은 2단계 간접 영향까지(SPEED -> TORQUE_LIMIT -> OUTPUT)
    early = by[next(k for k, t in types.items() if t == "early_exit_added")]
    est = {fe["feature"]: fe["stage"] for fe in early["features"]}
    assert est["SPEED"] == 0 and est["TORQUE_LIMIT"] == 1 and est["OUTPUT"] == 2
    # 태스크 주기 변경: 그 태스크에서 실행되는 기능은 실행 문맥 근거로 직접 영향
    tc = by[next(k for k, t in types.items() if t == "task_config_change")]
    tev = [e["label"] for fe in tc["features"] for i in fe["incoming"] for e in i["evidence"]]
    assert tev and all("주기 10→20ms" in x for x in tev)
    # 매핑 근거: 경로(map_path)와 SWC(map_swc)
    srcs = {f["id"]: f["sources"] for f in fi["features"]}
    assert srcs["TORQUE_LIMIT"] == {"map_swc": 4} and srcs["OUTPUT"] == {"map_path": 2}
    # 의존 행렬 전후 비교: 대입문 삭제로 last_limit 공유가 사라지고, 새 호출로 PLATFORM 의존이 생김
    cells = {(c["from"], c["to"]): c for c in fi["dependency"]["cells"]}
    assert cells[("TORQUE_LIMIT", "SENSOR_DIAG")]["status"] == "changed"
    assert cells[("TORQUE_LIMIT", "SENSOR_DIAG")]["removed"]["vars"] == ["g_tq_state.last_limit"]
    assert cells[("TORQUE_LIMIT", "PLATFORM")]["status"] == "added"
    # 회귀 시험 범위: 구분·지정 시험·경계 함수
    reg = {r["feature"]: r for r in fi["regression"]}
    assert reg["OUTPUT"]["tier_label"] == "직접 영향" and "TC_OUT_010 센서 고장 시 출력 0 차단" in reg["OUTPUT"]["tests"]
    assert "Out_Task" in reg["OUTPUT"]["interface"]
    s = fi["summary"]
    assert s["affected"] == len(reg) and s["changed"] + s["direct"] + s["indirect"] == s["affected"]


def test_example_feature_files_and_report(example_out):
    p = os.path.join(example_out, "feature_regression_scope.csv")
    with open(p, "rb") as f:
        assert f.read(3) == b"\xef\xbb\xbf"                     # Excel에서 바로 열리도록 BOM
    with open(p, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0][:3] == ["기능", "이름", "구분"] and len(rows) > 2
    with open(os.path.join(example_out, "feature_map_suggested.yaml"), encoding="utf-8") as f:
        sug = f.read()
    assert "TORQUE_LIMIT" in sug and "swcs: [SWC_Torque]" in sug
    from vfia.config import load_structured
    tmp = os.path.join(example_out, "sug_copy.yaml")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(sug)
    assert load_structured(tmp)["features"]                     # 제안 파일이 그대로 매핑 파일로 읽힘
    with open(os.path.join(example_out, "impact.html"), encoding="utf-8") as f:
        html = f.read()
    for need in ("id='tabbtn-feature'", "id='tab-feature'", "영향받는 기능", "id='fi-C003'", "href='#graph:C003:feature'",
                 "2. 회귀 시험 범위 제안", "3. 기능 간 의존 행렬", "id='tabbtn-graph'", "\"feature_units\""):
        assert need in html, need
    assert os.path.isfile(os.path.join(example_out, "graph_export", "features.graphml"))
