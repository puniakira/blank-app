# -*- coding: utf-8 -*-
import streamlit as st
import requests
import xml.etree.ElementTree as ET
import pandas as pd
from datetime import datetime, date
import google.generativeai as genai
import re # 正規表現のため

# --- PAGE CONFIG (MUST BE FIRST ST COMMAND) ---
st.set_page_config(page_title="e-Gov 法令検索 AI", layout="wide")
# --- END PAGE CONFIG ---

# --- Constants ---
API_BASE_URL = "https://laws.e-gov.go.jp/api/1"
EGOV_LAW_VIEW_URL = "https://elaws.e-gov.go.jp/document?lawid="

LAW_TYPES = { "すべて (All)": "1", "憲法・法律 (Constitution/Law)": "2", "政令・勅令 (Cabinet/Imperial Order)": "3", "府省令・規則 (Ministerial Ordinance/Rule)": "4", }
LAW_TYPES_REV = {v: k for k, v in LAW_TYPES.items()}
SPECIFIC_LAW_TYPE_CODES = ['2', '3', '4']
TYPE_SORT_ORDER = { '2': 1, '3': 2, '4': 3, }
TYPE_SORT_KEY = "_type_sort_key"
BASE_SORTABLE_COLUMNS = { "法令名 (Law Name)": "法令名 (Law Name)", "法令番号 (Law Number)": "法令番号 (Law Number)", "公布日 (Promulgation Date)": "公布日 (Promulgation Date)_dt", }
DEFAULT_SPECIFIC_TYPE_SORT_COLUMN = "法令番号 (Law Number)"
DEFAULT_SPECIFIC_TYPE_SORT_ASCENDING = True
DEFAULT_ALL_TYPE_SORT_COLUMN = TYPE_SORT_KEY
DEFAULT_ALL_TYPE_SORT_ASCENDING = True

# --- Gemini Configuration ---
try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash') # Or 'gemini-1.5-pro-latest' if available
    GEMINI_ENABLED = True
except (KeyError, FileNotFoundError):
    st.warning("Gemini API Key not found in st.secrets. AI features disabled.", icon="⚠️")
    GEMINI_ENABLED = False
except Exception as e:
    st.error(f"Error initializing Gemini: {e}. AI features disabled.", icon="🚨")
    GEMINI_ENABLED = False


# --- API Helper Functions (parse_api_response, _fetch_specific_type, fetch_law_list は変更なし) ---
def parse_api_response(xml_text):
    try:
        if xml_text.startswith('\ufeff'): xml_text = xml_text[1:]
        root = ET.fromstring(xml_text)
        result_code_node = root.find("./Result/Code"); message_node = root.find("./Result/Message")
        result_code = result_code_node.text if result_code_node is not None else "-1"
        message = message_node.text if message_node is not None else "No message provided"
        if result_code != "0": return None, result_code, message
        app_data = root.find("./ApplData")
        if app_data is None: return None, "0", "API returned success code 0 but no ApplData found."
        return app_data, result_code, message
    except ET.ParseError as e:
        error_line = None; L = xml_text.splitlines()
        if hasattr(e, 'position') and len(L) >= e.position[0]: error_line = L[e.position[0]-1]
        return None, "-1", f"XML Parse Error: {e}. Near line {e.position[0] if hasattr(e,'position') else '?'}: '{error_line}'"
    except Exception as e: return None, "-1", f"Unexpected parsing error: {e}"

def _fetch_specific_type(law_type_code):
    url = f"{API_BASE_URL}/lawlists/{law_type_code}"; laws = []
    try:
        response = requests.get(url, timeout=30); response.raise_for_status()
        response.encoding = response.apparent_encoding or 'utf-8'
        app_data, result_code, message = parse_api_response(response.text)
        if app_data is None: st.error(f"法令種別 {law_type_code} のリスト取得APIエラー (Code: {result_code}): {message}"); return []
        for list_info in app_data.findall("./LawNameListInfo"):
            law_id = list_info.findtext("LawId"); law_name = list_info.findtext("LawName"); law_no = list_info.findtext("LawNo")
            promulgation_date_str = list_info.findtext("PromulgationDate"); promulgation_date_obj = None
            if promulgation_date_str:
                 try: promulgation_date_obj = datetime.strptime(promulgation_date_str, "%Y%m%d").date()
                 except (ValueError, TypeError): promulgation_date_obj = None
            laws.append({ "LawId": law_id, "法令名 (Law Name)": law_name, "法令番号 (Law Number)": law_no, "公布日 (Promulgation Date)_dt": promulgation_date_obj, "法令種別コード (Law Type Code)": law_type_code, TYPE_SORT_KEY: TYPE_SORT_ORDER.get(law_type_code, 99) })
        return laws
    except requests.exceptions.RequestException as e: st.error(f"ネットワークエラー (法令種別 {law_type_code}): {e}"); return []
    except Exception as e: st.error(f"予期せぬエラー (法令種別 {law_type_code}): {e}"); return []

def fetch_law_list(requested_law_type_code):
    all_laws = []
    if requested_law_type_code == '1':
        st.write("「すべて」を選択したため、種別ごとにリストを取得します...")
        progress_bar = st.progress(0.0); num_types = len(SPECIFIC_LAW_TYPE_CODES); fetch_errors = False
        for i, code in enumerate(SPECIFIC_LAW_TYPE_CODES):
            st.write(f"- {LAW_TYPES_REV.get(code, code)} を取得中..."); laws_for_type = _fetch_specific_type(code)
            if not laws_for_type: fetch_errors = True
            all_laws.extend(laws_for_type); progress_bar.progress((i + 1) / num_types)
        progress_bar.empty()
        if not all_laws and fetch_errors: st.error("すべての法令種別の取得に失敗しました。"); return None
        elif fetch_errors: st.warning("一部の法令種別の取得中にエラーが発生しました。")
        st.write("リストの結合完了。"); return all_laws
    elif requested_law_type_code in SPECIFIC_LAW_TYPE_CODES:
        laws = _fetch_specific_type(requested_law_type_code)
        if not laws: return None; return laws
    else: st.error(f"無効な法令種別コード: {requested_law_type_code}"); return None

# ★★★ fetch_law_data_for_ai (変更なし - XMLからテキスト抽出) ★★★
@st.cache_data(ttl=3600)
def fetch_law_data_for_ai(law_id):
    if not law_id: return None, "Error: Law ID required."
    url = f"{API_BASE_URL}/lawdata/{law_id}"
    try:
        response = requests.get(url, timeout=60); response.raise_for_status()
        response.encoding = response.apparent_encoding or 'utf-8'; xml_content = response.text
        app_data, result_code, message = parse_api_response(xml_content)
        if result_code != "0": return None, f"API Error (Code: {result_code}) fetching law data for {law_id}: {message}"
        if app_data is None: return None, f"API Error: No ApplData found despite success code for {law_id}."
        relevant_tags = {"LawTitle","ArticleTitle","ParagraphSentence","ItemSentence","Subitem1Sentence","Subitem2Sentence","SupplProvisionLabel","SupplProvisionSentence","Sentence"}
        extracted_texts = [];
        for element in app_data.iter():
            if element.tag in relevant_tags and element.text:
                text = element.text.strip();
                if text: extracted_texts.append(text)
        if not extracted_texts:
            fallback_node = app_data.find("./LawFullText")
            if fallback_node is not None and fallback_node.text and fallback_node.text.strip():
                 st.warning(f"Could not extract structured text for {law_id}, using LawFullText fallback.")
                 raw_text = fallback_node.text; cleaned_text = re.sub(r'\s+', ' ', raw_text).strip()
                 return cleaned_text, None
            else: return None, f"Failed to extract any relevant text content from XML structure for {law_id}."
        combined_text = "\n".join(extracted_texts)
        cleaned_text = re.sub(r'\s+', ' ', combined_text).strip()
        MAX_CHARS = 30000
        if not cleaned_text: return None, f"Extracted text for {law_id} became empty after cleaning."
        return cleaned_text[:MAX_CHARS], None
    except requests.exceptions.HTTPError as e:
         status_code = e.response.status_code
         if status_code == 404: return None, f"指定された法令ID {law_id} が見つかりません (HTTP 404)。"
         elif status_code == 406: return None, f"法令ID {law_id} のデータ取得で問題が発生しました (HTTP 406)。"
         else: return None, f"HTTP Error {status_code} fetching law details for {law_id}."
    except requests.exceptions.RequestException as e: return None, f"Network Error fetching law details for {law_id}: {e}"
    except Exception as e: return None, f"Unexpected error processing law details for {law_id}: {e}"


# --- Gemini Interaction Functions ---
# ★★★ プロンプトに引用指示を追加 ★★★
def get_gemini_chat_response(context, history, user_question):
    if not GEMINI_ENABLED: return "AI機能は無効です。"
    if not context: return "チャットのコンテキスト（法令本文）がありません。"
    gemini_history = [{"role": ("user" if entry["role"] == "user" else "model"), "parts": [entry["content"]]} for entry in history]
    system_instruction = f"""あなたは日本の法律アシスタントAIです。提供された以下の法令本文に基づいて、ユーザーの質問にのみ回答してください。本文から回答が見つからない場合は「提供された本文からは回答できません。」と明確に述べてください。外部知識や推測は使用しないでください。回答は簡潔にお願いします。
**重要:** 回答を作成した後、その回答の主な根拠となった条文番号を、回答の最後に `【引用元: 第〇条】` または `【引用元: 第〇条、第△条】` の形式で**必ず**示してください。該当する条文がない場合は `【引用元: なし】` と記載してください。

--- 法令本文 ---
{context}
--- ここまで ---
"""
    messages_for_api = [{"role": "user", "parts": [system_instruction]}, {"role": "model", "parts": ["承知いたしました。提供された法令本文に基づいて回答し、引用元を示します。"]}]
    messages_for_api.extend(gemini_history)
    messages_for_api.append({"role": "user", "parts": [user_question]})
    try:
        response = gemini_model.generate_content(messages_for_api)
        if response.parts: return response.text
        else:
             reason = response.candidates[0].finish_reason if response.candidates else "Unknown"
             if reason == genai.types.FinishReason.SAFETY: return "回答が安全基準によりブロックされました。"
             elif reason == genai.types.FinishReason.RECITATION: return "回答が引用制限によりブロックされました。"
             else: return f"回答を生成できませんでした。理由コード: {reason}"
    except Exception as e: st.error(f"Gemini チャットエラー: {e}", icon="🚨"); return "チャット応答の生成中にエラーが発生しました。"

# ★★★ 引用元条文を抽出・検索するヘルパー関数 ★★★
def extract_citations(ai_response_text):
    """AIの応答テキストから【引用元: ...】の部分を探し、条文番号のリストを返す"""
    citations = []
    # 【引用元: 第〇条】 や 【引用元: 第〇条、第△条】 などを探す
    match = re.search(r"【引用元:\s*(.+?)\s*】", ai_response_text)
    if match:
        source_text = match.group(1).strip()
        if source_text != "なし":
            # "第〇条" の形式を抽出 (漢数字にも対応できるように簡易的に)
            # 例: "第一条"、"第百二十三条"、"第5条の2" などに対応 (複雑なものは未対応)
            # "、" や "及び" で区切られている可能性も考慮
            potential_articles = re.findall(r"(?:第(?:[一二三四五六七八九十百千]+|[0-9]+)(?:条(?:の[一二三四五六七八九十百千]|[0-9]+)*)?)", source_text)
            citations.extend(potential_articles)
    return citations

def kanji_to_arabic(kanji_num):
    """簡易的な漢数字（一〜九千九百九十九）をアラビア数字に変換"""
    # 簡単のため、ここでは基本的な一桁のみ対応（必要ならライブラリ等で拡張）
    kanji_map = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6', '七': '7', '八': '8', '九': '9'}
    # TODO: 十、百、千や「の」を含むより複雑な変換ロジックを追加
    arabic_num_str = ""
    for char in kanji_num:
        arabic_num_str += kanji_map.get(char, char) # マップにない文字はそのまま
    return arabic_num_str

def find_article_text(full_law_text, article_title):
    """
    法令全文テキストから指定された条のテキスト（次の条まで）を抽出する試み。
    article_title は "第一条" や "第5条の2" のような形式を想定。
    """
    if not full_law_text or not article_title:
        return None

    # 簡易的な正規表現: 「第〇条」で始まり、次の「第△条」の前までを非貪欲にマッチ
    # re.DOTALL で改行も"."にマッチさせる
    # 漢数字とアラビア数字の両方を考慮 (簡易)
    # 例: article_title = "第一条" -> pattern_str = r"(第一条\s*.*?)(?=第二条|\Z)" (次の条を推測する必要あり)
    # より汎用的に: 次の「第〜条」が現れるまでを探す
    pattern_str = rf"({re.escape(article_title)}\s*.*?)(?=第(?:[一二三四五六七八九十百千]+|[0-9]+)(?:条(?:の[一二三四五六七八九十百千]|[0-9]+)*)?|\Z)"
    match = re.search(pattern_str, full_law_text, re.DOTALL)

    if match:
        return match.group(1).strip()
    else:
        # 単純な文字列検索でフォールバック
        start_index = full_law_text.find(article_title)
        if start_index != -1:
            # 次の条が見つからない場合、末尾までか、一定文字数で区切る？
             end_index = full_law_text.find("第", start_index + len(article_title)) # 次の「第」を探す
             if end_index != -1:
                 # 次の「第」が本当に条の始まりか判断するのは難しい
                 # とりあえず次の「第」の前までを返す（精度は低い）
                 return full_law_text[start_index:end_index].strip()
             else:
                 return full_law_text[start_index:].strip() # 末尾まで
        return None # 見つからなかった

# --- Filtering Function (変更なし) ---
def filter_laws(laws, name_query, num_query, keyword_query, date_from, date_to):
    if not laws: return pd.DataFrame()
    df = pd.DataFrame(laws)
    if name_query: df = df[df['法令名 (Law Name)'].str.contains(name_query, case=False, na=False)]
    if num_query: df = df[df['法令番号 (Law Number)'].str.contains(num_query, case=False, na=False)]
    if keyword_query: df = df[ (df['法令名 (Law Name)'].str.contains(keyword_query, case=False, na=False)) | (df['法令番号 (Law Number)'].str.contains(keyword_query, case=False, na=False)) ]
    if date_from: df = df[df['公布日 (Promulgation Date)_dt'].notna() & (df['公布日 (Promulgation Date)_dt'] >= date_from)]
    if date_to: df = df[df['公布日 (Promulgation Date)_dt'].notna() & (df['公布日 (Promulgation Date)_dt'] <= date_to)]
    df['法令種別 (Law Type)'] = df['法令種別コード (Law Type Code)'].map(LAW_TYPES_REV).fillna("不明")
    final_cols = [ "LawId", "法令名 (Law Name)", "法令番号 (Law Number)", "公布日 (Promulgation Date)_dt", "法令種別 (Law Type)", TYPE_SORT_KEY ]
    df_filtered = df[[col for col in final_cols if col in df.columns]].copy()
    return df_filtered


# --- Streamlit UI ---
st.title("e-Gov 法令検索システム (AI機能付き)")
st.caption("法令名を検索・ソートし、AIによる要約や質問応答を利用できます。質問応答では引用元条文も表示します。")
st.sidebar.header("検索条件 (Search Criteria)")
if 'selected_law_type_name' not in st.session_state: st.session_state.selected_law_type_name = "すべて (All)"
selected_law_type_name_input = st.sidebar.selectbox( "法令種別 (Law Type)", options=list(LAW_TYPES.keys()), key='selected_law_type_name', )
law_type_code_selected = LAW_TYPES[st.session_state.selected_law_type_name]
name_query = st.sidebar.text_input("法令名キーワード")
num_query = st.sidebar.text_input("法令番号キーワード")
keyword_query = st.sidebar.text_input("フリーキーワード")
today = datetime.today().date(); col1, col2 = st.sidebar.columns(2)
with col1: date_from = st.date_input("公布日 From", value=None, max_value=today)
with col2: date_to = st.date_input("公布日 To", value=today, max_value=today)
if date_from and date_to and date_from > date_to: st.sidebar.error("Error: 'From' date cannot be after 'To' date."); search_clicked = False
else: search_clicked = st.sidebar.button("検索実行 (Search)")
# Session State Init (変更なし)
if 'search_results_raw' not in st.session_state: st.session_state.search_results_raw = None
if 'filtered_results_df' not in st.session_state: st.session_state.filtered_results_df = pd.DataFrame()
current_selection_code_init = LAW_TYPES.get(st.session_state.get('selected_law_type_name', "すべて (All)"), '1'); current_default_sort_col_init = DEFAULT_ALL_TYPE_SORT_COLUMN if current_selection_code_init == '1' else DEFAULT_SPECIFIC_TYPE_SORT_COLUMN; current_default_sort_asc_init = DEFAULT_ALL_TYPE_SORT_ASCENDING if current_selection_code_init == '1' else DEFAULT_SPECIFIC_TYPE_SORT_ASCENDING
if 'sort_column' not in st.session_state: st.session_state.sort_column = current_default_sort_col_init
if 'sort_ascending' not in st.session_state: st.session_state.sort_ascending = current_default_sort_asc_init
if 'summarize_law_id' not in st.session_state: st.session_state.summarize_law_id = None; 
if 'current_summary' not in st.session_state: st.session_state.current_summary = None; 
if 'summary_loading' not in st.session_state: st.session_state.summary_loading = False
if 'qa_law_id' not in st.session_state: st.session_state.qa_law_id = None; 
if 'qa_law_name' not in st.session_state: st.session_state.qa_law_name = None; 
if 'qa_law_context' not in st.session_state: st.session_state.qa_law_context = None; 
if 'qa_chat_history' not in st.session_state: st.session_state.qa_chat_history = []; 
if 'qa_loading' not in st.session_state: st.session_state.qa_loading = False

# --- Search Execution (変更なし) ---
if search_clicked:
    st.session_state.search_results_raw = None; st.session_state.filtered_results_df = pd.DataFrame(); st.session_state.summarize_law_id = None; st.session_state.current_summary = None; st.session_state.qa_law_id = None; st.session_state.qa_chat_history = []; st.session_state.qa_law_context = None
    if law_type_code_selected == '1': st.session_state.sort_column = DEFAULT_ALL_TYPE_SORT_COLUMN; st.session_state.sort_ascending = DEFAULT_ALL_TYPE_SORT_ASCENDING
    else: st.session_state.sort_column = DEFAULT_SPECIFIC_TYPE_SORT_COLUMN; st.session_state.sort_ascending = DEFAULT_SPECIFIC_TYPE_SORT_ASCENDING
    with st.spinner(f"'{st.session_state.selected_law_type_name}' の法令リストを取得・フィルタリング中..."):
        raw_laws = fetch_law_list(law_type_code_selected)
        st.session_state.search_results_raw = raw_laws
        if raw_laws is not None: filtered_df = filter_laws(raw_laws, name_query, num_query, keyword_query, date_from, date_to); st.session_state.filtered_results_df = filtered_df
        else: st.session_state.filtered_results_df = pd.DataFrame()

# --- Logic to Fetch Data for AI actions (変更なし) ---
if st.session_state.summary_loading and st.session_state.summarize_law_id:
     summary_law_id = st.session_state.summarize_law_id; law_text, error = fetch_law_data_for_ai(summary_law_id)
     if error: st.session_state.current_summary = f"要約のための本文取得エラー: {error}"; st.error(st.session_state.current_summary)
     elif law_text: st.session_state.current_summary = get_gemini_summary(law_text)
     else: st.session_state.current_summary = "要約対象の法令本文が見つかりませんでした (本文空)。"
     st.session_state.summary_loading = False; st.rerun()
if st.session_state.qa_loading and st.session_state.qa_law_id:
     qa_law_id_fetch = st.session_state.qa_law_id; law_text, error = fetch_law_data_for_ai(qa_law_id_fetch)
     if error: st.error(f"Q&Aのための本文取得エラー ({qa_law_id_fetch}): {error}"); st.session_state.qa_law_id = None; st.session_state.qa_loading = False; st.rerun()
     elif law_text: st.session_state.qa_law_context = law_text; st.session_state.qa_loading = False; st.rerun()
     else: st.error(f"Q&A対象の法令本文が見つかりませんでした ({qa_law_id_fetch})。"); st.session_state.qa_law_id = None; st.session_state.qa_loading = False; st.rerun()

# --- AI Feature Display Area ---
summary_placeholder = st.empty(); qa_placeholder = st.empty()
# Display Summary (変更なし)
if st.session_state.summarize_law_id and not st.session_state.summary_loading:
    with summary_placeholder.container(border=True):
        st.subheader(f"📜 法令要約"); st.caption(f"対象法令ID: {st.session_state.summarize_law_id}")
        if st.session_state.current_summary: st.markdown(st.session_state.current_summary)
        else: st.warning("要約を表示できません。エラーを確認してください。")
        if st.button("要約を閉じる", key="close_summary"): st.session_state.summarize_law_id = None; st.session_state.current_summary = None; st.rerun()

# Display Q&A (★ 引用表示ロジック追加)
if st.session_state.qa_law_id and not st.session_state.qa_loading:
    with qa_placeholder.container(border=True):
        st.subheader(f"💬 法令に関する質問 ({st.session_state.qa_law_name or st.session_state.qa_law_id})")
        if st.session_state.qa_law_context is None : st.error("法令コンテキストを読み込めなかったため、質問を開始できません。")
        else:
            # Display chat history
            for i, message in enumerate(st.session_state.qa_chat_history):
                with st.chat_message(message["role"]):
                    st.markdown(message["content"])
                    # ★ AIの回答の後に引用元を表示
                    if message["role"] == "assistant":
                        citations = extract_citations(message["content"])
                        if citations:
                            with st.expander("引用元条文（AIによる推定）", expanded=False):
                                for cited_article_title in citations:
                                    cited_text = find_article_text(st.session_state.qa_law_context, cited_article_title)
                                    if cited_text:
                                        st.caption(f"--- {cited_article_title} ---")
                                        st.text(cited_text) # textで整形を維持
                                        st.caption("---")
                                    else:
                                        st.warning(f"引用元「{cited_article_title}」の本文をコンテキスト内から見つけられませんでした。")
                                st.caption("※AIが示した引用元であり、正確性は保証されません。")

            # Chat input
            if prompt := st.chat_input("法令について質問を入力してください..."):
                st.session_state.qa_chat_history.append({"role": "user", "content": prompt})
                with st.chat_message("user"): st.markdown(prompt)
                with st.chat_message("assistant"):
                    message_placeholder = st.empty()
                    with st.spinner("AIが回答生成中..."):
                        response_text = get_gemini_chat_response(st.session_state.qa_law_context, st.session_state.qa_chat_history[:-1], prompt)
                        message_placeholder.markdown(response_text)
                st.session_state.qa_chat_history.append({"role": "assistant", "content": response_text})
                st.rerun() # ★ 新しいメッセージと引用元を表示するためにリラン

        if st.button("チャットを終了", key="close_qa"): st.session_state.qa_law_id = None; st.session_state.qa_law_name = None; st.session_state.qa_law_context = None; st.session_state.qa_chat_history = []; st.rerun()


# --- Display Search Results (表示部分は変更なし) ---
st.divider(); st.header("検索結果 (Search Results)")
results_df = st.session_state.filtered_results_df
if not results_df.empty:
    # ...(Info, Sorting Controls, Sorting Logic は変更なし)...
    st.info(f"{len(results_df)} 件の法令が見つかりました。")
    current_sortable_columns = BASE_SORTABLE_COLUMNS.copy();
    if TYPE_SORT_KEY in results_df.columns: current_sortable_columns["法令種別 (Law Type)"] = TYPE_SORT_KEY
    num_sort_cols = len(current_sortable_columns); total_header_cols = num_sort_cols + 2; control_cols = st.columns(total_header_cols)
    for i, (display_name, sort_key) in enumerate(current_sortable_columns.items()):
        with control_cols[i]:
            arrow = " ▲" if st.session_state.sort_column == sort_key and st.session_state.sort_ascending else (" ▼" if st.session_state.sort_column == sort_key else "")
            if st.button(f"{display_name}{arrow}", key=f"sort_{sort_key}", use_container_width=True):
                if st.session_state.sort_column == sort_key: st.session_state.sort_ascending = not st.session_state.sort_ascending
                else: st.session_state.sort_column = sort_key; st.session_state.sort_ascending = (sort_key != "公布日 (Promulgation Date)_dt")
                st.rerun()
    control_cols[num_sort_cols].markdown("**AI機能**", help="各行右のボタンで利用")
    with control_cols[num_sort_cols + 1]:
        active_search_code = LAW_TYPES.get(st.session_state.selected_law_type_name, '1'); reset_target_col = DEFAULT_ALL_TYPE_SORT_COLUMN if active_search_code == '1' else DEFAULT_SPECIFIC_TYPE_SORT_COLUMN; reset_target_asc = DEFAULT_ALL_TYPE_SORT_ASCENDING if active_search_code == '1' else DEFAULT_SPECIFIC_TYPE_SORT_ASCENDING
        if st.button("デフォルト順序", key="reset_sort", use_container_width=True, help="ソート順をデフォルトに戻します。"): st.session_state.sort_column = reset_target_col; st.session_state.sort_ascending = reset_target_asc; st.rerun()
    sort_col_key = st.session_state.sort_column; sorted_df = results_df
    if sort_col_key in results_df.columns:
        try: sorted_df = results_df.sort_values(by=sort_col_key, ascending=st.session_state.sort_ascending, na_position='last')
        except Exception as e: st.error(f"ソートエラー: {e}")
    else:
        active_search_code = LAW_TYPES.get(st.session_state.selected_law_type_name, '1'); fallback_sort_col = DEFAULT_ALL_TYPE_SORT_COLUMN if active_search_code == '1' else DEFAULT_SPECIFIC_TYPE_SORT_COLUMN; fallback_sort_asc = DEFAULT_ALL_TYPE_SORT_ASCENDING if active_search_code == '1' else DEFAULT_SPECIFIC_TYPE_SORT_ASCENDING
        if fallback_sort_col in results_df.columns: sorted_df = results_df.sort_values(by=fallback_sort_col, ascending=fallback_sort_asc, na_position='last')
        else: st.warning(f"デフォルトソート列 '{fallback_sort_col}' が見つかりません。")

    st.divider(); data_column_ratios = [3, 2, 1.5, 1.5, 1.5]; cols_to_display_base = ["法令名 (Law Name)", "法令番号 (Law Number)", "公布日 (Promulgation Date)", "法令種別 (Law Type)"]
    for index, row in sorted_df.iterrows():
        display_cols = st.columns(data_column_ratios); col_idx = 0; law_id = row.get("LawId"); law_name_text = row.get("法令名 (Law Name)", "N/A")
        if law_id and law_name_text != "N/A": link_url = f"{EGOV_LAW_VIEW_URL}{law_id}"; display_name = law_name_text.replace('"', '"'); link_html = f'<a href="{link_url}" target="_blank" rel="noopener noreferrer" title="e-Govで開く: {display_name}">{display_name}</a>'; display_cols[col_idx].markdown(link_html, unsafe_allow_html=True)
        else: display_cols[col_idx].write(law_name_text)
        col_idx += 1; display_cols[col_idx].write(row.get("法令番号 (Law Number)", "N/A")); col_idx += 1
        date_obj = row.get("公布日 (Promulgation Date)_dt")
        if pd.notna(date_obj) and isinstance(date_obj, (datetime, date, pd.Timestamp)):
            try: display_cols[col_idx].write(date_obj.strftime('%Y-%m-%d'))
            except ValueError: display_cols[col_idx].write("Date Format Error")
        else: display_cols[col_idx].write("N/A")
        col_idx += 1; display_cols[col_idx].write(row.get("法令種別 (Law Type)", "N/A")); col_idx += 1
        with display_cols[col_idx]:
            sub_cols = st.columns(1)
            with sub_cols[0]:
                 if GEMINI_ENABLED and law_id:
                      button_key_summary = f"summary_{law_id}_{index}"; disable_summary = (st.session_state.summary_loading and st.session_state.summarize_law_id == law_id) or (st.session_state.qa_loading and st.session_state.qa_law_id == law_id) or (st.session_state.summarize_law_id == law_id and not st.session_state.summary_loading)
                      if st.button("要約", key=button_key_summary, help="この法令のAI要約を表示します。", use_container_width=True, disabled=disable_summary): st.session_state.qa_law_id = None; st.session_state.qa_chat_history = []; st.session_state.summarize_law_id = law_id; st.session_state.current_summary = None; st.session_state.summary_loading = True; st.rerun()
            with sub_cols[0]:
                if GEMINI_ENABLED and law_id:
                    button_key_qa = f"qa_{law_id}_{index}"; disable_qa = (st.session_state.summary_loading and st.session_state.summarize_law_id == law_id) or (st.session_state.qa_loading and st.session_state.qa_law_id == law_id) or (st.session_state.qa_law_id == law_id and not st.session_state.qa_loading)
                    if st.button("質問", key=button_key_qa, help="この法令についてAIに質問します。", use_container_width=True, disabled=disable_qa): st.session_state.summarize_law_id = None; st.session_state.current_summary = None; st.session_state.qa_law_id = law_id; st.session_state.qa_law_name = law_name_text; st.session_state.qa_chat_history = []; st.session_state.qa_law_context = None; st.session_state.qa_loading = True; st.rerun()
        st.divider()
elif search_clicked and st.session_state.filtered_results_df.empty:
    if st.session_state.search_results_raw is None: st.warning("法令リストの取得に失敗したか、中断されました。APIエラーを確認してください。")
    else: st.info("指定された条件に一致する法令は見つかりませんでした。")
else: st.write("検索条件を入力して検索を実行してください。")
