#!/usr/bin/env python3
"""
Notion → index.html 本棚同期スクリプト
使い方:
  python sync_notion.py          # 通常の同期
  python sync_notion.py --test   # APIテスト（HTMLは変更しない）
  python sync_notion.py --debug  # 最初の1件のプロパティを全て表示
"""

import json
import re
import sys
from pathlib import Path
import urllib.request
import urllib.error

# ============================================================
# 設定
# ============================================================
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "notion_config.json"
HTML_PATH   = SCRIPT_DIR / "index.html"

# 対象フィルター
IMPORTANCE_PROP  = "個人的重要度"
IMPORTANCE_VALUE = "★★★ 最重要"

# 本の色（ローテーション用）
COLORS = ["wine", "forest", "navy", "charcoal", "ivory"]  # plum は新刊コーナー専用にしない場合は追加してもOK

# ジャーナル略称マップ
JOURNAL_ABBREV = {
    "Journal of Vascular Surgery":                              "J Vasc Surg",
    "Annals of Thoracic Surgery":                               "Ann Thorac Surg",
    "Journal of Thoracic and Cardiovascular Surgery":           "J Thorac Cardiovasc Surg",
    "Annals of Vascular Surgery":                               "Ann Vasc Surg",
    "European Journal of Vascular and Endovascular Surgery":    "Eur J Vasc Endovasc Surg",
    "Circulation":                                              "Circulation",
    "JAMA":                                                     "JAMA",
    "New England Journal of Medicine":                          "N Engl J Med",
    "Journal of the American College of Cardiology":            "JACC",
    "Vascular":                                                 "Vascular",
    "Interactive CardioVascular and Thoracic Surgery":          "ICVTS",
}

# ============================================================
# Notion API
# ============================================================
def load_config():
    if not CONFIG_PATH.exists():
        print(f"❌ {CONFIG_PATH} が見つかりません。")
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

def notion_request(token, endpoint, method="POST", data=None):
    url = f"https://api.notion.com/v1/{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    body = json.dumps(data or {}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8")
        print(f"❌ Notion API エラー {e.code}: {msg}")
        sys.exit(1)

def fetch_all_important_pages(token, database_id):
    """★★★ 最重要 の全ページを取得（新しい順）"""
    pages   = []
    cursor  = None
    payload = {
        "filter": {
            "property": IMPORTANCE_PROP,
            "select":   {"equals": IMPORTANCE_VALUE}
        },
        "sorts": [{"property": "登録日", "direction": "descending"}]
    }
    while True:
        data = dict(payload)
        if cursor:
            data["start_cursor"] = cursor
        result = notion_request(token, f"databases/{database_id}/query", data=data)
        pages.extend(result.get("results", []))
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return pages

# ============================================================
# プロパティ取得ヘルパー
# ============================================================
def find_title(props):
    """title 型プロパティの値（プロパティ名を問わず）"""
    for val in props.values():
        if val.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in val.get("title", []))
    return ""

def get_text(props, key):
    p = props.get(key, {})
    if p.get("type") == "rich_text":
        return "".join(t.get("plain_text", "") for t in p.get("rich_text", []))
    return ""

def get_select(props, key):
    p = props.get(key, {})
    if p.get("type") == "select":
        s = p.get("select")
        return s.get("name", "") if s else ""
    return ""

def get_number(props, key):
    p = props.get(key, {})
    return p.get("number") if p.get("type") == "number" else None

def get_url(props, key):
    p = props.get(key, {})
    if p.get("type") == "url":
        return p.get("url") or ""
    if p.get("type") == "rich_text":
        return "".join(t.get("plain_text", "") for t in p.get("rich_text", []))
    return ""

def get_multi_select(props, key):
    """multi_select プロパティの値をリストで返す"""
    p = props.get(key, {})
    if p.get("type") == "multi_select":
        return [s.get("name", "") for s in p.get("multi_select", [])]
    return []

def prop_val(props, key):
    """ジャーナル名など select or rich_text どちらでも取れるよう汎用的に"""
    v = get_select(props, key)
    return v if v else get_text(props, key)

# ============================================================
# 変換ヘルパー
# ============================================================
def journal_abbrev(name):
    for full, abbr in JOURNAL_ABBREV.items():
        if full.lower() in name.lower():
            return abbr
    return name[:30] if len(name) > 30 else name

def make_spine_title(title, max_len=65):
    if len(title) <= max_len:
        return title
    words = title.split()
    result, total = [], 0
    for w in words:
        if total + len(w) + 1 > max_len - 1:
            result.append("…")
            break
        result.append(w)
        total += len(w) + 1
    return " ".join(result)

def page_to_key(page_id):
    """Notion ページ ID → HTML data-paper キー（英数字8文字）"""
    return "n" + page_id.replace("-", "")[:8]

def pick_color(idx, used_colors):
    for color in COLORS:
        if color not in used_colors:
            return color
    return COLORS[idx % len(COLORS)]

def esc(s):
    """JS テンプレートリテラル内で安全に使えるようエスケープ"""
    return (s or "").replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")

def esc_dq(s):
    """JS ダブルクォート文字列内エスケープ（改行・バックスラッシュ・引用符）"""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")

# ============================================================
# HTML / JS テキスト生成
# ============================================================
def build_book_html(key, notion_id, color, j_abbr, year, spine_title, full_title):
    safe_label = make_spine_title(full_title, 50)
    return (
        f'\n                        <article class="book book--{color}"\n'
        f'                                 onclick="openPaperModal(this)"\n'
        f'                                 onmouseenter="showBookPreview(this)"\n'
        f'                                 onmouseleave="hideBookPreview()"\n'
        f'                                 onfocus="showBookPreview(this)"\n'
        f'                                 onblur="hideBookPreview()"\n'
        f'                                 data-paper="{key}"\n'
        f'                                 data-notion-id="{notion_id}"\n'
        f'                                 role="button" tabindex="0"\n'
        f'                                 aria-label="{safe_label} の論文解説を読む">\n'
        f'                            <div class="book-spine">\n'
        f'                                <div class="spine-band spine-band--top"></div>\n'
        f'                                <div class="spine-top">{j_abbr} · {year}</div>\n'
        f'                                <div class="spine-title">{spine_title}</div>\n'
        f'                                <div class="spine-bottom">Commentary</div>\n'
        f'                                <div class="spine-band spine-band--bottom"></div>\n'
        f'                            </div>\n'
        f'                        </article>'
    )

def build_paper_data_entry(key, page):
    props    = page["properties"]
    title    = find_title(props)
    journal  = prop_val(props, "ジャーナル名")
    year     = str(get_number(props, "発行年") or "")
    doi      = get_url(props, "URL/DOI")
    summary  = get_text(props, "Notion AI要約")
    memo     = get_text(props, "私的メモ")
    disease  = "、".join(get_multi_select(props, "対象疾患"))   # multi_select
    design   = get_select(props, "研究デザイン")
    n        = get_number(props, "症例数")
    evidence = get_select(props, "エビデンスレベル")
    rank     = prop_val(props, "Journal Rank")

    summary_html = f'<p class="mb-6 text-[14.5px] leading-relaxed">{esc(summary)}</p>'
    if n or design:
        summary_html += (
            '<div class="grid grid-cols-1 md:grid-cols-2 gap-6 text-sm mt-6">'
            '<div><div class="text-zinc-400 text-xs mb-2 tracking-wider">STUDY INFO</div>'
            '<div class="space-y-2">'
            + (f'<div><strong>研究デザイン</strong>: {esc(design)}</div>' if design else '')
            + (f'<div><strong>症例数</strong>: {n}例</div>' if n else '')
            + (f'<div><strong>対象疾患</strong>: {esc(disease)}</div>' if disease else '')
            + (f'<div><strong>エビデンスレベル</strong>: {esc(evidence)}</div>' if evidence else '')
            + (f'<div><strong>Journal Rank</strong>: {esc(rank)}</div>' if rank else '')
            + '</div></div></div>'
        )

    memo_html = (
        f'<p><span class="inline-block text-[10px] bg-emerald-50 text-emerald-700 px-2 py-0.5 rounded mr-2 align-middle font-semibold tracking-wider">'
        f'大島の私見</span>{esc(memo)}</p>'
        if memo else
        '<p class="text-zinc-400 text-sm">（コメント準備中）</p>'
    )

    return (
        f'            {key}: {{\n'
        f'                journal: "{esc_dq(journal)}",\n'
        f'                citation: "{year}",\n'
        f'                title: "{esc_dq(title)}",\n'
        f'                doi: "{esc_dq(doi)}",\n'
        f'                summary: `{summary_html}`,\n'
        f'                commentary: `{memo_html}`\n'
        f'            }}'
    )

def build_preview_entry(key, page):
    props   = page["properties"]
    title   = find_title(props)
    journal = prop_val(props, "ジャーナル名")
    year    = str(get_number(props, "発行年") or "")
    summary = get_text(props, "Notion AI要約")
    j_abbr  = journal_abbrev(journal)
    eyebrow = f"{j_abbr} · {year}"
    preview = (summary[:120] + "…") if len(summary) > 120 else summary

    return (
        f'            {key}: {{\n'
        f'                eyebrow: "{esc_dq(eyebrow)}",\n'
        f'                title: "{esc_dq(title[:80])}",\n'
        f'                text: "{esc_dq(preview)}"\n'
        f'            }}'
    )

# ============================================================
# index.html 更新
# ============================================================
def get_existing_notion_ids(html):
    return set(re.findall(r'data-notion-id="([^"]+)"', html))

def get_used_colors(html):
    return set(re.findall(r'class="book book--(\w+)"', html))

def insert_books(html, new_html):
    """<!-- ▼ 既刊（書庫） --> の直後（最初のarticleの前）に挿入"""
    marker = '<!-- ▼ 既刊（書庫） -->'
    pos = html.find(marker)
    if pos == -1:
        # フォールバック: bookshelf-divider の後
        pos = html.find('<div class="bookshelf-divider"')
        if pos == -1:
            print("❌ 挿入位置（書庫コーナー）が見つかりません")
            return html
        pos = html.find("\n", pos) + 1
    else:
        pos = html.find("\n", pos) + 1  # マーカー行の末尾
    return html[:pos] + new_html + "\n" + html[pos:]

def insert_js_entries(html, pattern_str, new_entries):
    """JS オブジェクト（paperData / bookPreviews）の末尾に追加"""
    m = re.search(pattern_str + r'(\{.*?\})\s*;', html, re.DOTALL)
    if not m:
        print(f"❌ '{pattern_str}' が見つかりません")
        return html
    body = m.group(1)
    # 最後の } の直前に挿入
    new_body = body.rstrip()[:-1].rstrip() + ",\n" + new_entries + "\n        }"
    return html[:m.start(1)] + new_body + html[m.end(1):]

# ============================================================
# メイン
# ============================================================
def main():
    test_mode  = "--test"  in sys.argv
    debug_mode = "--debug" in sys.argv

    cfg         = load_config()
    token       = cfg["token"]
    database_id = cfg["database_id"]

    print("🔄 Notion データベースに接続中...")
    pages = fetch_all_important_pages(token, database_id)
    print(f"⭐ {IMPORTANCE_VALUE} 論文: {len(pages)} 件")

    if debug_mode and pages:
        p = pages[0]
        print(f"\n--- DEBUG: 最初の1件のプロパティ ---")
        print(f"ID: {p['id']}")
        print(f"タイトル: {find_title(p['properties'])}")
        for k, v in p["properties"].items():
            print(f"  {k}: type={v.get('type')}  → {json.dumps(v, ensure_ascii=False)[:80]}")
        return

    if test_mode:
        print("\n--- テストモード（HTMLは変更しません）---")
        for page in pages:
            props = page["properties"]
            print(f"  ID: {page['id']}")
            print(f"     {find_title(props)[:70]}")
        print(f"\n合計 {len(pages)} 件")
        return

    html = HTML_PATH.read_text(encoding="utf-8")
    existing_ids = get_existing_notion_ids(html)
    used_colors  = get_used_colors(html)

    new_pages = [p for p in pages if p["id"] not in existing_ids]
    print(f"✨ 新規追加: {len(new_pages)} 件  (スキップ: {len(pages) - len(new_pages)} 件)")

    if not new_pages:
        print("✅ 追加対象なし。最新の状態です。")
        return

    books_html = ""
    pd_entries = []
    bp_entries = []

    for idx, page in enumerate(new_pages):
        notion_id = page["id"]
        key       = page_to_key(notion_id)
        props     = page["properties"]
        title     = find_title(props)
        journal   = prop_val(props, "ジャーナル名")
        year      = str(get_number(props, "発行年") or "")
        color     = pick_color(idx, used_colors)
        used_colors.add(color)

        j_abbr     = journal_abbrev(journal)
        spine_title = make_spine_title(title)

        print(f"  + [{color}] {title[:55]}...")
        books_html += build_book_html(key, notion_id, color, j_abbr, year, spine_title, title)
        pd_entries.append(build_paper_data_entry(key, page))
        bp_entries.append(build_preview_entry(key, page))

    html = insert_books(html, books_html)
    html = insert_js_entries(html, r'const paperData\s*=\s*', ",\n".join(pd_entries))
    html = insert_js_entries(html, r'const bookPreviews\s*=\s*', ",\n".join(bp_entries))

    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"\n✅ {len(new_pages)} 冊を追加しました！")
    print("次のステップ:")
    print("  git add index.html")
    print('  git commit -m "Sync papers from Notion"')
    print("  git push")

if __name__ == "__main__":
    main()
