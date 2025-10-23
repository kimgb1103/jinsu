# mes_login_step1.py
# ----------------------------------------
# 1) 로그인 + [변환 및 출고] 클릭 시 LOT 재고조회 화면
# 2) 변환 미리보기 표 하단 같은 줄에 [🧾 기타출고] · [🏷️ 라벨출력] · [📥 기타입고] 버튼
#    - [기타출고]: top-save → lot-save → transfer
#    - [라벨출력]: 변환미리보기 + 품목 API로 라벨 HTML 생성 → 팝업(브라우저 인쇄)
#    - [기타입고]: top-save → top-list(accountResultId 확보)
#                → bottom-save → (전송) menugrid-data-cnt → bottom-transmit-proc → top-transmit-proc
# 3) UI/동작은 기존 요구사항 유지
# 4) 추가: [라벨출력] 옆 LH/RH 인쇄 버튼, [저장]/[불러오기] 가로 배치 + JSON 저장/복원
# 5) 검색조건 초기화: 세션값 직접 대입으로 초기화, q_limit 경고 제거
# 6) 불러오기 후 NaN/누락 컬럼 자동보정(IDs/UOM/Warehouse) + 안전 캐스팅으로 기타출고 오류 해결
# ----------------------------------------

import json
import re
import base64
import datetime as dt
from typing import Any, Dict, Optional, List, Tuple, Set

import requests
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, DataReturnMode, JsCode
import streamlit.components.v1 as components

# =========================
# 전역 설정 (다크모드 + 페이지 설정)
# =========================

st.set_page_config(page_title="MES 로그인 (1단계)", layout="wide")

DARK_CSS = """
<style>
:root {
  --bg: #0f1115;
  --panel: #151821;
  --panel-2: #171a24;
  --text: #d7dbe7;
  --muted: #9aa3b2;
  --accent: #5ac8fa;
  --accent-2: #7ee081;
  --danger: #ff6b6b;
  --border: #242a38;
}
html, body, [data-testid="stAppViewContainer"] {
  background-color: var(--bg) !important;
  color: #d7dbe7 !important;
}
[data-testid="stHeader"] { background-color: rgba(0,0,0,0); }
.block-container { padding-top: 1rem; }
[data-testid="stSidebar"] { background-color: var(--panel); }
div.stButton>button, button[kind="primary"] {
  background: linear-gradient(135deg, var(--accent), var(--accent-2)) !important;
  color: #0b1020 !important;
  border: 0 !important;
  border-radius: 10px !important;
  font-weight: 700 !important;
}
.stTextInput>div>div>input, .stPassword>div>div>input, .stSelectbox>div>div>select, .stTextArea textarea {
  background-color: var(--panel-2) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
  border-radius: 8px !important;
}
</style>
"""
st.markdown(DARK_CSS, unsafe_allow_html=True)

# 사이드바 숨김 플래그 기본값
if "collapse_sidebar" not in st.session_state:
  st.session_state["collapse_sidebar"] = False

# 플래그가 True일 때 CSS로 숨김 처리
if st.session_state["collapse_sidebar"]:
  st.markdown(
    """
    <style>
    [data-testid="stSidebar"] { display: none !important; }
    .block-container { padding-left: 2rem !important; padding-right: 2rem !important; }
    </style>
    """,
    unsafe_allow_html=True,
  )

# =========================
# 유틸 함수
# =========================
def _b64url_to_json(b64url_str: str) -> Optional[Dict[str, Any]]:
  try:
    rem = len(b64url_str) % 4
    if rem:
      b64url_str += "=" * (4 - rem)
    decoded = base64.urlsafe_b64decode(b64url_str.encode("utf-8")).decode("utf-8")
    return json.loads(decoded)
  except Exception:
    return None

def parse_jwt_exp(jwt_token: str) -> Optional[dt.datetime]:
  try:
    parts = jwt_token.split(".")
    if len(parts) != 3:
      return None
    payload = _b64url_to_json(parts[1])
    if not payload:
      return None
    exp = payload.get("exp")
    if exp is None:
      return None
    return dt.datetime.fromtimestamp(int(exp), tz=dt.timezone.utc)
  except Exception:
    return None

def _split_or_terms(s: str) -> List[str]:
  if not s:
    return []
  parts = re.split(r"\s*(?:\b또는\b|\||,)\s*", s.strip())
  return [p for p in parts if p]

def _wildcard_to_regex(term: str) -> str:
  return "".join([".*" if ch == "%" else re.escape(ch) for ch in term])

def _apply_client_filters(df: pd.DataFrame, conds: Dict[str, str]) -> pd.DataFrame:
  filtered = df
  for col, raw in conds.items():
    if not raw or col not in filtered.columns:
      continue
    terms = _split_or_terms(raw)
    if not terms:
      continue
    regexes = [_wildcard_to_regex(t) for t in terms]
    pattern = "(" + "|".join(regexes) + ")"
    filtered = filtered[filtered[col].fillna("").astype(str).str.contains(pattern, flags=re.IGNORECASE, regex=True)]
  return filtered

def _with_leading_percent(s: str) -> str:
  if not s:
    return ""
  return s if s.startswith("%") else "%" + s

def _sel_len(sel) -> int:
  if sel is None:
    return 0
  if isinstance(sel, list):
    return len(sel)
  if isinstance(sel, pd.DataFrame):
    return len(sel.index)
  return 0

def _to_int_safe(v: Any, default: int = 0) -> int:
  try:
    if pd.isna(v):
      return default
    return int(v)
  except Exception:
    try:
      return int(float(v))
    except Exception:
      return default

def _http_post_json(sess: requests.Session, url: str, payload: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
  headers = {"Accept": "application/json", "Content-Type": "application/json"}
  resp = sess.post(url, json=payload, headers=headers, timeout=timeout)
  if resp.status_code != 200:
    raise requests.RequestException(f"HTTP {resp.status_code}")
  try:
    return resp.json() if resp.content else {}
  except Exception:
    return {}

# =========================
# 상태 초기화
# =========================
defaults = {
  "is_authed": False,
  "sess": None,
  "auth_cookies": {},
  "user_info": {},
  "org_info": {},
  "token_exp_utc": None,
  "base_url": "https://qf3.qfactory.biz:8000",
  "show_lot_view": False,
  "lot_df": pd.DataFrame(),
  "cart_df": pd.DataFrame(),
  "show_preview": False,
  "wh_list": pd.DataFrame(),
  "wh_selected": None,
  "alias_list": pd.DataFrame(),
  "alias_selected": None,
  "preview_df_full": pd.DataFrame(),
  "label_copies": 1,
}
for k, v in defaults.items():
  if k not in st.session_state:
    st.session_state[k] = v

# =========================
# 로그인/사이드바
# =========================
with st.sidebar:
  st.markdown("### 🔐 로그인 (1단계)")

  exp_open = not st.session_state["is_authed"]
  with st.expander("로그인 입력", expanded=exp_open):
    base_url = st.text_input("BASE_URL", value=st.session_state["base_url"])
    company_code = st.text_input("회사코드", value="BWC40601")
    user_key = st.text_input("아이디", value="")
    password = st.text_input("비밀번호", type="password", value="")
    language_code = st.selectbox("언어", options=["KO", "EN"], index=0)

  if st.session_state["is_authed"]:
    u = st.session_state.get("user_info", {})
    o = st.session_state.get("org_info", {})
    st.success(f"로그인됨 · {u.get('userKey','-')} · {o.get('orgCompanyCode','-')}/{o.get('plantCode','-')}", icon="✅")

  colA, colB = st.columns([1, 1])
  with colA:
    login_btn = st.button("로그인", use_container_width=True)
  with colB:
    reset_btn = st.button("입력 초기화", use_container_width=True)

  go_btn_sidebar = st.button(
    "변환 및 출고",
    use_container_width=True,
    disabled=not st.session_state["is_authed"],
  )
  if go_btn_sidebar and st.session_state["is_authed"]:
    st.session_state["show_lot_view"] = True
    st.session_state["collapse_sidebar"] = True
    st.rerun()

if reset_btn:
  st.rerun()

if login_btn:
  if not base_url.strip():
    st.error("BASE_URL을 입력하세요.")
  elif not company_code.strip() or not user_key.strip() or not password.strip():
    st.error("회사코드 / 아이디 / 비밀번호를 모두 입력하세요.")
  else:
    try:
      sess = requests.Session()
      url = base_url.rstrip("/") + "/common/login/post-login"
      payload = {
        "companyCode": company_code.strip(),
        "userKey": user_key.strip(),
        "password": password,
        "languageCode": language_code,
      }
      headers = {"Accept": "application/json", "Content-Type": "application/json"}
      resp = sess.post(url, json=payload, headers=headers, timeout=30)
      if resp.status_code != 200:
        st.error(f"로그인 실패: HTTP {resp.status_code}")
      else:
        data = {}
        try:
          data = resp.json()
        except Exception:
          st.error("로그인 응답이 JSON이 아닙니다.")
          data = {}

        if not data or not data.get("success"):
          err_msg = data.get("msg") if isinstance(data, dict) else None
          st.error(f"로그인 실패: {err_msg or '자격증명/서버 상태를 확인하세요.'}")
        else:
          st.session_state["sess"] = sess
          st.session_state["is_authed"] = True
          st.session_state["base_url"] = base_url.strip()
          ck = sess.cookies
          st.session_state["auth_cookies"] = {
            "token": ck.get("token") or "",
            "language_code": ck.get("language_code") or "",
            "company_code": ck.get("company_code") or "",
            "user_key": ck.get("user_key") or "",
          }
          st.session_state["user_info"] = data.get("userInfo") or {}
          st.session_state["org_info"] = data.get("orgInfo") or {}
          token_cookie = st.session_state["auth_cookies"]["token"]
          st.session_state["token_exp_utc"] = parse_jwt_exp(token_cookie) if token_cookie else None
          st.session_state["show_lot_view"] = False
          st.toast("로그인 성공", icon="✅")
          st.rerun()
    except requests.RequestException as e:
      st.error(f"네트워크 오류: {e}")

# =========================
# 공통 보조 함수 (기타출고/입고/라벨에서 사용)
# =========================
def _context_ids() -> Tuple[int, int, str, int]:
  org = st.session_state["org_info"]
  user = st.session_state["user_info"]
  return (
    org.get("orgCompanyId") or user.get("companyId") or 0,
    org.get("plantId") or user.get("plantId") or 0,
    org.get("orgCompanyCode") or user.get("companyCode") or "",
    user.get("userId") or 0,
  )

def _get_code_rule_id_for_another_acct() -> Optional[int]:
  try:
    sess: requests.Session = st.session_state["sess"]
    base_url = st.session_state["base_url"].rstrip("/")
    company_id, plant_id, company_code, user_id = _context_ids()
    url = base_url + "/system/combo/system-profile-control-value"
    payload = {
      "companyId": company_id,
      "plantId": plant_id,
      "authorityId": st.session_state["user_info"].get("authorityId") or 10033,
      "userId": user_id,
      "controlCode": "ANOTHER_ACCT_RULE",
      "companyCode": company_code,
      "languageCode": "KO",
    }
    data = _http_post_json(sess, url, payload, timeout=60)
    lst = (((data or {}).get("data") or {}).get("list")) or []
    if not lst:
      return None
    return int(lst[0].get("controlTableKeyId") or 0)
  except Exception:
    return None

def _get_account_num_by_code_rule(base_date_str: str) -> Optional[str]:
  sess: requests.Session = st.session_state["sess"]
  base_url = st.session_state["base_url"].rstrip("/")
  company_id, plant_id, company_code, user_id = _context_ids()
  code_rule_id = _get_code_rule_id_for_another_acct()
  if not code_rule_id:
    return None
  url = base_url + "/base/popup/code-rule-assign-data"
  payload = {
    "companyId": company_id,
    "plantId": plant_id,
    "codeRuleId": code_rule_id,
    "baseDate": base_date_str,
    "itemId": 0,
    "referenceTable": [],
    "referenceColumn": [],
    "referenceId": [],
    "userId": user_id,
    "checkUnusedLot": "YES",
    "companyCode": company_code,
    "languageCode": "KO",
  }
  data = _http_post_json(sess, url, payload, timeout=60)
  lst = (((data or {}).get("data") or {}).get("list")) or []
  if not lst:
    return None
  return str(lst[0].get("codeRuleAssign") or "")

# ----- 기타출고 -----
def _top_save_account_issue(header_rows: List[Dict[str, Any]]) -> Optional[int]:
  sess: requests.Session = st.session_state["sess"]
  base_url = st.session_state["base_url"].rstrip("/")
  url = base_url + "/inv/stock-etc-issue/top-save"
  payload = {
    "recordsIMain": json.dumps(header_rows, ensure_ascii=False),
    "recordsUMain": "[]",
    "recordsDMain": "[]",
    "menuTreeId": "13633",
    "languageCode": "KO",
    "companyCode": _context_ids()[2],
    "companyId": _context_ids()[0],
  }
  data = _http_post_json(sess, url, payload, timeout=90)
  try:
    return int((((data or {}).get("data") or {}).get("list")) or 0)
  except Exception:
    return None

def _fetch_lot_onhand_record(item_id: int, lot_code: str, warehouse_id: int) -> Optional[Dict[str, Any]]:
  sess: requests.Session = st.session_state["sess"]
  base_url = st.session_state["base_url"].rstrip("/")
  company_id, plant_id, company_code, _ = _context_ids()
  url = base_url + "/inv/combo/warehouse-onhand-stock-lot-list"
  payload = {
    "languageCode": "KO",
    "companyId": company_id,
    "plantId": plant_id,
    "itemId": _to_int_safe(item_id, 0),
    "lotCode": lot_code,
    "warehouseId": _to_int_safe(warehouse_id, 0),
    "locationId": 0,
    "projectId": 0,
    "effectiveStartDate": None,
    "effectiveEndDate": None,
    "page": 1,
    "limit": 200,
    "companyCode": company_code,
  }
  data = _http_post_json(sess, url, payload, timeout=60)
  lst = (((data or {}).get("data") or {}).get("list")) or []
  if not lst:
    return None
  return lst[0]

def _lot_save_issue(lot_records: List[Dict[str, Any]]) -> bool:
  sess: requests.Session = st.session_state["sess"]
  base_url = st.session_state["base_url"].rstrip("/")
  url = base_url + "/inv/stock_etc_issue/lot-save"
  payload = {
    "recordsI": json.dumps(lot_records, ensure_ascii=False),
    "recordsU": "[]",
    "recordsD": "[]",
    "menuTreeId": "13633",
    "languageCode": "KO",
    "companyCode": _context_ids()[2],
    "companyId": _context_ids()[0],
  }
  data = _http_post_json(sess, url, payload, timeout=90)
  return bool((data or {}).get("success"))

def _top_list_confirm_issue(account_num: str, item_code: str, ymd: str) -> Dict[str, Any]:
  sess: requests.Session = st.session_state["sess"]
  base_url = st.session_state["base_url"].rstrip("/")
  company_id, plant_id, *_ = _context_ids()
  url = base_url + "/inv/stock-etc-issue/top-list"
  payload = {
    "languageCode":"KO",
    "companyId": company_id,
    "plantId": plant_id,
    "transactionTypeCode":"Account_Issue",
    "accountNum": account_num or "",
    "itemCode": item_code or "",
    "itemName": "",
    "transactionDateFrom": ymd,
    "transactionDateTo": ymd,
    "itemType": "",
    "productGroup": "",
    "accountAliasCode": "",
    "warehouseCode": "",
    "warehouseName": "",
    "locationCode": "",
    "locationName": "",
    "interfaceFlag": "",
    "start": 1,
    "page": 1,
    "limit": 11,
  }
  return _http_post_json(sess, url, payload, timeout=60)

def _transfer_account_issue(account_result_ids: List[int]) -> bool:
  if not account_result_ids:
    return False
  sess: requests.Session = st.session_state["sess"]
  base_url = st.session_state["base_url"].rstrip("/")
  url = base_url + "/inv/stock-etc-issue/transfer"
  company_id, plant_id, company_code, _ = _context_ids()
  payload = {
    "companyId": company_id,
    "plantId": plant_id,
    "accountResultId": [_to_int_safe(i, 0) for i in account_result_ids],
    "languageCode": "KO",
    "companyCode": company_code,
  }
  data = _http_post_json(sess, url, payload, timeout=90)
  return bool((data or {}).get("success"))

def _issue_top_update_transaction_date(row: Dict[str, Any], new_dt: str) -> bool:
  """기타출고 top-list로 받은 row를 현재시간 new_dt로 갱신(수정 저장)"""
  try:
    sess: requests.Session = st.session_state["sess"]
    base_url = st.session_state["base_url"].rstrip("/")
    url = base_url + "/inv/stock-etc-issue/top-save"
    upd = dict(row)
    upd["editStatus"] = "U"
    upd["transactionDate"] = new_dt
    upd["row-active"] = True
    payload = {
      "recordsIMain": "[]",
      "recordsUMain": json.dumps([upd], ensure_ascii=False),
      "recordsDMain": "[]",
      "menuTreeId": "13633",
      "languageCode": "KO",
      "companyCode": _context_ids()[2],
      "companyId": _context_ids()[0],
    }
    data = _http_post_json(sess, url, payload, timeout=90)
    return bool((data or {}).get("success"))
  except Exception:
    return False

def _receipt_top_update_transaction_date(row: Dict[str, Any], new_dt: str) -> bool:
  """기타입고 top-list row에서 필수키만 뽑아 거래일자만 U 저장(안전 갱신)"""
  try:
    sess: requests.Session = st.session_state["sess"]
    base_url = st.session_state["base_url"].rstrip("/")
    url = base_url + "/inv/stock-account-receipt/top-save"

    safe = {
      "editStatus": "U",
      "row-active": True,
      # 식별/컨텍스트 최소키
      "companyId": row.get("companyId"),
      "plantId": row.get("plantId"),
      "accountResultId": row.get("accountResultId"),
      "transactionTypeId": row.get("transactionTypeId"),
      "transactionTypeCode": row.get("transactionTypeCode"),
      # 실제로 바꿀 값
      "transactionDate": new_dt,
    }

    payload = {
      "recordsIMain": "[]",
      "recordsUMain": json.dumps([safe], ensure_ascii=False),
      "recordsDMain": "[]",
      "menuTreeId": "13650",
      "languageCode": "KO",
      "companyCode": _context_ids()[2],
      "companyId": _context_ids()[0],
    }
    data = _http_post_json(sess, url, payload, timeout=90)
    return bool((data or {}).get("success"))
  except Exception:
    return False

# ----- 기타입고(저장 + 전송) -----
def _plant_item_list(q_code:str="", q_name:str="")->pd.DataFrame:
  try:
    sess=st.session_state["sess"]; base=st.session_state["base_url"].rstrip("/")
    company_id, plant_id, *_=_context_ids()
    data=_http_post_json(sess, base+"/base/combo/plant-item-list",
      {"companyId":company_id,"plantId":plant_id,"controlLotSerial":"","makeOrBuy":"",
       "status":"","itemType":"","itemCode":q_code,"itemName":q_name,"productionGroup":"",
       "productionType":"","specialaType":"","specialbType":"","specialcType":"",
       "partnerId":0,"partnerTypeId":0,"languageCode":"KO","start":1,"page":1,"limit":"20"})
    return pd.DataFrame((((data or {}).get("data") or {}).get("list")) or [])
  except:
    return pd.DataFrame()

def _receipt_top_save(header_rows:List[Dict[str,Any]])->bool:
  sess=st.session_state["sess"]; base=st.session_state["base_url"].rstrip("/")
  data=_http_post_json(sess, base+"/inv/stock-account-receipt/top-save",
    {"recordsIMain":json.dumps(header_rows, ensure_ascii=False),"recordsUMain":"[]","recordsDMain":"[]",
     "menuTreeId":"13650","languageCode":"KO","companyCode":_context_ids()[2],"companyId":_context_ids()[0]}, timeout=90)
  return bool((data or {}).get("success"))

def _receipt_top_list(ymd:str)->pd.DataFrame:
  sess=st.session_state["sess"]; base=st.session_state["base_url"].rstrip("/")
  company_id, plant_id, *_=_context_ids()
  data=_http_post_json(sess, base+"/inv/stock-account-receipt/top-list",
    {"languageCode":"KO","companyId":company_id,"plantId":plant_id,"transactionTypeCode":"",
     "accountNum":"","itemCode":"","itemName":"","transactionDateFrom":ymd,"transactionDateTo":ymd,
     "itemType":"","productGroup":"","accountAliasCode":"","warehouseCode":"","warehouseName":"",
     "locationCode":"","locationName":"","interfaceFlag":"","start":1,"page":1,"limit":999})
  return pd.DataFrame((((data or {}).get("data") or {}).get("list")) or [])

def _receipt_bottom_save(records: List[Dict[str, Any]]) -> Tuple[bool, str]:
  sess = st.session_state["sess"]; base = st.session_state["base_url"].rstrip("/")
  data = _http_post_json(
    sess, base + "/inv/stock-account-receipt/bottom-save",
    {
      "recordsI": json.dumps(records, ensure_ascii=False),
      "recordsU": "[]",
      "recordsD": "[]",
      "menuTreeId": "13650",
      "languageCode": "KO",
      "companyCode": _context_ids()[2],
      "companyId": _context_ids()[0],
    },
    timeout=90,
  )
  ok = bool((data or {}).get("success"))
  msg = (data or {}).get("msg") or ""
  return ok, msg

def _receipt_menugrid_data_cnt(account_result_id:int)->int:
  sess=st.session_state["sess"]; base=st.session_state["base_url"].rstrip("/")
  company_id, plant_id, company_code, _=_context_ids()
  data=_http_post_json(sess, base+"/inv/stock-account-receipt/menugrid-data-cnt",
    {"companyId":company_id,"plantId":plant_id,"accountResultId":int(account_result_id),
     "companyCode":company_code,"languageCode":"KO"}, timeout=60)
  lst=(((data or {}).get("data") or {}).get("list")) or []
  return int((lst[0] or {}).get("dataCnt") or 0) if lst else 0

def _receipt_bottom_transmit_proc()->bool:
  sess=st.session_state["sess"]; base=st.session_state["base_url"].rstrip("/")
  company_id, _, company_code, _=_context_ids()
  data=_http_post_json(sess, base+"/inv/stock-account-receipt/bottom-transmit-proc",
    {"recordsI":"[]","recordsU":"[]","recordsD":"[]","menuTreeId":"13650",
     "languageCode":"KO","companyCode":company_code,"companyId":company_id}, timeout=90)
  return bool((data or {}).get("success"))

def _receipt_top_transmit_proc(top_row:Dict[str,Any])->bool:
  sess=st.session_state["sess"]; base=st.session_state["base_url"].rstrip("/")
  payload = {
    "recordsIMain": json.dumps([top_row], ensure_ascii=False),
    "recordsUMain": json.dumps([top_row], ensure_ascii=False),
    "recordsDMain": "[]",
    "menuTreeId": "13650",
    "languageCode": "KO",
    "companyCode": _context_ids()[2],
    "companyId": _context_ids()[0],
  }
  data=_http_post_json(sess, base+"/inv/stock-account-receipt/top-transmit-proc", payload, timeout=90)
  return bool((data or {}).get("success"))

def _receipt_transmit(account_result_id:int)->bool:
  ymd = dt.datetime.now().strftime("%Y-%m-%d")
  tl = _receipt_top_list(ymd)
  row_df = tl[tl["accountResultId"]==int(account_result_id)]
  if row_df.empty:
    return False
  row = row_df.iloc[0].to_dict()
  row["cnt"] = _receipt_menugrid_data_cnt(account_result_id) or int(row.get("lotDataCount") or 0)
  row["row-active"] = True
  row.setdefault("id","extModel-receipt-tx")
  ok_bottom = _receipt_bottom_transmit_proc()
  if not ok_bottom:
    return False
  ok_top = _receipt_top_transmit_proc(row)
  return ok_top

# =========================
# 본문
# =========================
if not st.session_state["show_lot_view"]:
  st.title("🧪 MES 로그인 (1단계)")
  st.info("로그인이 필요합니다. 좌측 사이드바에서 자격증명을 입력하고 **로그인**을 눌러주세요.")

if st.session_state["is_authed"] and st.session_state["show_lot_view"]:

  # ── 검색조건 초기화 선처리 ──
  if st.session_state.get("do_reset_filters", False):
    for k in ("q_wh", "q_item_code", "q_item_name", "q_lot"):
      st.session_state[k] = ""
    st.session_state["q_limit"] = 500
    st.session_state["do_reset_filters"] = False
    st.rerun()

  # ── 검색 폼(Enter=조회) ──
  with st.form(key="lot_search_form", clear_on_submit=False):
    c1, c2, c3, c4 = st.columns(4)
    with c1:
      q_wh = st.text_input("창고명", value="", key="q_wh")
    with c2:
      q_item_code = st.text_input("품목코드", value="", key="q_item_code")
    with c3:
      q_item_name = st.text_input("품목명", value="", key="q_item_name")
    with c4:
      q_lot = st.text_input("LOT NO", value="", key="q_lot")

    c5 = st.columns(1)[0]
    with c5:
      if "q_limit" not in st.session_state:
        st.session_state["q_limit"] = 500
      limit = st.number_input("limit", min_value=1, max_value=5000, step=50, key="q_limit")

    submitted = st.form_submit_button("조회")

  # 폼 바깥: 검색조건 초기화
  col_reset = st.columns([1, 3])[0]
  with col_reset:
    reset_filters = st.button("검색조건 초기화", key="btn_reset_filters")
  if reset_filters:
    st.session_state["do_reset_filters"] = True
    st.rerun()

  # ── 서버 조회 ──
  need_fetch = submitted or st.session_state["lot_df"].empty
  if need_fetch:
    try:
      sess: requests.Session = st.session_state["sess"]
      base_url = st.session_state["base_url"].rstrip("/")
      url = base_url + "/inv/stock-onhand-lot/detail-list"
      payload = {
        "languageCode": "KO",
        "companyId": (st.session_state["org_info"].get("orgCompanyId")
                      or st.session_state["user_info"].get("companyId") or 0),
        "plantId": (st.session_state["org_info"].get("plantId")
                    or st.session_state["user_info"].get("plantId") or 0),
        "itemCode": _with_leading_percent(q_item_code),
        "itemName": _with_leading_percent(q_item_name),
        "itemType": "",
        "projectCode": "",
        "projectName": "",
        "productGroup": "",
        "itemClass1": "",
        "itemClass2": "",
        "warehouseCode": "",
        "warehouseName": _with_leading_percent(q_wh),
        "warehouseLocationCode": "",
        "defectiveFlag": "Y",
        "itemClass3": "",
        "itemClass4": "",
        "effectiveDateFrom": "",
        "effectiveDateTo": "",
        "creationDateFrom": "",
        "creationDateTo": "",
        "lotStatus": "",
        "lotCode": _with_leading_percent(q_lot),
        "jobName": "",
        "partnerItem": "",
        "peopleName": "",
        "start": 1,
        "page": 1,
        "limit": str(int(limit)),
      }
      with st.spinner("재고(LOT별) 조회 중..."):
        data = _http_post_json(sess, url, payload, timeout=90)
      rows = (((data or {}).get("data") or {}).get("list")) or []
      df_full = pd.DataFrame(rows)
      df_full = _apply_client_filters(df_full, {
        "warehouseName": q_wh,
        "itemCode": q_item_code,
        "itemName": q_item_name,
        "lotCode": q_lot,
      })
      st.session_state["lot_df"] = df_full
    except requests.RequestException as e:
      st.error(f"네트워크 오류: {e}")

  # ── 좌/우 레이아웃 ──
  left, right = st.columns(2)

  # 공통 설정
  preferred = ["warehouseName", "itemCode", "itemName", "lotCode", "primaryUom", "onhandQuantity"]
  header_map = {
    "warehouseName": "창고명",
    "itemCode": "품목코드",
    "itemName": "품목명",
    "lotCode": "LOT NO",
    "primaryUom": "단위",
    "onhandQuantity": "수량",
  }
  AUTO_FLEX_MIN = 90
  def _apply_flex_to_all_columns(gb_obj, cols):
    for c in cols:
      gb_obj.configure_column(c, flex=1, minWidth=AUTO_FLEX_MIN)

  js_rowid = JsCode("""
    function(params){
      const r = params.data || {};
      return String(r.lotCode || '') + '|' + String(r.itemCode || '');
    }
  """)

  # ---------- 왼쪽: 재고조회(LOT별) ----------
  with left:
    c_title, c_btn = st.columns([4, 1])
    with c_title:
      st.markdown("### 📦 재고조회(LOT별)")
    with c_btn:
      btn_add = st.button("담기", use_container_width=True, key="btn_add")

    df_left_src = st.session_state["lot_df"].copy()
    display_cols_left = [c for c in preferred if c in df_left_src.columns] + \
                        [c for c in df_left_src.columns if c not in preferred]
    df_left_display = df_left_src[display_cols_left] if not df_left_src.empty else pd.DataFrame(columns=preferred)

    gb = GridOptionsBuilder.from_dataframe(df_left_display)
    gb.configure_selection("multiple", use_checkbox=True)
    gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=50)
    gb.configure_grid_options(
      rowSelection="multiple",
      rowMultiSelectWithClick=True,
      suppressRowClickSelection=False,
      suppressRowDeselection=False,
      suppressHorizontalScroll=True,
      rememberSelected=True,
      getRowId=js_rowid,
    )
    for field, header in header_map.items():
      if field in df_left_display.columns:
        gb.configure_column(field, header_name=header)
    _apply_flex_to_all_columns(gb, list(df_left_display.columns))

    grid_left = AgGrid(
      df_left_display,
      gridOptions=gb.build(),
      height=600,
      theme="dark",
      update_mode=GridUpdateMode.SELECTION_CHANGED,
      data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
      allow_unsafe_jscode=True,
      fit_columns_on_grid_load=False,
      key="grid_left_main",
    )
    selected_left = grid_left.get("selected_rows", [])
    if _sel_len(selected_left) > 0:
      st.session_state["left_selection"] = (
        selected_left.to_dict("records") if isinstance(selected_left, pd.DataFrame) else selected_left
      )

  # ---------- 오른쪽: 카트 ----------
  with right:
    c_title2, c_btn2, c_btn3 = st.columns([4, 1, 1])
    with c_title2:
      st.markdown("### 🛒 카트")
    with c_btn2:
      btn_del = st.button("삭제", use_container_width=True)
    with c_btn3:
      btn_convert = st.button("3공장 품목변환", use_container_width=True)

    cart_df_full = st.session_state["cart_df"].copy()
    display_cols_right = [c for c in preferred if c in cart_df_full.columns] + \
                         [c for c in cart_df_full.columns if c not in preferred]
    cart_display = cart_df_full[display_cols_right] if not cart_df_full.empty else pd.DataFrame(columns=preferred)

    gb2 = GridOptionsBuilder.from_dataframe(cart_display)
    gb2.configure_selection("multiple", use_checkbox=True)
    gb2.configure_pagination(paginationAutoPageSize=False, paginationPageSize=200)
    gb2.configure_grid_options(
      rowSelection="multiple",
      rowMultiSelectWithClick=True,
      suppressRowClickSelection=False,
      suppressRowDeselection=False,
      suppressHorizontalScroll=True,
      getRowId=js_rowid,
    )
    for field, header in header_map.items():
      if field in cart_display.columns:
        gb2.configure_column(field, header_name=header)
    _apply_flex_to_all_columns(gb2, list(cart_display.columns))

    st.session_state.setdefault("grid_right_nonce", 0)
    grid_right = AgGrid(
      cart_display,
      gridOptions=gb2.build(),
      height=600,
      theme="dark",
      update_mode=GridUpdateMode.SELECTION_CHANGED,
      data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
      allow_unsafe_jscode=True,
      fit_columns_on_grid_load=False,
      key=f"grid_right_cart_{st.session_state['grid_right_nonce']}",
    )
    selected_cart = grid_right.get("selected_rows", [])
    if _sel_len(selected_cart) > 0:
      st.session_state["right_selection"] = (
        selected_cart.to_dict("records") if isinstance(selected_cart, pd.DataFrame) else selected_cart
      )

  # ---------- 버튼 동작 ----------
  if btn_add:
    if _sel_len(selected_left) > 0:
      sel_df_view = (selected_left.copy() if isinstance(selected_left, pd.DataFrame) else pd.DataFrame(selected_left))
    else:
      backup_rows = st.session_state.get("left_selection", [])
      sel_df_view = (backup_rows.copy() if isinstance(backup_rows, pd.DataFrame) else pd.DataFrame(backup_rows))

    if sel_df_view.empty or not {"lotCode", "itemCode"} <= set(sel_df_view.columns):
      st.warning("선택된 행이 없습니다.", icon="⚠️")
    else:
      keys: Set[Tuple[str, str]] = set(
        zip(sel_df_view["lotCode"].astype(str), sel_df_view["itemCode"].astype(str))
      )
      full_df = st.session_state["lot_df"].copy()
      full_df["_key"] = list(zip(full_df["lotCode"].astype(str), full_df["itemCode"].astype(str)))
      add_full = full_df[full_df["_key"].isin(keys)].drop(columns=["_key"])
      merged = (pd.concat([st.session_state["cart_df"], add_full], ignore_index=True)
                if not st.session_state["cart_df"].empty else add_full)
      if {"lotCode", "itemCode"} <= set(merged.columns):
        merged = merged.drop_duplicates(subset=["lotCode", "itemCode"], keep="first")

      st.session_state["cart_df"] = merged
      st.session_state["grid_right_nonce"] += 1
      st.toast(f"{len(add_full)}건 담았습니다.", icon="🧺")
      st.rerun()

  if 'btn_del' in locals() and btn_del:
    cur_sel = grid_right.get("selected_rows", [])
    if _sel_len(cur_sel) == 0:
      cur_sel = st.session_state.get("right_selection", [])
    if _sel_len(cur_sel) == 0:
      st.warning("선택된 행이 없습니다.", icon="⚠️")
    else:
      sel_df = (cur_sel.copy() if isinstance(cur_sel, pd.DataFrame) else pd.DataFrame(cur_sel))
      remain = st.session_state["cart_df"].copy()
      if not sel_df.empty and "lotCode" in remain.columns and "itemCode" in remain.columns:
        keys_del = set(zip(sel_df["lotCode"].astype(str), sel_df["itemCode"].astype(str)))
        mask = ~remain.apply(lambda r: (str(r.get("lotCode")), str(r.get("itemCode"))) in keys_del, axis=1)
        remain = remain[mask]
      st.session_state["cart_df"] = remain
      st.session_state["grid_right_nonce"] += 1
      st.toast(f"{len(sel_df)}건 삭제했습니다.", icon="🗑️")
      st.rerun()

  # ---------- 품목변환: 보조 조회 함수 ----------
  def _fetch_warehouse_list() -> pd.DataFrame:
    try:
      sess: requests.Session = st.session_state["sess"]
      base_url = st.session_state["base_url"].rstrip("/")
      url = base_url + "/inv/warehouse/list"
      payload = {
        "languageCode":"KO",
        "companyId": (st.session_state["org_info"].get("orgCompanyId")
                      or st.session_state["user_info"].get("companyId") or 0),
        "plantId": (st.session_state["org_info"].get("plantId")
                    or st.session_state["user_info"].get("plantId") or 0),
        "enabledFlag":"","warehouseCode":"","warehouseName":"","warehouseType":"",
        "outsideFlag":"","partnerCode":"","partnerName":"","availableForLocationFlag":"",
        "poReceivingFlag":"","wipProductionFlag":"","shipmentInspectionFlag":"",
        "defectiveStockFlag":"","wipProcessingFlag":"","managementType":"",
        "inventoryAssetFlag":"","start":1,"page":1,"limit":25
      }
      data = _http_post_json(sess, url, payload, timeout=60)
      rows = (((data or {}).get("data") or {}).get("list")) or []
      return pd.DataFrame(rows)
    except requests.RequestException as e:
      st.error(f"창고 목록 조회 실패: {e}")
      return pd.DataFrame()

  def _fetch_item_code_by_name(after_item_name: str) -> str:
    if not after_item_name:
      return ""
    try:
      sess: requests.Session = st.session_state["sess"]
      base_url = st.session_state["base_url"].rstrip("/")
      url = base_url + "/base/item/list"
      payload = {
        "languageCode":"KO",
        "companyId": (st.session_state["org_info"].get("orgCompanyId")
                      or st.session_state["user_info"].get("companyId") or 0),
        "status":"Active",
        "itemPlant": (st.session_state["org_info"].get("plantId")
                      or st.session_state["user_info"].get("plantId") or 0),
        "itemCode":"",
        "itemName": after_item_name,
        "itemType":"",
        "productGroup":"",
        "buyMake":"",
        "controlLot":"",
        "start":1,"page":1,"limit":25
      }
      data = _http_post_json(sess, url, payload, timeout=60)
      lst = (((data or {}).get("data") or {}).get("list")) or []
      if not lst:
        return ""
      return str(lst[0].get("itemCode") or "")
    except requests.RequestException as e:
      st.error(f"품목정보 조회 실패: {e}")
      return ""

  def _fetch_account_alias_list() -> pd.DataFrame:
    try:
      sess: requests.Session = st.session_state["sess"]
      base_url = st.session_state["base_url"].rstrip("/")
      url = base_url + "/inv/account-alias/list"
      payload = {
        "languageCode":"KO",
        "companyId": (st.session_state["org_info"].get("orgCompanyId")
                      or st.session_state["user_info"].get("companyId") or 0),
        "plantId": (st.session_state["org_info"].get("plantId")
                    or st.session_state["user_info"].get("plantId") or 0),
        "enabledFlag":"",
        "accountAliasCode":"",
        "accountAliasName":"",
        "start":1,"page":1,"limit":25
      }
      data = _http_post_json(sess, url, payload, timeout=60)
      rows = (((data or {}).get("data") or {}).get("list")) or []
      return pd.DataFrame(rows)
    except requests.RequestException as e:
      st.error(f"기타(입/출) 코드 조회 실패: {e}")
      return pd.DataFrame()

  if 'btn_convert' in locals() and btn_convert:
    if st.session_state["wh_list"].empty:
      st.session_state["wh_list"] = _fetch_warehouse_list()
    if st.session_state["alias_list"].empty:
      st.session_state["alias_list"] = _fetch_account_alias_list()
    st.session_state["show_preview"] = True
    st.session_state["rebuild_preview"] = True  # ← 카트 기준으로 미리보기 재생성 플래그
    st.session_state["show_lot_change"] = False  # LOT 편집 패널 초기화
    st.session_state["lot_edit_inputs"] = {}     # LOT 편집 입력 초기화

  # ---------- 변환 미리보기 ----------
  if st.session_state["show_preview"]:
    st.markdown("---")
    st.markdown("#### 🔄 변환 미리보기")

    if st.session_state["wh_list"].empty:
      st.session_state["wh_list"] = _fetch_warehouse_list()
    if st.session_state["alias_list"].empty:
      st.session_state["alias_list"] = _fetch_account_alias_list()

    wh_df = st.session_state["wh_list"]
    alias_df = st.session_state["alias_list"]

    wh_names = wh_df["warehouseName"].astype(str).tolist() if "warehouseName" in wh_df.columns else []
    alias_names = alias_df["accountAliasName"].astype(str).tolist() if "accountAliasName" in alias_df.columns else []

    DEFAULT_WH_NAME = "출하대기 창고"
    DEFAULT_ALIAS_NAME = "품목코드 변환"

    col_wh, col_alias, col_copies = st.columns([3, 2, 1])

    with col_wh:
      sel_idx_wh = None
      if st.session_state["wh_selected"] is not None and "warehouseName" in st.session_state["wh_selected"]:
        try:
          sel_idx_wh = wh_names.index(str(st.session_state["wh_selected"]["warehouseName"]))
        except Exception:
          sel_idx_wh = None
      if sel_idx_wh is None:
        sel_idx_wh = wh_names.index(DEFAULT_WH_NAME) if DEFAULT_WH_NAME in wh_names else 0
      selected_wh_name = st.selectbox("after warehouseName 선택", options=wh_names, index=sel_idx_wh if wh_names else 0, key="after_wh_select")
      try:
        st.session_state["wh_selected"] = wh_df.iloc[wh_names.index(selected_wh_name)].to_dict()
      except Exception:
        st.session_state["wh_selected"] = None

    with col_alias:
      sel_idx_alias = None
      if st.session_state["alias_selected"] is not None and "accountAliasName" in st.session_state["alias_selected"]:
        try:
          sel_idx_alias = alias_names.index(str(st.session_state["alias_selected"]["accountAliasName"]))
        except Exception:
          sel_idx_alias = None
      if sel_idx_alias is None:
        sel_idx_alias = alias_names.index(DEFAULT_ALIAS_NAME) if DEFAULT_ALIAS_NAME in alias_names else 0
      selected_alias_name = st.selectbox("기타(입/출) 코드 선택", options=alias_names, index=sel_idx_alias if alias_names else 0, key="after_alias_select")
      try:
        st.session_state["alias_selected"] = alias_df.iloc[alias_names.index(selected_alias_name)].to_dict()
      except Exception:
        st.session_state["alias_selected"] = None

    with col_copies:
      st.session_state["label_copies"] = st.number_input("라벨 매수(LOT당)", min_value=1, max_value=50, value=st.session_state["label_copies"], step=1)

    # ── 변환 미리보기 소스 준비 ──
    # 'rebuild_preview'가 True면 카트 내용으로 미리보기를 다시 생성(상위 작업)
    force_rebuild = bool(st.session_state.get("rebuild_preview"))

    if force_rebuild:
      src = st.session_state["cart_df"].copy()
      # 카트에 우연히 섞여 있을 수 있는 과거 after/alias 컬럼은 제거 후 깨끗하게 재생성
      drop_cols = [c for c in src.columns if c.startswith("_after_") or c.startswith("_alias_")]
      if drop_cols:
        src = src.drop(columns=drop_cols, errors="ignore")
    else:
      # 기존 미리보기가 있으면 우선 사용(사용자 LOT 수동변경 유지)
      if not st.session_state["preview_df_full"].empty:
        src = st.session_state["preview_df_full"].copy()
      else:
        src = st.session_state["cart_df"].copy()

    if src.empty:
      st.info("카트가 비어 있습니다. 왼쪽에서 행을 선택하고 [담기]를 눌러주세요.")
    else:
      # after 컬럼이 없을 때만 최초 생성 (이미 있으면 유지)
      if "_after_itemCode" not in src.columns:
        src["_after_itemName"] = "(완)" + src["itemName"].astype(str)

        unique_after_names = sorted(src["_after_itemName"].dropna().astype(str).unique())
        name_to_code: Dict[str, str] = {}
        for nm in unique_after_names:
          code = _fetch_item_code_by_name(nm)
          name_to_code[nm] = code
        src["_after_itemCode"] = src["_after_itemName"].map(name_to_code).fillna("")

        def _rebuild_lot(old_lot: Any, new_code: str) -> str:
          s = str(old_lot or "")
          nc = (new_code or "")[:7]
          if len(s) >= 7 and len(nc) == 7:
            return nc + s[7:]
          return s

        src["_after_lotCode"] = [
          _rebuild_lot(ol, ac) for ol, ac in zip(src["lotCode"].astype(str), src["_after_itemCode"].astype(str))
        ]

        after_wh_name = str(st.session_state["wh_selected"].get("warehouseName")) if st.session_state["wh_selected"] else ""
        src["_after_warehouseName"] = after_wh_name
        src["_after_primaryUom"] = src["primaryUom"].astype(str)

        _orig_qty = pd.to_numeric(src["onhandQuantity"], errors="coerce")
        src["onhandQuantity"] = _orig_qty.abs() * (-1)
        src["_after_onhandQuantity"] = _orig_qty.abs()

        if st.session_state["alias_selected"] is not None:
          for k, v in st.session_state["alias_selected"].items():
            src[f"_alias_{k}"] = v

      # ▼ 항상 최신 상태를 세션에 반영(LOT 변경 유지)
      st.session_state["preview_df_full"] = src.copy()
      st.session_state["rebuild_preview"] = False  # ← 재빌드 플래그 해제
      
      show_cols = [
        "warehouseName","itemCode","itemName","lotCode","primaryUom","onhandQuantity",
        "_after_warehouseName","_after_itemCode","_after_itemName","_after_lotCode","_after_primaryUom","_after_onhandQuantity"
      ]
      header_map_preview = {
        "warehouseName":"warehouseName",
        "itemCode":"itemCode",
        "itemName":"itemName",
        "lotCode":"lotCode",
        "primaryUom":"PrimaryUom",
        "onhandQuantity":"onhandQuantity",
        "_after_warehouseName":"after warehouseName",
        "_after_itemCode":"after itemCode",
        "_after_itemName":"after item Name",
        "_after_lotCode":"after lotCode",
        "_after_primaryUom":"after PrimaryUom",
        "_after_onhandQuantity":"after onhandQuantity",
      }
      disp = src[[c for c in show_cols if c in src.columns]].copy()
      gbp = GridOptionsBuilder.from_dataframe(disp)
      gbp.configure_selection("multiple", use_checkbox=False)
      gbp.configure_pagination(paginationAutoPageSize=False, paginationPageSize=200)
      gbp.configure_grid_options(
        rowSelection="single",
        suppressHorizontalScroll=True,
        getRowId=js_rowid,
      )
      for field, header in header_map_preview.items():
        if field in disp.columns:
          gbp.configure_column(field, header_name=header)
      for c in disp.columns:
        gbp.configure_column(c, flex=1, minWidth=90)
      AgGrid(
        disp,
        gridOptions=gbp.build(),
        height=380,
        theme="dark",
        update_mode=GridUpdateMode.NO_UPDATE,
        data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
        allow_unsafe_jscode=True,
        fit_columns_on_grid_load=False,
        key="grid_preview",
      )

      # ========= LOT 변경 버튼 =========
      btn_lot_change = st.button("LOT 변경", key="btn_lot_change")  # ← 너비/높이는 네가 조정

      # 버튼 클릭 시 편집용 기본값 구성 (_after_itemCode 그룹당 1개)
      if btn_lot_change:
        st.session_state["show_lot_change"] = True
        df_src = st.session_state["preview_df_full"].copy()
        inputs = {}
        def _pick_ymd(lot_str: str) -> str:
          m = re.search(r"-[A-Za-z0-9]{2}-(\d{6})\d{3}$", str(lot_str or ""))
          return m.group(1) if m else ""
        for code, g in df_src.groupby("_after_itemCode", dropna=False):
          code_s = str(code or "")
          if not code_s:
            continue
          name_s = str(g["_after_itemName"].iloc[0]) if "_after_itemName" in g.columns and not g.empty else ""
          # 그룹 내 가장 최근(문자 비교 max) YYMMDD 기본값 (컬럼 직접 접근)
          yy = [ _pick_ymd(x) for x in g["_after_lotCode"].astype(str).tolist() if x ]
          default_ymd = max(yy) if yy else dt.datetime.now().strftime("%y%m%d")
          inputs[code_s] = {"name": name_s, "ymd": default_ymd}
        st.session_state["lot_edit_inputs"] = inputs

      # 편집 패널 표시
      if st.session_state.get("show_lot_change"):
        st.markdown("##### LOT 변경")
        with st.form("lot_change_form", clear_on_submit=False):
          # 헤더
          h1, h2, h3 = st.columns([1.5, 3, 1])
          with h1: st.markdown("**_after_itemCode**")
          with h2: st.markdown("**_after_itemName**")   # 오타 수정
          with h3: st.markdown("**YYMMDD**")

          # 코드 · 이름 · YYMMDD 입력 (그룹당 1줄)
          for code_s, info in (st.session_state.get("lot_edit_inputs") or {}).items():
            c1, c2, c3 = st.columns([1.5, 3, 1])
            with c1: st.write(code_s)                 # _after_itemCode
            with c2: st.write(info.get("name",""))    # _after_itemName
            with c3:
              key_txt = f"ymd_{code_s}"
              default_val = info.get("ymd","")
              st.text_input("YYMMDD", value=default_val, key=key_txt, label_visibility="collapsed")
          apply_lot_btn = st.form_submit_button("적용")

        # 적용 로직: 그룹별로 YYMMDD 반영, 뒤 3자리 100부터 순번
        if apply_lot_btn:
          df_apply = st.session_state["preview_df_full"].copy()

          def _rebuild_lot(old_lot: str, code7: str, wc2: str, ymd: str, seq: int) -> str:
            return f"{code7}-{wc2}-{ymd}{seq:03d}"

          total_updates = 0
          for code_s in (st.session_state.get("lot_edit_inputs") or {}):
            ymd_in = str(st.session_state.get(f"ymd_{code_s}", "")).strip()
            if not re.fullmatch(r"\d{6}", ymd_in):
              continue  # YYMMDD 형식 아닐 때는 건너뜀

            mask = df_apply["_after_itemCode"].astype(str) == code_s
            idxs = list(df_apply[mask].index)

            for i, idx in enumerate(idxs):
              old = str(df_apply.at[idx, "_after_lotCode"] or "")
              m = re.match(r"^([^-]{7})-([^-]{2})-\d{6}\d{3}$", old)
              code7 = (m.group(1) if m else str(code_s)[:7]).ljust(7)[:7]
              wc2   = (m.group(2) if m else "C1").ljust(2)[:2]
              df_apply.at[idx, "_after_lotCode"] = _rebuild_lot(old, code7, wc2, ymd_in, 100 + i)
              total_updates += 1

          # ▼ 표 갱신 강제
          st.session_state["preview_df_full"] = df_apply
          # (표는 위에서 'src -> disp'로 그려졌기 때문에) 강제 재생성 트리거
          st.session_state["grid_right_nonce"] = st.session_state.get("grid_right_nonce", 0) + 1
          st.toast(f"LOT 변경 적용: {total_updates}건 · YYMMDD 반영 및 100부터 순번 부여", icon="✏️")
          st.rerun()

      # =========================
      # 저장/불러오기 (변환 미리보기 전용) — 가로 정렬
      # =========================
      c_sv1, c_sv2, c_sv3 = st.columns([1, 3, 1])
      
      with c_sv1:
        _save_payload = {
          "schema_version": 1,
          "saved_at": dt.datetime.now().isoformat(),
          "preview_df_full": st.session_state["preview_df_full"].to_dict(orient="records"),
          "wh_selected": st.session_state.get("wh_selected"),
          "alias_selected": st.session_state.get("alias_selected"),
          "label_copies": int(st.session_state.get("label_copies", 1)),
          "org_info": st.session_state.get("org_info", {}),
          "user_info": st.session_state.get("user_info", {}),
        }
        st.download_button(
          "💾 저장(.json)",
          data=json.dumps(_save_payload, ensure_ascii=False, indent=2).encode("utf-8"),
          file_name=f"preview_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
          mime="application/json",
          use_container_width=True,
          key="btn_preview_save",
        )
      with c_sv2:
        _uploaded = st.file_uploader("📂 불러오기(.json)", type=["json"], key="preview_import_file")
      with c_sv3:
        _apply = st.button("적용", use_container_width=True, key="btn_preview_apply")

      # 불러오기 적용
      if _apply and _uploaded is not None:
        try:
          _payload = json.load(_uploaded)
          _rows = _payload.get("preview_df_full", [])
          _df_new = pd.DataFrame(_rows)

          # 숫자 컬럼 1차 정규화
          _num_cols = ["itemId","warehouseId","accountResultId","onhandQuantity","secondaryQuantity","_after_onhandQuantity"]
          for _c in _num_cols:
            if _c in _df_new.columns:
              _df_new[_c] = pd.to_numeric(_df_new[_c], errors="coerce").fillna(0)

          # 필수 컬럼 최소 집합
          _need_cols = [
            "itemCode","warehouseName","lotCode","primaryUom",
            "_after_itemCode","_after_itemName","_after_lotCode","_after_primaryUom","_after_onhandQuantity"
          ]
          _missing_basic = [c for c in _need_cols if c not in _df_new.columns]
          if _missing_basic:
            st.error(f"불러오기 실패: 필수 컬럼 누락 {_missing_basic}", icon="❌")
            st.stop()

          # 누락/NaN 보정: secondaryUom, warehouseCode, itemId, warehouseId
          if "secondaryUom" not in _df_new.columns:
            _df_new["secondaryUom"] = _df_new["primaryUom"]
          else:
            _df_new["secondaryUom"] = _df_new["secondaryUom"].fillna(_df_new["primaryUom"])

          if "warehouseCode" not in _df_new.columns:
            _df_new["warehouseCode"] = ""

          # 창고 ID/코드 보정: wh_list 이용
          if st.session_state["wh_list"].empty:
            st.session_state["wh_list"] = _fetch_warehouse_list()
          _wh = st.session_state["wh_list"].copy()
          _wh_idx = {}
          if not _wh.empty:
            for _, r in _wh.iterrows():
              _wh_idx[str(r.get("warehouseName") or "")] = {
                "warehouseId": _to_int_safe(r.get("warehouseId"), 0),
                "warehouseCode": str(r.get("warehouseCode") or "")
              }

          def _fix_wh(row):
            nm = str(row.get("warehouseName") or "")
            info = _wh_idx.get(nm, None)
            wid = row.get("warehouseId", None)
            wcd = row.get("warehouseCode", "")
            if info:
              if pd.isna(wid) or _to_int_safe(wid, 0) == 0:
                row["warehouseId"] = info["warehouseId"]
              if not wcd:
                row["warehouseCode"] = info["warehouseCode"]
            row["warehouseId"] = _to_int_safe(row.get("warehouseId"), 0)
            return row

          _df_new = _df_new.apply(_fix_wh, axis=1)

          # itemId 보정: itemCode 기반 조회
          def _fix_item(row):
            iid = _to_int_safe(row.get("itemId"), 0)
            if iid == 0:
              icode = str(row.get("itemCode") or "")
              if icode:
                pl = _plant_item_list(q_code=icode)
                if not pl.empty:
                  iid = _to_int_safe(pl.iloc[0].get("itemId"), 0)
                  if pd.isna(row.get("primaryUom")) or not str(row.get("primaryUom")):
                    row["primaryUom"] = str(pl.iloc[0].get("primaryUom") or "")
                  if pd.isna(row.get("secondaryUom")) or not str(row.get("secondaryUom")):
                    row["secondaryUom"] = str(pl.iloc[0].get("secondaryUom") or row.get("primaryUom") or "")
            row["itemId"] = iid
            return row

          _df_new = _df_new.apply(_fix_item, axis=1)

          # 출고 필수 확장 컬럼 보정
          for col in ["warehouseId","warehouseCode","warehouseName","primaryUom","secondaryUom","itemId","itemCode","lotCode"]:
            if col not in _df_new.columns:
              _df_new[col] = "" if col.endswith("Code") or col.endswith("Name") else 0

          # onhandQuantity가 비어있으면 음수로 채움 (미리보기 규칙)
          if "onhandQuantity" not in _df_new.columns:
            _df_new["onhandQuantity"] = -pd.to_numeric(_df_new["_after_onhandQuantity"], errors="coerce").fillna(0)
          else:
            _df_new["onhandQuantity"] = -pd.to_numeric(_df_new["onhandQuantity"], errors="coerce").fillna(
              pd.to_numeric(_df_new["_after_onhandQuantity"], errors="coerce").fillna(0)
            ).abs()

          st.session_state["preview_df_full"] = _df_new
          st.session_state["wh_selected"] = _payload.get("wh_selected") or st.session_state.get("wh_selected")
          st.session_state["alias_selected"] = _payload.get("alias_selected") or st.session_state.get("alias_selected")
          st.session_state["label_copies"] = int(_payload.get("label_copies") or st.session_state.get("label_copies", 1))

          # 카트 표시용 기본컬럼 갱신
          _base_cols = ["warehouseName","itemCode","itemName","lotCode","primaryUom","onhandQuantity"]
          _exist = [c for c in _base_cols if c in _df_new.columns]
          if _exist:
            st.session_state["cart_df"] = _df_new[_exist].copy()

          st.toast("불러오기 완료", icon="📥")
          st.rerun()
        except Exception as _e:
          st.error(f"불러오기 예외: {_e}")

      # =========================
      # 하단 같은 줄: [🧾 기타출고] | [🏷️ 라벨출력] | [📥 기타입고]
      # =========================
      c_left, c_mid, c_right = st.columns(3)
      exec_issue_btn = c_left.button("🧾 기타출고", use_container_width=True)
      exec_label_btn = c_mid.button("🏷️ 라벨출력", use_container_width=True)
      exec_receipt_btn = c_right.button("📥 기타입고", use_container_width=True)

      # ---------- 기타출고 ----------
      if exec_issue_btn:
        src_full = st.session_state["preview_df_full"].copy()
        if src_full.empty:
          st.warning("미리보기/카트가 비어 있습니다.", icon="⚠️")
          st.stop()
        try:
          sess: requests.Session = st.session_state["sess"]
          if sess is None:
            st.error("세션이 없습니다. 다시 로그인하세요.")
            st.stop()

          _freeze = dt.datetime.now()
          _tx_now = _freeze + dt.timedelta(hours=9)  # 서버(UTC) 보정
          tx_dt  = _tx_now.strftime("%Y-%m-%d %H:%M:%S")  # 버튼 시각(보정) - DATETIME
          tx_ymd = _tx_now.strftime("%Y-%m-%d")          # 버튼 시각(보정) - DATE
          base_date_str = tx_ymd

          # 그룹 키 누락 보정
          grp_cols = ["itemId","itemCode","warehouseId","warehouseCode","warehouseName","primaryUom","secondaryUom"]
          for col in grp_cols:
            if col not in src_full.columns:
              src_full[col] = None

          grouped = src_full.groupby(grp_cols, dropna=False)

          all_results = []
          created_ids: List[int] = []
          alias = st.session_state["alias_selected"] or {}
          account_alias_id = _to_int_safe(alias.get("accountAliasId"), 10038)
          account_alias_code = str(alias.get("accountAliasCode") or "")          # <-- 코드
          account_alias_name = str(alias.get("accountAliasName") or "품목코드 변환")  # <-- 이름

          company_id, plant_id, company_code, _ = _context_ids()

          for (item_id, item_code, wh_id, wh_code, wh_name, p_uom, s_uom), gdf in grouped:
            account_num = _get_account_num_by_code_rule(base_date_str)
            if not account_num:
              st.error("계정번호 채번 실패(code-rule-assign-data)."); st.stop()

            qty_abs_sum = float(pd.to_numeric(gdf["_after_onhandQuantity"], errors="coerce").fillna(0).sum())
            sec_abs_sum = float(pd.to_numeric(gdf.get("secondaryQuantity", pd.Series([0]*len(gdf))), errors="coerce").fillna(0).sum())
            lot_count = int(len(gdf.index))

            header_rows = [{
              "editStatus":"I","companyId": company_id,"plantId": plant_id,"accountNum": account_num,
              "transactionTypeId": 10079,"transactionTypeCode":"Account_Issue","transactionTypeName":"기타출고",
              "accountAliasId": account_alias_id,"accountAliasCode": account_alias_code,"accountAliasName": account_alias_name,
              "warehouseId": _to_int_safe(wh_id, 0),"warehouseCode": str(wh_code or ""), "warehouseName": str(wh_name or ""),
              "locationId": 0,"locationCode": None,"locationName": None,
              "transactionDate": tx_dt,
              "accountResultId": 0,"lotCount": lot_count,
              "primaryQuantity": qty_abs_sum,"secondaryQuantity": sec_abs_sum,
              "projectId": 0,"effectiveStartDate": None,"effectiveEndDate": None,
              "approvalFlag":"Y","interfaceFlag":"N","workStatus":"I",
              "id":"extModel-streamlit","row-active": True,
              "itemCode": str(item_code or ""), "itemId": _to_int_safe(item_id, 0),
              "itemName": str(gdf.iloc[0].get("itemName") or ""),
              "controlLotSerial":"LOT","primaryUom": str(p_uom or ""), "secondaryUom": str(s_uom or (p_uom or "")),
              "effectivePeriodOfDay": 0,"effectivePeriodOfDayFlag":"N","errorField": {}
            }]

            with st.spinner(f"① 기타출고 헤더 저장(top-save) 중... [{item_code}/{wh_name}]"):
              account_result_id = _top_save_account_issue(header_rows)
            if not account_result_id:
              st.error("top-save 실패"); st.stop()
            created_ids.append(int(account_result_id))

            lot_records: List[Dict[str,Any]] = []
            with st.spinner(f"② LOT 상세조회/저장 준비 중... [{item_code}/{wh_name}]"):
              for _, r in gdf.iterrows():
                it_id = _to_int_safe(r.get("itemId"), 0)
                lot_code = str(r.get("lotCode") or "")
                src_wh_id = _to_int_safe(r.get("warehouseId"), 0)
                rec = _fetch_lot_onhand_record(it_id, lot_code, src_wh_id)
                if not rec:
                  st.error(f"LOT 상세조회 실패: {lot_code}"); st.stop()
                rec = dict(rec); rec["accountResultId"] = int(account_result_id); rec["interfaceFlag"] = "N"
                lot_records.append(rec)

            with st.spinner(f"③ LOT 저장(lot-save) 중... [{item_code}/{wh_name}]"):
              ok = _lot_save_issue(lot_records)
            if not ok:
              st.error("lot-save 실패"); st.stop()

            with st.spinner(f"④ 저장내용 검증(top-list) 중... [{item_code}/{wh_name}]"):
              confirm = _top_list_confirm_issue(account_num, str(item_code or ""), tx_ymd)
            lst = (((confirm or {}).get("data") or {}).get("list")) or []
            if lst:
              row = dict(lst[0])
              # ▼ 버튼 시각(보정)으로 거래일자 강제 갱신(수정 저장)
              _ = _issue_top_update_transaction_date(row, tx_dt)
              all_results.append({
                "accountNum": row.get("accountNum"),
                "itemCode": row.get("itemCode"),
                "itemName": row.get("itemName"),
                "warehouseName": wh_name,
                "lotCount": row.get("lotCount"),
                "primaryQuantity": row.get("primaryQuantity"),
                "secondaryQuantity": row.get("secondaryQuantity"),
                "accountResultId": _to_int_safe(row.get("accountResultId"), account_result_id),
              })
            else:
              all_results.append({
                "accountNum": account_num, "itemCode": str(item_code or ""),
                "itemName": str(gdf.iloc[0].get("itemName") or ""), "warehouseName": wh_name,
                "lotCount": lot_count, "primaryQuantity": -qty_abs_sum, "secondaryQuantity": -sec_abs_sum,
                "accountResultId": int(account_result_id),
              })

          with st.spinner("⑤ 인터페이스 처리(transfer) 중..."):
            ok_transfer = _transfer_account_issue([r["accountResultId"] for r in all_results])
          if not ok_transfer:
            st.error("transfer 실패"); st.stop()

          st.success("✅ 기타출고 + 인터페이스(transfer) 완료")
          for r in all_results:
            st.write(f"- 전표: **{r['accountNum']}** / 창고: **{r['warehouseName']}** / {r['itemCode']} ({r['itemName']}) / LOT:{r['lotCount']} / 기본:{r['primaryQuantity']} · 2차:{r['secondaryQuantity']} / accountResultId:{r['accountResultId']}")

        except Exception as ex:
          st.error(f"예외 발생: {ex}")

      # ---------- 🏷️ 라벨출력 : 클라이언트 PDF-417 + 팝업 인쇄 ----------
      if exec_label_btn:
        after_df = st.session_state["preview_df_full"].copy()
        if after_df.empty:
          st.warning("미리보기/카트가 비어 있습니다.", icon="⚠️")
          st.stop()
        try:
          copies = int(st.session_state.get("label_copies", 1) or 1)

          # after 품목코드별로 품목 API 호출하여 specialbType / color 확보
          unique_codes = sorted(after_df["_after_itemCode"].dropna().astype(str).unique())
          code_to_extra: Dict[str, Dict[str, Any]] = {}
          for code in unique_codes:
            info = _plant_item_list(q_code=code)
            if info.empty:
              code_to_extra[code] = {"specialbType":"", "color":""}
            else:
              row = info.iloc[0].to_dict()
              code_to_extra[code] = {
                "specialbType": str(row.get("specialbType") or ""),
                "color": str(row.get("color") or row.get("colorName") or ""),
              }

          # 라벨 1장 HTML
          def _label_html(row: Dict[str, Any], barcode_data_url: str) -> str:
            aft_code = str(row.get("_after_itemCode") or "")
            aft_name = str(row.get("_after_itemName") or "")
            lot_code = str(row.get("_after_lotCode") or "")
            qty = str(int(round(float(pd.to_numeric(row.get("_after_onhandQuantity"), errors="coerce") or 0))))
            aft_uom = str(row.get("_after_primaryUom") or "")
            extra = code_to_extra.get(aft_code, {"specialbType":"","color":""})
            specialbType = str(extra.get("specialbType") or "")
            color = str(extra.get("color") or "")
            barcode_text = re.sub(r"[-]", "", lot_code) + qty
            return f"""
<section class="label">
  <div class="title">{specialbType}</div>
  <div class="grid">
    <table class="tbl">
      <colgroup>
        <col class="c1">
        <col class="c2">
        <col class="c3">
        <col class="c4">
      </colgroup>
      <tr>
        <td class="head">품명 item</td>
        <td class="val" colspan="2">{aft_name}</td>
        <td class="blank" rowspan="5"></td>
      </tr>
      <tr>
        <td class="head">수량 Count</td>
        <td class="val">{qty}</td>
        <td class="val">{aft_uom}</td>
      </tr>
      <tr>
        <td class="head">로트 Lot</td>
        <td class="val" colspan="2">{lot_code}</td>
      </tr>
      <tr>
        <td class="head">비고 Note</td>
        <td class="val" colspan="2">&nbsp;</td>
      </tr>
      <tr>
        <td class="head">색상 Color</td>
        <td class="val color" colspan="2">{color}</td>
      </tr>
    </table>
  </div>
  <div class="barcode-area">
    <img class="pdf417" src="{barcode_data_url}" alt="PDF417" />
    <div class="inspector">
      <div class="ins-title">검 사 인<br/><span>Inspector</span></div>
      <div class="ins-box"></div>
    </div>
  </div>
  <div class="foot-note">{barcode_text}</div>
</section>
"""

          # PDF-417 이미지 생성(data URL)
          import io
          try:
            from pdf417gen import encode, render_image
          except Exception as _e:
            st.error("pdf417gen 모듈이 필요합니다. 'pip install pdf417gen pillow' 후 재시도하세요.")
            raise

          # 공통 빌더
          def _build_labels_html(df_src: pd.DataFrame, copies: int) -> str:
            labels_local: List[str] = []
            for _, r in df_src.iterrows():
              aft_lot = str(r.get("_after_lotCode") or "")
              qty_val = str(int(round(float(pd.to_numeric(r.get("_after_onhandQuantity"), errors="coerce") or 0))))
              barcode_text = re.sub(r"[-]", "", aft_lot) + qty_val
              codes = encode(barcode_text, columns=6, security_level=2)
              img = render_image(codes, scale=4, ratio=5, padding=0)
              buf = io.BytesIO()
              img.save(buf, format="PNG")
              data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
              for _ in range(copies):
                labels_local.append(_label_html(r.to_dict(), data_url))
            return "\n".join(labels_local)

          df_all = after_df.copy()
          df_lh = df_all[df_all["_after_itemName"].astype(str).str.contains(r"\bLH\b", case=False, na=False)]
          df_rh = df_all[df_all["_after_itemName"].astype(str).str.contains(r"\bRH\b", case=False, na=False)]
          combined_html_all = _build_labels_html(df_all, copies)
          combined_html_lh  = _build_labels_html(df_lh, copies)
          combined_html_rh  = _build_labels_html(df_rh, copies)

          base_href = st.session_state["base_url"].rstrip("/") + "/"
          printable_html_tpl = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Labels</title>
  <base href="__BASE__">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <style>
    @page { size: 100mm 125mm; margin: 4mm; }
    * { box-sizing: border-box; }
    html, body { padding:0; margin:0; }
    #labels { page-break-inside: avoid; }
    #labels > .label { page-break-after: always; }
    #labels > .label:last-child { page-break-after: auto; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans KR', Arial, sans-serif; }
    .label { width: 100%; min-height: calc(125mm - 8mm); border: 1px solid #111; padding: 6mm; position: relative; background:#fff; }
    .title { border: 1px solid #111; text-align: center; font-weight: 800; font-size: 14pt; padding: 1mm 1mm; margin-bottom: 4mm; background:#fff; height: 10mm }
    .grid { border:1px solid #111; background:#fff; }
    .grid .tbl { width:100%; border-collapse:collapse; table-layout:fixed; margin:0; }
    .grid col.c1 { width:18mm; }
    .grid col.c2 { width:auto; }
    .grid col.c3 { width:15mm; }
    .grid col.c4 { width:15mm; }
    .grid td { border:1px solid #111; padding:1mm 1.8mm; font-size:9pt; vertical-align:middle; }
    .grid td.head { font-weight:700; font-size:8pt; }
    .grid td.color { color:#e21; font-weight:800; }
    .grid td.blank { background:#fff; }
    .grid tr td:last-child { border-right:1px solid #111; }
    .row .cell.span2, .row .cell.val.span2 { border-right: 1px solid #111 !important; }
    .row .cell.blank { border-left: 0 !important; }
    .row.qty .cell:nth-child(2) { border-right: 1px solid #111 !important; }
    .row.qty .cell:nth-child(3) { border-left: 0 !important; }
    .color { color:#e21; font-weight:800; }
    .val{ font-size: 8pt; line-height: 0.8; word-break: break-word; white-space: normal; padding: 2mm 0.5mm; }
    .row .cell{ padding: 1.0mm 1mm; line-height: 1.25; }
    .row:not(:last-child) .blank { border-bottom:0; }
    .cell:last-child { border-right:0; }
    .barcode-area { margin-top: 0mm !important; position: relative; padding-right: 20mm; background:#fff; }
    .barcode-area img.pdf417 { width: 55mm; height: 10mm; display: block; object-fit: contain; }
    .inspector { position: absolute; top: 0; right: 0; width: 20mm; height: auto; display: flex; flex-direction: column; gap: 1mm; background: #fff; }
    .ins-title { border: 1px solid #111; text-align: center; padding: 2mm; font-weight: 700; line-height: 1.2; background: #fff; font-size: 8pt; }
    .ins-title span { font-weight:600; font-size:9pt; }
    .ins-box { border: 1px solid #111; height: 10mm; background: #fff; }
    .foot-note { margin-top:2mm; font-size:9pt; color:#333; background:#fff; }
  </style>
</head>
<body>
  <div id="labels">__LABELS__</div>
</body>
</html>"""

          printable_html_all = printable_html_tpl.replace("__BASE__", base_href).replace("__LABELS__", combined_html_all)
          printable_html_lh  = printable_html_tpl.replace("__BASE__", base_href).replace("__LABELS__", combined_html_lh)
          printable_html_rh  = printable_html_tpl.replace("__BASE__", base_href).replace("__LABELS__", combined_html_rh)

          _html_json_all = json.dumps(printable_html_all).replace("</script", "</scr\"+\"ipt")
          _html_json_lh  = json.dumps(printable_html_lh ).replace("</script", "</scr\"+\"ipt")
          _html_json_rh  = json.dumps(printable_html_rh ).replace("</script", "</scr\"+\"ipt")

          viewer_html = """
<div style="padding:8px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
  <button id="btn-print-all" style="padding:10px 14px;border:0;border-radius:10px;font-weight:700;background:linear-gradient(135deg,#5ac8fa,#7ee081);color:#0b1020;cursor:pointer;">🖨️ 라벨 인쇄</button>
  <button id="btn-print-lh" style="padding:10px 14px;border:0;border-radius:10px;font-weight:700;background:linear-gradient(135deg,#7ee081,#5ac8fa);color:#0b1020;cursor:pointer;">LH</button>
  <button id="btn-print-rh" style="padding:10px 14px;border:0;border-radius:10px;font-weight:700;background:linear-gradient(135deg,#7ee081,#5ac8fa);color:#0b1020;cursor:pointer;">RH</button>
  <div style="font-size:12px;color:#9aa3b2;margin-top:6px;">새 창 없이 인쇄 미리보기를 엽니다. (팝업 허용 불필요)</div>
</div>
<iframe id="print-frame" style="width:0;height:0;border:0;position:absolute;left:-9999px;top:-9999px;" aria-hidden="true"></iframe>
<script>
(function(){
  const htmlAll = {{HTML_ALL}};
  const htmlLH  = {{HTML_LH}};
  const htmlRH  = {{HTML_RH}};
  const fr = document.getElementById('print-frame');
  function loadAndPrint(html){
    try{
      fr.onload = function(){
        try{ fr.contentWindow.focus(); fr.contentWindow.print(); }catch(e){ alert('인쇄 호출 실패: ' + e); }
        finally{ fr.onload = null; }
      };
      fr.srcdoc = html;
    }catch(e){ alert('인쇄 프레임 설정 실패: ' + e); }
  }
  document.getElementById('btn-print-all').addEventListener('click', function(){ loadAndPrint(htmlAll); });
  document.getElementById('btn-print-lh' ).addEventListener('click', function(){ loadAndPrint(htmlLH ); });
  document.getElementById('btn-print-rh' ).addEventListener('click', function(){ loadAndPrint(htmlRH ); });
})();
</script>
""".replace("{{HTML_ALL}}", _html_json_all).replace("{{HTML_LH}}", _html_json_lh).replace("{{HTML_RH}}", _html_json_rh)

          components.html(viewer_html, height=120)

        except Exception as e:
          st.error(f"라벨출력 예외: {e}")

      # ---------- 기타입고 (저장 → 전송 즉시) ----------
      if exec_receipt_btn:
        after_df = st.session_state["preview_df_full"].copy()
        if after_df.empty:
          st.warning("미리보기/카트가 비어 있습니다.", icon="⚠️")
          st.stop()
        try:
          company_id, plant_id, company_code, user_id = _context_ids()
          sess: requests.Session = st.session_state["sess"]
          base_now = dt.datetime.now()
          base_ymd = base_now.strftime("%Y-%m-%d")
          trans_dt = base_now.strftime("%Y-%m-%d %H:%M:%S")
          tx_dt   = (base_now + dt.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")  # 서버표시 보정(+9h)

          after_wh = st.session_state["wh_selected"] or {}
          wh_id = _to_int_safe(after_wh.get("warehouseId"), 0)
          wh_code = after_wh.get("warehouseCode") or ""
          wh_name = after_wh.get("warehouseName") or ""

          alias = st.session_state["alias_selected"] or {}
          account_alias_id = _to_int_safe(alias.get("accountAliasId"), 10009)
          account_alias_code = str(alias.get("accountAliasCode") or "")   # ← 추가
          account_alias_name = str(alias.get("accountAliasName") or "TEST")

          grp = after_df.groupby(["_after_itemCode","_after_itemName","_after_primaryUom"], dropna=False)

          results = []
          for (aft_code, aft_name, aft_uom), g in grp:
            plant_items = _plant_item_list(q_code=str(aft_code or ""))
            if plant_items.empty:
              st.error(f"품목정보 없음: {aft_code} / {aft_name}"); st.stop()
            item_row = plant_items.iloc[0]
            item_id = _to_int_safe(item_row.get("itemId"), 0)
            primary_uom = str(item_row.get("primaryUom") or aft_uom or "")
            secondary_uom = str(item_row.get("secondaryUom") or primary_uom)

            total_qty = float(pd.to_numeric(g["_after_onhandQuantity"], errors="coerce").fillna(0).sum())

            acct_num = _get_account_num_by_code_rule(base_ymd)
            if not acct_num:
              st.error("타계정번호 채번 실패"); st.stop()

            header = [{
              "editStatus":"I","companyId":company_id,"plantId":plant_id,"accountNum":acct_num,
              "transactionTypeId":10080,"transactionTypeCode":"Account_Receipt","transactionTypeName":"기타입고",
              "accountAliasId":account_alias_id,"accountAliasCode":account_alias_code,"accountAliasName":account_alias_name,
              "warehouseId":wh_id,"warehouseCode":wh_code,"warehouseName":wh_name,
              "locationId":0,"locationCode":"","locationName":None,
              "transactionDate":trans_dt,"accountResultId":0,"lotCount":0,
              "primaryQuantity":total_qty,"secondaryQuantity":total_qty,
              "projectId":0,"effectiveStartDate":None,"effectiveEndDate":None,
              "approvalFlag":"Y","interfaceFlag":"N","workStatus":"I",
              "id":"ext-receipt","row-active":True,
              "itemCode":str(aft_code or ""), "itemId":item_id, "itemName":str(aft_name or ""),
              "status":"Active","itemType":item_row.get("itemType") or "", "itemTypeName":item_row.get("itemTypeName") or "",
              "controlLotSerial":"LOT","primaryUom":primary_uom,"secondaryUom":secondary_uom,
              "effectivePeriodOfDay":0,"effectivePeriodOfDayFlag":"N","availableForLocationFlag":"N","errorField":{}
            }]
            with st.spinner(f"① 기타입고 헤더 저장(top-save) 중... [{aft_code}]"):
              ok = _receipt_top_save(header)
            if not ok:
              st.error("기타입고 top-save 실패"); st.stop()

            tl = _receipt_top_list(ymd=base_ymd)
            tl = tl[(tl["accountNum"]==acct_num)]
            if tl.empty:
              st.error("기타입고 top-list 조회 실패"); st.stop()
            top_row = tl.iloc[0].to_dict()
            account_result_id = int(top_row["accountResultId"])
            # ▼ (순서 변경) 거래일자 갱신은 bottom-save 성공 후에 수행

            lot_rows = []
            for _, row in g.iterrows():
              _qty = float(pd.to_numeric(row["_after_onhandQuantity"], errors="coerce") or 0)
              lot_rows.append({
                "editStatus":"I","companyId":company_id,"plantId":plant_id,"accountResultId":account_result_id,
                "warehouseId":wh_id,"warehouseCode":wh_code,"warehouseName":wh_name,
                "itemId":item_id,"primaryUom":primary_uom,"primaryQuantity":_qty,
                "lotQuantity":_qty,"secondaryUom":secondary_uom,"secondaryQuantity":_qty,
                "effectiveStartDate":None,"effectiveEndDate":None,"effectivePeriodOfDayFlag":"N",  # ← 필수 필드 추가
                "parentLotCount":int(len(g)),"parentPrimaryQuantity":float(total_qty),
                "parentEffectiveStartDate":None,"parentEffectiveEndDate":None,"parentInterfaceFlag":"N",
                "lotCode":str(row["_after_lotCode"]),"lotType":"양품","lotId":0,"interfaceFlag":"N",
                "id":"ext-receipt-lot","row-active":True,"errorField":{}
              })
            with st.spinner(f"② LOT 저장(bottom-save) 중... [{aft_code}]"):
              ok2, err_msg = _receipt_bottom_save(lot_rows)
            if not ok2:
              st.error(f"기타입고 bottom-save 실패: {err_msg or '서버 사유 미반환'}")
              st.stop()
              
             # ▼ 거래일자 현재시간(+9h 보정)으로 갱신(최소 변경 1줄)
            _ = _receipt_top_update_transaction_date(top_row, tx_dt)

            with st.spinner("③ 전송 처리 중...(menugrid → bottom-transmit → top-transmit)"):
              ok_tx = _receipt_transmit(account_result_id)
            if not ok_tx:
              st.error("전송 실패(top/bottom transmit)"); st.stop()

            results.append({"accountNum":acct_num, "accountResultId":account_result_id, "itemCode":aft_code, "qty":total_qty})

          st.success("✅ 기타입고 저장 + 전송 완료")
          for r in results:
            st.write(f"- 전표 **{r['accountNum']}** · accountResultId={r['accountResultId']} · 품목 {r['itemCode']} · 수량 {r['qty']}")

        except Exception as e:
          st.error(f"예외: {e}")
