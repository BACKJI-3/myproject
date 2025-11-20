import time
import json
import subprocess
import os
import re
import multiprocessing
import sqlite3 # [추가] sqlite3 모듈 임포트

# --- 설정 ---
PCAP_DIRECTORY = 'pcap_files'
STATUS_FILE = 'game_status.json'
SEND_INTERVAL = 10  # 전송 간격 (초)
DATABASE_FILE = 'training.db' # [추가] DB 파일 경로

# --- 함수 정의 ---

# [추가] DB 연결 함수
def get_db_conn():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row 
    return conn

# [수정] 디버깅을 위해 curl 오류를 출력하도록 변경
def _execute_curl_task(curl_command):
    """(Helper) 병렬 처리를 위해 단일 curl 명령을 실행하는 함수"""
    try:
        # capture_output=True로 stdout/stderr를 캡처하고, 5초 타임아웃 설정
        result = subprocess.run(curl_command, capture_output=True, text=True, timeout=5)
        
        # [수정] curl 실행이 실패하면 (returncode != 0), 오류 메시지 출력
        # (예: 7 = 'Connection refused', 28 = 'Timeout')
        if result.returncode != 0:
            # stderr에 내용이 있으면 stderr를, 없으면 stdout을 출력
            error_output = result.stderr if result.stderr else result.stdout
            # 보기 편하도록 curl 명령어 일부와 오류 내용 출력
            print(f"    [!] Curl Error (Target: {curl_command[-1]}): {error_output.strip()}")
            
    except subprocess.TimeoutExpired:
        print(f"    [!] Curl Timeout: {curl_command[-1]}")
    except Exception as e:
        # 병렬 작업 중 개별 오류 (e.g., curl 못찾음)
        print(f"    [!] Subprocess Error: {e}")

# ▼▼▼ [수정된 함수] ▼▼▼
def send_pcap_to_waf(pcap_path, target_ip, team_name):
    """tshark와 curl을 사용하여 PCAP에서 추출한 HTTP 요청을 WAF로 병렬 전송합니다."""
    print(f"\n--- [WAF (tshark+curl) 공격 시작] ---")
    
    # [수정 1] Tshark 필드 확장: Host, User-Agent, Referer, Cookie 추출 추가
    tshark_command = [
        'tshark', '-r', pcap_path, '-Y', 'http.request', '-T', 'fields',
        '-e', 'http.request.method', 
        '-e', 'http.host', 
        '-e', 'http.request.uri', 
        '-e', 'http.file_data',
        '-e', 'http.user_agent',   # [추가] User-Agent 헤더 추출 (Log4Shell 등)
        '-e', 'http.referer',      # [추가] Referer 헤더 추출
        '-e', 'http.cookie'        # [추가] Cookie 헤더 추출 (IDOR 등)
    ]

    try:
        result = subprocess.run(tshark_command, capture_output=True, text=True, check=True)
        
        all_curl_commands = []
        lines = result.stdout.strip().split('\n')

        if not any(lines):
            print(f"[!] {os.path.basename(pcap_path)}에서 HTTP 요청을 찾을 수 없습니다.")
            return
            
        print(f"[*] {team_name} 타겟({target_ip})으로 {len(lines)}개의 HTTP 공격 요청을 병렬 전송합니다...")

        for line in lines:
            if not line: continue
            
            # parts 순서: method, host, uri, post_data_hex, user_agent, referer, cookie
            parts = line.split('\t')
            # 추출된 필드 개수 보정 (없으면 빈 문자열 할당)
            parts.extend([''] * (7 - len(parts)))
            method, host, uri, post_data_hex, user_agent, referer, cookie = parts[:7]

            target_url = f"http://{target_ip}{uri}"
            
            # [수정 2] curl 기본 옵션 수정: --globoff 추가 (SSTI 공격 {{ }} 파손 방지)
            curl_command = ['curl', '-s', '-o', '/dev/null', '--max-time', '5', '--globoff']

            # [수정 3] 추출된 헤더들을 curl 명령어에 추가
            if host:
                curl_command.extend(['-H', f'Host: {host}'])
            if user_agent:
                curl_command.extend(['-H', f'User-Agent: {user_agent}'])
            if referer:
                curl_command.extend(['-H', f'Referer: {referer}'])
            if cookie:
                curl_command.extend(['-H', f'Cookie: {cookie}'])
            
            if method == "POST":
                # [수정 4] POST 데이터 처리 로직 강화: fromhex 에러 및 raw 데이터 대응
                if post_data_hex:
                    post_data_text = post_data_hex # 기본은 텍스트로 가정
                    
                    # 1. 16진수 문자가 포함되어 있는지 확인하여 fromhex 오류를 회피
                    if all(c in '0123456789abcdefABCDEF:' for c in post_data_hex):
                        try:
                            post_data_text = bytes.fromhex(post_data_hex.replace(':', '')).decode('utf-8', 'ignore')
                        except ValueError:
                            pass # 디코딩 실패 시 원래 텍스트를 사용
                    
                    # 2. -d 대신 --data-raw 사용 (Desync, File Upload, URL 인코딩 방지)
                    curl_command.extend(['-X', 'POST', '--data-raw', post_data_text])
                else:
                    curl_command.extend(['-X', 'POST'])
            else:
                curl_command.extend(['-X', method])

            curl_command.append(target_url)
            all_curl_commands.append(curl_command)
            
        # 멀티프로세싱 풀을 사용하여 준비된 모든 curl 명령을 병렬로 실행
        if all_curl_commands:
            worker_count = max(1, multiprocessing.cpu_count() // 2)
            with multiprocessing.Pool(processes=worker_count) as pool:
                pool.map(_execute_curl_task, all_curl_commands)

        print(f"    -> {team_name} 타겟 전송 완료.")

    except FileNotFoundError:
        print("[!] 오류: 'tshark'가 설치되어 있지 않습니다. (sudo apt install tshark)")
    except subprocess.CalledProcessError:
        print(f"[!] {os.path.basename(pcap_path)}에서 HTTP 요청을 찾을 수 없거나 tshark 실행 오류 발생.")
    except Exception as e:
        print(f"[!] WAF 전송 중 알 수 없는 오류: {e}")
# ▲▲▲ [수정된 함수 완료] ▲▲▲


def main_loop():
    print(">>> [WAF 전용] 공격 패킷 전송 시스템(packet_sender)을 시작합니다. (Ctrl+C로 종료)")
    
    # ▼▼▼ [수정] JSON 파일 로드 대신 DB에서 설정 로드 ▼▼▼
    try:
        with get_db_conn() as conn:
            # 1. 라운드(문제) 정보 로드 (pcap_file 컬럼 필요)
            rounds_config = conn.execute("SELECT problem_id, pcap_file FROM Problems ORDER BY problem_id").fetchall()
            if not rounds_config:
                print("[!] 치명적 오류: DB의 'Problems' 테이블에 라운드 정보가 없습니다."); return

            # 2. 팀 정보 로드
            teams_rows = conn.execute("SELECT team_name, target_ip FROM Teams").fetchall()
            # 기존 로직과 호환되도록 dict로 변환
            teams_config = {row['team_name']: {'target_ip': row['target_ip']} for row in teams_rows}
            if not teams_config:
                print("[!] 치명적 오류: DB의 'Teams' 테이블에 팀 정보가 없습니다."); return

        print(f"[*] 라운드 {len(rounds_config)}개 로드 완료.")
        print(f"[*] 타겟 팀 정보 로드 완료: {list(teams_config.keys())}")
        
    except sqlite3.Error as e:
        print(f"[!] 치명적 오류: 데이터베이스 로드 실패: {e}"); return
    except Exception as e:
        print(f"[!] 치명적 오류: {e}"); return
    # ▲▲▲ [수정 완료] ▲▲▲

    # --- 여기가 핵심 루프 ---
    while True:
        try:
            with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                game_state = json.load(f)

            current_round = game_state.get('current_round', 0)
            status_message = game_state.get('status_message', '')

            print(f"\n[상태 확인] 현재 라운드: {current_round} (상태: {status_message})")

            if current_round > 0 and "진행 중" in status_message:
                round_index = current_round - 1
                if 0 <= round_index < len(rounds_config):
                    current_round_config = rounds_config[round_index]
                    
                    # ▼▼▼ [수정] DB 컬럼명('pcap_file')으로 접근 ▼▼▼
                    pcap_filename = current_round_config['pcap_file']
                    # ▲▲▲ [수정 완료] ▲▲▲

                    if not pcap_filename:
                        print(f"[!] 현재 라운드에 pcap 파일이 설정되지 않았습니다.")
                        time.sleep(SEND_INTERVAL)
                        continue

                    original_pcap_path = os.path.join(PCAP_DIRECTORY, pcap_filename)
                    if not os.path.exists(original_pcap_path):
                        print(f"[!] 원본 pcap 파일을 찾을 수 없습니다 -> {original_pcap_path}")
                        time.sleep(SEND_INTERVAL)
                        continue
                        
                    for team_name, team_info in teams_config.items():
                        target_ip = team_info.get('target_ip')
                        if target_ip:
                            # 2. WAF용 tshark+curl 전송
                            send_pcap_to_waf(original_pcap_path, target_ip, team_name)
                        else:
                            # ▼▼▼ [수정] teams.json 대신 DB를 명시 ▼▼▼
                            print(f"[!] {team_name}의 'target_ip'가 DB에 설정되지 않았습니다.")
                else:
                    print(f"[!] 잘못된 라운드 번호({current_round})입니다.")
            
            else:
                print(f"    -> 게임이 '진행 중'이 아니므로 {SEND_INTERVAL}초간 대기합니다.")
            
            time.sleep(SEND_INTERVAL)

        except FileNotFoundError:
            print(f"[대기] 게임 시작을 기다리고 있습니다... ({STATUS_FILE} 파일 없음)")
            time.sleep(SEND_INTERVAL)
        except json.JSONDecodeError:
            print(f"[!] {STATUS_FILE} 파일이 손상되었습니다. 5초 후 다시 시도합니다.")
            time.sleep(5)
        except KeyboardInterrupt:
            print("\n>>> 스크립트를 종료합니다."); break

if __name__ == '__main__':
    main_loop()