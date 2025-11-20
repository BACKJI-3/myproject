import os
import time
import threading
import json
import sqlite3
import re
import socket
import subprocess 
import signal 

from flask import Flask, render_template, request, send_from_directory, jsonify
from flask_socketio import SocketIO, emit

# ================== LLM ==================
try:
    from openai import OpenAI
    _LLM_SDK_OK = True
except Exception:
    _LLM_SDK_OK = False

OPENAI_API_KEY = os.environ.get(
    "OPENAI_API_KEY",
    "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"
)
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = 15.0 
_llm_sem = threading.BoundedSemaphore(8) 

# ================== 환경/DB ==================
CENTRAL_SERVER_IP = os.environ.get("CENTRAL_SERVER_IP", "192.168.0.4")
DATABASE_FILE = os.environ.get("DB_FILE", "training.db")

TEAM_MAP_JSON = os.environ.get("TEAM_MAP", "")
TEAM_PREFIX_A = os.environ.get("TEAM_PREFIX_A", "")
TEAM_PREFIX_B = os.environ.get("TEAM_PREFIX_B", "")
TEAM_NAME_A = os.environ.get("TEAM_NAME_A", "A팀")
TEAM_NAME_B = os.environ.get("TEAM_NAME_B", "B팀")

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
PACKET_SENDER_SCRIPT = "packet_sender.py" 
_current_sender_process = None 

# [신규] 게임 설정 전역 변수 (기본값: 라운드 10분, 대기 10초)
GAME_CONFIG = {
    "round_duration": 600,   # 라운드 진행 시간 (초)
    "prepare_duration": 10   # 라운드 시작 전 대기 시간 (초)
}

# ================== 앱/소켓 ==================
_async_mode = None
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = "very-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=_async_mode)

# ================== 외부 프로세스 관리 ==================
def start_packet_sender(round_num_str):
    global _current_sender_process
    try:
        _current_sender_process = subprocess.Popen(
            ["python3", PACKET_SENDER_SCRIPT, round_num_str],
            preexec_fn=os.setsid 
        )
        add_log(f"✅ {PACKET_SENDER_SCRIPT} 실행됨 (PID: {_current_sender_process.pid}, 라운드: {round_num_str})")
        return True
    except FileNotFoundError:
        add_log(f"❌ 오류: {PACKET_SENDER_SCRIPT} 스크립트를 찾을 수 없습니다.")
    except Exception as e:
        add_log(f"❌ 오류: {PACKET_SENDER_SCRIPT} 실행 실패: {e}")
    return False

def stop_packet_sender():
    global _current_sender_process
    if _current_sender_process:
        pid = _current_sender_process.pid
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            _current_sender_process.wait(timeout=5) 
            add_log(f"🛑 {PACKET_SENDER_SCRIPT} 종료됨 (PID: {pid})")
        except ProcessLookupError:
            add_log(f"🛑 {PACKET_SENDER_SCRIPT} 프로세스 (PID: {pid})가 이미 종료되었습니다.")
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            add_log(f"🛑 {PACKET_SENDER_SCRIPT} 강제 종료됨 (PID: {pid})")
        except Exception as e:
            add_log(f"❌ {PACKET_SENDER_SCRIPT} 종료 중 오류 발생: {e}")
        
        _current_sender_process = None
        return True
    return False

# ================== DB 유틸 ==================
def get_db_conn():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def get_all_team_names():
    try:
        with get_db_conn() as conn:
            rows = conn.execute("SELECT team_name FROM Teams").fetchall()
            names = [r["team_name"] for r in rows]
            return names or [TEAM_NAME_A, TEAM_NAME_B]
    except Exception:
        return [TEAM_NAME_A, TEAM_NAME_B]

# ================== 네트워크/식별 ==================
def _client_ip(req):
    xff = req.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return req.remote_addr or ""

def _server_host_ip():
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return ""

def _is_admin_request(req):
    ip = _client_ip(req)
    allowed = {CENTRAL_SERVER_IP, "127.0.0.1", _server_host_ip()}
    if ADMIN_KEY and req.args.get("key") == ADMIN_KEY:
        return True
    return ip in allowed

def _team_from_env_prefixes(ip: str):
    if TEAM_MAP_JSON:
        try:
            mapping = json.loads(TEAM_MAP_JSON)
            for prefix, tname in mapping.items():
                if ip.startswith(prefix):
                    return tname
        except Exception:
            pass
    if TEAM_PREFIX_A and ip.startswith(TEAM_PREFIX_A):
        return TEAM_NAME_A
    if TEAM_PREFIX_B and ip.startswith(TEAM_PREFIX_B):
        return TEAM_NAME_B
    
    if ip.startswith("192.168.11.227"):
        return TEAM_NAME_A
    if ip.startswith("192.168.11.228"):
        return TEAM_NAME_B
    return None

def team_from_ip(ip):
    if ip == CENTRAL_SERVER_IP or ip == _server_host_ip():
        return None
    try:
        with get_db_conn() as conn:
            rows = conn.execute("SELECT team_name, source_prefix FROM Teams").fetchall()
        for r in rows:
            sp = r["source_prefix"]
            if sp and ip.startswith(sp):
                return r["team_name"]
    except Exception:
        pass
    return _team_from_env_prefixes(ip)

# ================== 스코어/상태 ==================
def winners_from_scores(scores: dict):
    if not scores:
        return []
    top = max(scores.values())
    return [t for t, s in scores.items() if s == top]

def init_state():
    teams = get_all_team_names()
    return {
        "phase": "waiting", # waiting | preparing | running | reviewing | finished
        "game_over": False,
        "current_round": 0,
        "total_rounds": 0,
        "scores": {t: 0 for t in teams},
        "timer": 0, # 남은 초
        "status_message": "대기 중",
        "log": [],
        "is_submission_allowed": False,
        "submissions": {t: 0 for t in teams},
        "round_results": {}, # team -> round -> detail
        "pcap_file": "",
        "clue_file": "",
        "winners": [],
        "final_message": "",
        "postgame_feedback": None,
        "feedback_ready": False,
        "state_ver": 0,
    }

state = init_state()
lock = threading.RLock()
_loop_thread = None

def bump_broadcast():
    state["state_ver"] += 1
    socketio.emit("update_state", state)

def add_log(msg: str):
    ts = time.strftime("%H:%M:%S")
    state["log"].insert(0, f"[{ts}] {msg}")
    state["log"] = state["log"][:200]

def write_status_file():
    try:
        with open("game_status.json", "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ================== LLM 유틸 ==================
def _make_client():
    return OpenAI(api_key=OPENAI_API_KEY)

def _extract_text(resp):
    try:
        return resp.choices[0].message.content
    except Exception:
        try:
            return resp.output[0].content[0].text
        except Exception:
            text = getattr(resp, "output_text", None)
            if text:
                return text
            return str(resp)

def _llm_call_text(prompt: str) -> str:
    if not (_LLM_SDK_OK and OPENAI_API_KEY):
        return "LLM unavailable"
    try:
        ok = _llm_sem.acquire(timeout=LLM_TIMEOUT)
        if not ok:
            return "LLM busy"
        client = _make_client()
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500, 
                timeout=LLM_TIMEOUT + 10.0 
            )
            return _extract_text(resp)
        finally:
            _llm_sem.release()
    except Exception as e:
        try:
            _llm_sem.release()
        except Exception:
            pass
        return f"[AI 피드백 생성 실패] {e}"
        
def _extract_json_from_ai(response_str: str) -> dict:
    try:
        json_match = re.search(r'\{.*\}', response_str, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
        else:
            return {"score": 0, "reason": f"AI가 JSON을 반환하지 않음: {response_str}"}
    except Exception as e:
        return {"score": 0, "reason": f"AI JSON 파싱 실패: {e} (응답: {response_str})"}

# --- 프롬프트 함수들 ---
def build_feedback_prompt(team, rows, round_cfgs) -> str:
    round_analysis = []
    for r in rows:
        round_num = r['round']
        suri_score = r.get('suricata_score', 0)
        waf_score = r.get('waf_score', 0)
        suri_rule = r.get('submitted_suricata_rule', '제출 안함')
        waf_rule = r.get('submitted_waf_rule', '제출 안함')
        reason = r.get('reason', '채점 기록 없음')
        
        analysis_text = f"  - **{round_num}라운드: {suri_score + waf_score}점** (Suri: {suri_score}점, WAF: {waf_score}점)\n"
        analysis_text += f"    - 제출 룰 (Suri): `{suri_rule}`\n"
        analysis_text += f"    - 제출 룰 (WAF): `{waf_rule}`\n"
        analysis_text += f"    - AI 채점 요약: {reason}"
        
        round_analysis.append(analysis_text)

    return f"""
    Task: 당신은 {team}을(를) 담당하는 전문 보안 분석가입니다.
    아래는 {team}의 '라운드별 상세 분석' 데이터입니다. 이 데이터에는 '제출한 룰'과 'AI 채점 요약'이 포함되어 있습니다.
    
    당신의 임무는 이 데이터를 *기술적으로* 분석하여, 'Suricata 피드백'과 'WAF 피드백' 섹션을 작성하는 것입니다.
    
    요청사항:
    1.  '### 라운드별 상세 분석' 섹션은 아래 제공된 데이터 그대로 사용해주세요.
    2.  '### Suricata 피드백' 섹션을 작성해주세요. 
        - '라운드별 상세 분석' 데이터를 *모두* 참고하여, 제출한 룰들의 공통적인 실수(예: `http.uri`와 `http.request_body` 혼동)나 잘한 점을 'AI 채점 요약'을 바탕으로 *구체적으로* 요약해주세요.
        - (만약 룰을 하나도 제출하지 않았다면, 룰 작성의 중요성을 언급해주세요.)
    3.  '### WAF 피드백' 섹션을 2번과 동일한 방식으로 작성해주세요.
    4.  *절대로* 인사말이나 격려의 말을 추가하지 마세요. '### 라운드별 상세 분석'으로 시작해서 '### WAF 피드백'으로 끝나야 합니다.
    
    --- [데이터 시작] ---

    ### 라운드별 상세 분석
    {os.linesep.join(round_analysis)}

    ---
    
    ### Suricata 피드백
    (AI가 이 부분을 '라운드별 상세 분석' 데이터를 기반으로 전문적으로 작성)
    
    ### WAF 피드백
    (AI가 이 부분을 '라운드별 상세 분석' 데이터를 기반으로 전문적으로 작성)
    """

def build_final_feedback_verification_prompt(team: str, draft_feedback: str) -> str:
    return f"""
    Task: 당신은 선임 보안 분석가입니다.
    아래는 신입 분석가가 {team}을(를) 위해 작성한 '게임 종료 피드백' 초안입니다.
    이 초안의 내용이 '라운드별 상세 분석' 데이터와 일치하는지, 그리고 피드백이 *구체적이고 기술적인지* 검토해주세요.
    
    - 불필요한 인사말이나 격려의 말은 *모두 삭제*하세요.
    - 문맥을 더 전문적이고 간결하게 다듬어주세요.
    - Markdown 형식(###, `code` 등)은 그대로 유지해야 합니다.
    - 오직 '최종 완성본 피드백'만 한국어로 반환해주세요.

    --- [신입 분석가 초안] ---
    {draft_feedback}
    --- [여기까지가 초안입니다] ---
    """

def build_suricata_prompt(round_cfg, suri_rule: str) -> str:
    desc = round_cfg.get('description', 'N/A')
    pcap = round_cfg.get('pcap_file', 'N/A')
    if not suri_rule or len(suri_rule) < 10:
        return "No Suricata rule submitted" 
    return f"""
    Task: Grade the following Suricata rule based on the attack description.
    The score must be between 0 and 10.
    - 10 points: Perfect. Correctly identifies the 'detection area' (e.g., `http.uri`, `http.request_body`) AND all 'key keywords'.
    - 7-9 points: Good. Correct 'detection area', but misses one 'key keyword'.
    - 4-6 points: Partial. Correct 'detection area' but misses all 'key keywords' OR wrong 'detection area' but correct 'key keywords'.
    - 1-3 points: Incorrect. Wrong 'detection area' and wrong 'key keywords', but an attempt was made.
    - 0 points: Completely irrelevant.
    Attack Description (Problem): {desc} (related file: {pcap})
    Submitted Suricata Rule: `{suri_rule}`
    Respond ONLY with a JSON object in the format:
    {{"score": <score_number>, "reason": "<brief_reason_in_korean_for_the_score_using_terms_like_탐지_영역_and_핵심_키워드>"}}
    """

def build_waf_prompt(round_cfg, waf_rule: str) -> str:
    desc = round_cfg.get('description', 'N/A')
    pcap = round_cfg.get('pcap_file', 'N/A')
    if not waf_rule or len(waf_rule) < 5:
        return "No WAF rule submitted"
    return f"""
    Task: Grade the following WAF (Web Application Firewall) rule based on the attack description.
    The score must be between 0 and 10.
    - 10 points: Perfect rule that blocks the specific attack effectively without false positives.
    - 7-9 points: Good rule, but has minor flaws, is too broad, or could be bypassed.
    - 4-6 points: The rule is relevant but has significant flaws or only partially blocks the attack.
    - 1-3 points: The rule attempts to solve the problem but is mostly incorrect or ineffective.
    - 0 points: Rule is completely irrelevant to the attack.
    Attack Description (Problem): {desc} (related file: {pcap})
    Submitted WAF Rule: `{waf_rule}`
    Respond ONLY with a JSON object in the format:
    {{"score": <score_number>, "reason": "<brief_reason_in_korean_for_the_score>"}}
    """

def build_verification_prompt(rule_type: str, round_cfg, rule: str, initial_score: int, initial_reason: str) -> str:
    desc = round_cfg.get('description', 'N/A')
    return f"""
    Task: You are a senior security expert. Please review the following grading.
    A junior analyst provided an initial score. Verify if it is correct, consistent, and fair.
    Attack Description: {desc}
    Rule Type: {rule_type}
    Submitted Rule: `{rule}`
    Junior Analyst's Grade:
    - Score: {initial_score} / 10
    - Reason (in Korean): "{initial_reason}"
    Your job is to provide the FINAL grade. You can agree with the junior or correct them.
    Focus on consistency. Does the score {initial_score} match the reason "{initial_reason}"?
    Respond ONLY with the FINAL JSON object in the format:
    {{"score": <final_score_number>, "reason": "<final_reason_in_korean>"}}
    """

def judge_with_llm(round_cfg, suri_rule: str, waf_rule: str):
    suri_score = 0
    suri_reason = "Suricata 룰이 제출되지 않았습니다."
    waf_score = 0
    waf_reason = "Modsecurity 룰이 제출되지 않았습니다."

    suri_rule_clean_for_check = suri_rule.strip()
    is_suri_meaningful = suri_rule_clean_for_check and len(suri_rule_clean_for_check) > 10
    if is_suri_meaningful:
        try:
            suri_prompt_1 = build_suricata_prompt(round_cfg, suri_rule)
            ai_response_str_1 = _llm_call_text(suri_prompt_1)
            ai_json_1 = _extract_json_from_ai(ai_response_str_1)
            suri_score_1 = int(ai_json_1.get("score", 0))
            suri_reason_1 = ai_json_1.get("reason", "AI가 1차 채점 이유를 반환하지 않았습니다.")
            suri_prompt_2 = build_verification_prompt("Suricata", round_cfg, suri_rule, suri_score_1, suri_reason_1)
            ai_response_str_2 = _llm_call_text(suri_prompt_2)
            ai_json_2 = _extract_json_from_ai(ai_response_str_2)
            suri_score = int(ai_json_2.get("score", 0))
            suri_reason = ai_json_2.get("reason", "AI가 2차(최종) 채점 이유를 반환하지 않았습니다.").strip().strip("'\"")
        except Exception as e:
            suri_reason = f"Suricata 채점 AI 호출 중 오류 발생: {e}"
            suri_score = 0
    elif suri_rule.strip():
        suri_reason = "제출된 Suricata 룰이 너무 짧거나 의미가 없어 0점 처리되었습니다."
        suri_score = 0

    waf_rule_clean_for_check = waf_rule.lower().strip()
    is_waf_meaningful = waf_rule_clean_for_check and waf_rule_clean_for_check not in ["12", "123", "afdk", "12312", ""] and len(waf_rule_clean_for_check) > 5
    if is_waf_meaningful:
        try:
            waf_prompt_1 = build_waf_prompt(round_cfg, waf_rule)
            ai_response_str_1 = _llm_call_text(waf_prompt_1)
            ai_json_1 = _extract_json_from_ai(ai_response_str_1)
            waf_score_1 = int(ai_json_1.get("score", 0))
            waf_reason_1 = ai_json_1.get("reason", "AI가 1차 채점 이유를 반환하지 않았습니다.")
            waf_prompt_2 = build_verification_prompt("Modsecurity", round_cfg, waf_rule, waf_score_1, waf_reason_1)
            ai_response_str_2 = _llm_call_text(waf_prompt_2)
            ai_json_2 = _extract_json_from_ai(ai_response_str_2)
            waf_score = int(ai_json_2.get("score", 0))
            waf_reason = ai_json_2.get("reason", "AI가 2차(최종) 채점 이유를 반환하지 않았습니다.").strip().strip("'\"")
        except Exception as e:
            waf_reason = f"Modsecurity 채점 AI 호출 중 오류 발생: {e}"
            waf_score = 0
    elif waf_rule.strip():
        waf_reason = "제출된 Modsecurity 룰이 너무 짧거나 의미가 없어 0점 처리되었습니다."
        waf_score = 0
        
    if suri_score > 10: suri_score = 10
    if waf_score > 10: waf_score = 10
    total = suri_score + waf_score
    penalty = 0
    suri_trash = (suri_rule.strip() and suri_score == 0 and not is_suri_meaningful)
    waf_trash = (waf_rule.strip() and waf_score == 0 and not is_waf_meaningful)
    if (suri_trash and waf_trash) or (not suri_rule.strip() and waf_trash) or (suri_trash and not waf_rule.strip()):
        penalty = 5
        total -= penalty
        
    return {
        "suricata_score": suri_score,
        "waf_score": waf_score,
        "total_score": total,
        "penalty": penalty,
        "reason": f"Suricata: {suri_reason} / Modsecurity: {waf_reason}",
    }

def apply_non_submission_penalty():
    teams = get_all_team_names()
    r = state["current_round"]
    if r < 1:
        return
    for t in teams:
        if state["submissions"].get(t, 0) < 1:
            state["scores"][t] -= 5
            add_log(f"{t}: 라운드 {r} 미제출 (-5점)")

def update_final_feedback_snapshot():
    summary = {}
    try:
        with get_db_conn() as conn:
            cfg_rows = conn.execute("SELECT * FROM Problems").fetchall()
            round_cfgs = {cfg['problem_id']: dict(cfg) for cfg in cfg_rows}
        with lock:
            per_team = state["round_results"].copy()
        for team, rounds in per_team.items():
            try:
                rows = []
                for rnd, res in sorted(rounds.items()):
                    suri_rule = res.get("submitted_suricata_rule", "").strip()
                    waf_rule = res.get("submitted_waf_rule", "").strip()
                    if waf_rule.lower() in ["12", "123", "afdk", "12312", ""] or len(waf_rule) < 5:
                        waf_rule = ""
                    rows.append({
                        "round": rnd,
                        "suricata_score": res.get("suricata_score", 0), 
                        "waf_score": res.get("waf_score", 0),
                        "submitted_suricata_rule": suri_rule,
                        "submitted_waf_rule": waf_rule,
                        "reason": res.get("reason", "")
                    })
                if rows:
                    draft_prompt = build_feedback_prompt(team, rows, round_cfgs)
                    draft_feedback = _llm_call_text(draft_prompt)
                    final_prompt = build_final_feedback_verification_prompt(team, draft_feedback)
                    final_feedback = _llm_call_text(final_prompt)
                    summary[team] = final_feedback
                else:
                    summary[team] = "제출 기록이 없습니다."
            except Exception as e:
                summary[team] = f"피드백 생성 중 오류가 발생했습니다: {e}"
    except Exception as e:
        summary["error"] = f"피드백 생성 중 심각한 오류가 발생했습니다: {e}"
    finally:
        with lock:
            state["postgame_feedback"] = summary
            state["feedback_ready"] = True
            bump_broadcast() 

def finish_game(reason="게임 종료"): 
    with lock:
        if state["phase"] == "running" or state["phase"] == "reviewing":
            if state["phase"] == "running":
                apply_non_submission_penalty()
        state["phase"] = "finished"
        state["is_submission_allowed"] = False
        state["timer"] = 0
        w = winners_from_scores(state["scores"])
        state["winners"] = w
        state["final_message"] = reason + (f" — 우승: {', '.join(w)}" if w else "")
        add_log(state["final_message"])
        if not state.get("postgame_feedback"):
            add_log("제출 기록이 없어, 지금부터 최종 피드백을 생성합니다.")
            socketio.start_background_task(update_final_feedback_snapshot)
        else:
            state["feedback_ready"] = True 
        write_status_file()
        bump_broadcast() 

REVIEW_TIME = 30 

def game_loop():
    global _current_sender_process
    try:
        with get_db_conn() as conn:
            rounds = conn.execute(
                "SELECT * FROM Problems ORDER BY problem_id"
            ).fetchall()
    except Exception as e:
        with lock:
            add_log(f"[DB] 라운드 로드 실패: {e}")
            state["phase"] = "waiting"
            state["current_round"] = 0
            bump_broadcast()
        return
    if not rounds:
        with lock:
            add_log("[DB] 오류: Problems 테이블에 라운드(문제)가 0개입니다.")
            state["phase"] = "waiting"
            bump_broadcast()
        return
    teams = get_all_team_names()
    with lock:
        state["total_rounds"] = len(rounds)
        write_status_file()
        bump_broadcast()
    stop_all = False
    
    for i, cfg_row in enumerate(rounds):
        if stop_all:
            break
        r = i + 1
        cfg = dict(cfg_row) 
        
        # [수정] 전역 설정값 사용
        LIMIT_TIME = GAME_CONFIG["round_duration"] 
        PREPARE_TIME = GAME_CONFIG["prepare_duration"]

        # 0. PREPARING PHASE (라운드 시작 전 대기)
        with lock:
            state["phase"] = "preparing"
            state["current_round"] = r 
            state["status_message"] = f"{r}라운드 준비..."
            state["timer"] = PREPARE_TIME
            state["pcap_file"] = cfg["pcap_file"]
            state["clue_file"] = cfg["clue_file"]
            state["is_submission_allowed"] = False 
            state["submissions"] = {t: 0 for t in teams} 
            state["feedback_ready"] = False 
            add_log(f"{r}라운드 {PREPARE_TIME}초 후 시작...")
            bump_broadcast()
        for t in range(PREPARE_TIME, -1, -1):
            with lock:
                if state["phase"] != "preparing": 
                    stop_all = True
                    break
                state["timer"] = t
                bump_broadcast()
            if t > 0:
                socketio.sleep(1.0)
        if stop_all: break

        # 1. RUNNING PHASE (문제 풀이 시간)
        with lock:
            if state["phase"] == "finished":
                break
            state["phase"] = "running"
            state["current_round"] = r
            state["status_message"] = f"작전 시간 (라운드 {r}/{state['total_rounds']})"
            state["is_submission_allowed"] = True
            state["timer"] = LIMIT_TIME
            add_log(f"{r}라운드 시작! ({LIMIT_TIME}초)")
            write_status_file()
            bump_broadcast()
        try:
            _current_sender_process = subprocess.Popen(
                ["python3", PACKET_SENDER_SCRIPT, str(r)],
                preexec_fn=os.setsid 
            )
            add_log(f"[자동] 패킷 전송 시작 (PID: {_current_sender_process.pid})")
        except Exception as e:
            add_log(f"[오류] 패킷 전송 스크립트 실행 실패: {e}")
        for t in range(LIMIT_TIME, -1, -1):
            with lock:
                if state["phase"] != "running":
                    stop_all = True
                    break
                state["timer"] = t
                if _current_sender_process and _current_sender_process.poll() is not None:
                     add_log(f"[알림] 패킷 전송 스크립트가 조기 종료됨 (Exit Code: {_current_sender_process.returncode})")
                     _current_sender_process = None
                bump_broadcast()
                everyone_submitted = all(
                    state["submissions"].get(team, 0) >= 1 for team in teams
                )
                if everyone_submitted:
                    add_log("모든 팀 제출 완료 → 조기 종료")
                    break
            if t > 0 and not everyone_submitted:
                socketio.sleep(1.0)
        if stop_all:
            break
        if _current_sender_process:
            stop_packet_sender()
        with lock:
            state["is_submission_allowed"] = False
            apply_non_submission_penalty()
            add_log(f"{r}라운드 채점 완료")
            state["status_message"] = f"분석 시간 (라운드 {r})"
            write_status_file()
        
        # 2. REVIEWING PHASE (분석 시간)
        with lock:
            state["phase"] = "reviewing"
            state["timer"] = REVIEW_TIME
            if state.get("postgame_feedback"):
                 state["feedback_ready"] = True
            bump_broadcast()
        for t in range(REVIEW_TIME, -1, -1):
            with lock:
                if state["phase"] != "reviewing": 
                    stop_all = True
                    break
                state["timer"] = t
                bump_broadcast()
            if t > 0:
                socketio.sleep(1.0)
        if stop_all:
            break
        with lock:
            state["status_message"] = f"{r}라운드 종료, 다음 라운드 준비 중..."
            state["timer"] = 0
            bump_broadcast()
        socketio.sleep(3.0) 
    finish_game("게임 종료") 

@app.route("/")
def index():
    teams = get_all_team_names()
    return render_template("index.html", teams=teams)

@app.route("/admin")
def admin_page():
    if not _is_admin_request(request): return "Access Denied", 403
    return render_template("admin.html")

@app.post("/api/admin/config")
def update_game_config():
    if not _is_admin_request(request): return jsonify({"error": "Access Denied"}), 403
    try:
        data = request.json
        # 라운드 시간 설정
        new_duration = int(data.get("round_duration", 600))
        if new_duration < 10: new_duration = 10 
        GAME_CONFIG["round_duration"] = new_duration

        # [신규] 대기 시간 설정
        new_prepare = int(data.get("prepare_duration", 10))
        if new_prepare < 0: new_prepare = 0
        GAME_CONFIG["prepare_duration"] = new_prepare

        add_log(f"[관리자] 설정 변경: 라운드 {new_duration}초, 대기 {new_prepare}초")
        return jsonify({"status": "success", "config": GAME_CONFIG})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.get("/api/admin/config")
def get_game_config():
    if not _is_admin_request(request): return jsonify({"error": "Access Denied"}), 403
    return jsonify(GAME_CONFIG)

@app.route("/submit_page")
def submit_page():
    return render_template("submit.html")

@app.route("/download/<filename>")
def download_file(filename):
    pcap_dir = os.path.join(app.root_path, 'pcap_files')
    return send_from_directory(pcap_dir, filename, as_attachment=True)

@app.get("/api/state")
def api_state():
    with lock:
        return jsonify(state)

@app.get("/postgame_feedback")
def postgame_feedback():
    with lock:
        all_feedback = state.get("postgame_feedback")
        if not all_feedback:
            if state["phase"] == "waiting":
                return jsonify({"A팀": "게임이 시작되지 않았습니다."})
            return jsonify({"A팀": "최종 분석 결과를 생성 중입니다... 잠시 후 다시 시도해주세요."})
        ip = _client_ip(request)
        team = team_from_ip(ip)
        if team and team in all_feedback:
            return jsonify({team: all_feedback[team]})
        else:
            return jsonify(all_feedback)

@app.get("/whoami")
def whoami():
    ip = _client_ip(request)
    t = team_from_ip(ip)
    return jsonify({"ip": ip, "team": t})

@app.get("/start")
def start():
    if not _is_admin_request(request):
        return "Access Denied", 403
    global _loop_thread
    with lock:
        if state.get("phase") in ["preparing", "running", "reviewing"]:
            return "게임이 이미 진행 중입니다. 중단하려면 /stop을 사용하세요.", 400
        fresh = init_state()
        state.clear()
        state.update(fresh)
        add_log("게임이 시작됩니다.")
        write_status_file()
        bump_broadcast()
    _loop_thread = socketio.start_background_task(game_loop)
    return "게임 시작"

@app.get("/stop")
def stop():
    if not _is_admin_request(request):
        return "Access Denied", 403
    stop_packet_sender()
    finish_game("관리자에 의해 게임이 종료되었습니다.") 
    return "중단되었습니다."

@socketio.on("connect")
def on_connect():
    with lock:
        if state["phase"] in ("waiting", "preparing", "running", "reviewing"):
            state["game_over"] = False
            state["winners"] = []
            state["final_message"] = ""
        emit("update_state", state)

@socketio.on("submit_answer")
def on_submit(payload):
    try:
        current_round_cache = 0
        team_cache = ""
        cfg_row_cache = None
        sid_cache = request.sid 

        with lock:
            if state["phase"] != "running" or not state.get("is_submission_allowed", False):
                emit("submit_response", {"status": "error", "message": "지금은 제출 시간이 아닙니다."})
                return
            ip = _client_ip(request)
            team = team_from_ip(ip)
            if not team:
                emit("submit_response", {"status": "error", "message": "팀을 식별할 수 없습니다."})
                return
            if state["submissions"].get(team, 0) >= 1:
                emit("submit_response", {"status": "error", "message": "이미 제출했습니다."})
                return
            state["submissions"][team] = state["submissions"].get(team, 0) + 1
            emit("submit_response", {"status": "info", "message": "AI가 채점 중입니다... (최대 10초 소요)"})
            bump_broadcast() 
            team_cache = team
            current_round_cache = state["current_round"]

        suri = (payload.get("suricata_rule") or "").strip()
        waf = (payload.get("waf_rule") or "").strip()

        with get_db_conn() as conn:
            cfg_row_cache = conn.execute(
                "SELECT * FROM Problems WHERE problem_id=?",
                (current_round_cache,)
            ).fetchone()
        
        if not cfg_row_cache:
            emit("submit_response", {"status": "error", "message": "라운드 정보를 찾을 수 없습니다."})
            with lock:
                state["submissions"][team_cache] = 0
                bump_broadcast()
            return
        
        socketio.start_background_task(
            _process_submission_task,
            team=team_cache,
            current_round=current_round_cache,
            suri=suri,
            waf=waf,
            cfg_dict=dict(cfg_row_cache),
            sid=sid_cache
        )
    except Exception as e:
        emit("submit_response", {"status": "error", "message": f"서버 예외: {e}"})

def _process_submission_task(team, current_round, suri, waf, cfg_dict, sid):
    try:
        judged = judge_with_llm(cfg_dict, suri, waf) 
        with lock:
            if state["current_round"] != current_round or state["phase"] != "running":
                socketio.emit("submit_response", {
                    "status": "error",
                    "message": f"제출 시간이 초과되어 {current_round}라운드 채점 결과가 반영되지 않았습니다."
                }, room=sid)
                state["submissions"][team] = 0
                bump_broadcast()
                return

            judged_with_submission = {
                "submitted_suricata_rule": suri,
                "submitted_waf_rule": waf,
                **judged 
            }
            state["round_results"].setdefault(team, {})[current_round] = judged_with_submission
            state["scores"][team] = state["scores"].get(team, 0) + judged["total_score"]
            if judged["total_score"] > 0:
                add_log(
                    f"{team} 정답: +{judged['total_score']}점 "
                    f"(Suricata {judged['suricata_score']}, WAF {judged['waf_score']})"
                )
            else:
                add_log(
                    f"{team} 오답: {judged['total_score']}점 "
                    f"(감점 {judged['penalty']} 포함)"
                )
            write_status_file()
            bump_broadcast() 

        update_final_feedback_snapshot() 
        socketio.emit("submit_response", {
            "status": "success" if judged["total_score"] > 0 else "fail",
            "message": f"총 {judged['total_score']}점",
            "detail": judged 
        }, room=sid)
    except Exception as e:
        socketio.emit("submit_response", {
            "status": "error", 
            "message": f"채점 중 서버 예외 발생: {e}"
        }, room=sid)
        with lock:
            state["submissions"][team] = 0
            bump_broadcast()

if __name__ == "__main__":
    with lock:
        fresh = init_state()
        state.clear()
        state.update(fresh)
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("APP_PORT", "5000")), debug=False)