import base64
import json
import os
import textwrap
import requests
from datetime import datetime
from flask import Flask, render_template, request
from openai import OpenAI

# ====★ 自分の OpenAI APIキーを入れる ★====
OPENAI_API_KEY = "sk-ここに自分のキーを貼る"
# =========================================

client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)

CONTEXT_FILE = "global_context.json"


# ========= 共通ユーティリティ =========
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_context_symbols() -> dict:
    """
    保存済みの銘柄ごとの環境情報を読み込む。
    戻り値: { "6954": {"summary": "...", "updated_at": "...", "name": "ファナック"}, ... }
    旧フォーマット（summary だけ）も一応読み替え対応。
    """
    if not os.path.exists(CONTEXT_FILE):
        return {}

    try:
        with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    # 新フォーマット
    if isinstance(data, dict) and "symbols" in data:
        symbols = data.get("symbols") or {}
        if isinstance(symbols, dict):
            return symbols

    # 旧フォーマット（1銘柄ぶんしかないやつ）を救済
    if isinstance(data, dict) and "summary" in data:
        return {
            "_default": {
                "summary": data.get("summary", ""),
                "updated_at": data.get("updated_at", _now_str()),
                "name": data.get("name", ""),
            }
        }

    return {}


def save_context_symbol(symbol: str, name: str, summary: str) -> None:
    """
    1銘柄ぶんの環境要約を保存（上書き）。
    """
    symbol = symbol.strip()
    name = (name or "").strip()

    all_symbols = load_context_symbols()
    all_symbols[symbol] = {
        "summary": summary,
        "updated_at": _now_str(),
        "name": name,
    }

    data = {
        "version": 2,
        "symbols": all_symbols,
    }

    with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def to_data_url(img_bytes: bytes) -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# ========= 日足＋材料などの環境要約を作る =========
def build_environment_summary(
    daily_bytes: bytes | None,
    extra_imgs: list[bytes],
    urls: list[str],
    memo_text: str,
):
    """
    日足画像（任意）、追加画像複数、URL群、テキストメモを GPT に渡して
    「今日の環境・材料」要約テキストを作ってもらう。
    """
    contents = []

    base_instruction = """あなたは日本株のトレード環境アナリストです。
ユーザーがアップロードした「日足チャート」「材料に関する画像」「URL」「テキストメモ」から、
その銘柄の当面の相場環境を日本語でコンパクトに要約してください。

・日足チャート: 中長期〜数日スパンのトレンド（上昇/下降/レンジ）、重要な高値安値、移動平均との位置など
・追加画像: ニュース、上位足、ランキング情報など
・URL: IR資料やニュース記事などのテキスト情報
・メモ: ユーザーの補足コメント

出力フォーマットは、必ず次の JSON 形式だけにしてください:

{
  "summary": "日本語で200文字以内の要約（地合い、日足トレンド、材料の方向性など）"
}

余計な文章やコメントは一切書かず、必ずこの JSON だけを返してください。
"""
    contents.append({"type": "input_text", "text": base_instruction})

    # 日足画像
    if daily_bytes:
        contents.append(
            {
                "type": "input_text",
                "text": "▼これは日足チャートの画像です。中期トレンドや重要なサポート/レジスタンスを読み取ってください。",
            }
        )
        contents.append({"type": "input_image", "image_url": to_data_url(daily_bytes)})

    # 追加画像
    for idx, b in enumerate(extra_imgs, start=1):
        contents.append(
            {
                "type": "input_text",
                "text": f"▼追加参考画像 {idx}。ニュースや上位足など、環境把握に役立つ情報です。",
            }
        )
        contents.append({"type": "input_image", "image_url": to_data_url(b)})

    # URLの中身を取得してテキストとして渡す
    cleaned_urls = [u.strip() for u in urls if u.strip()]
    for idx, url in enumerate(cleaned_urls, start=1):
        snippet = ""
        try:
            resp = requests.get(url, timeout=5)
            text = resp.text
            # 長すぎると重いので先頭だけ
            snippet = textwrap.shorten(text, width=2000, placeholder="...")
        except Exception as e:
            snippet = f"(URL取得エラー: {e})"

        contents.append(
            {
                "type": "input_text",
                "text": f"▼URL {idx}: {url}\nこのページの内容要約に使ってください。\n抜粋テキスト:\n{snippet}",
            }
        )

    # メモ
    memo_text = (memo_text or "").strip()
    if memo_text:
        contents.append(
            {
                "type": "input_text",
                "text": f"ユーザーからのメモ:\n{memo_text}",
            }
        )

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[{"role": "user", "content": contents}],
        max_output_tokens=400,
    )

    try:
        output_text = response.output[0].content[0].text
        data = json.loads(output_text)
        summary = str(data.get("summary", "")).strip()
        if not summary:
            summary = "要約生成に失敗しましたが、環境情報として利用してください。"
    except Exception:
        summary = "要約の解析に失敗しました。環境情報の解釈はAIに任せてください。"

    return summary


# ========= 5分足＋板から短期アクション判定 =========
def analyze_intraday(
    chart_bytes: bytes,
    board_bytes: bytes,
    memo_text: str,
    env_summary: str | None,
):
    """
    5分足チャート＋板＋（任意）環境要約を元に、BUY/SELL/HOLD を判定。
    ついでに「買い/売りの狙い目レンジ」も出してもらう。
    """
    contents = []

    base_instruction = """あなたは日本株の短期トレードアドバイザーです。
以下の情報を総合的に使って、ごく短期（数分〜数十分）目線のアクションを判断してください。

必ず以下の3択から1つだけ選んでください:
- "buy"  : 新規買い or 追加エントリーが有利そうな場合
- "sell" : 利確 or 損切りを含む売りが妥当そうな場合
- "hold" : まだ様子見が無難な場合

また、可能であれば現在値付近から見て
・「買うならこのあたりの価格帯が良さそう」というざっくりレンジ
・「売るならこのあたりの価格帯が良さそう」というざっくりレンジ
も提案してください（分からない場合は null で構いません）。

理由はできるだけ具体的に書き、
「どのあたりの高値・安値」「5分足の形」「板の厚さ・偏り」など
何を見てその判断に至ったかを簡潔に説明してください。

出力フォーマットは次の JSON のみとします:

{
  "action": "buy | sell | hold",
  "reason": "日本語で120文字以内の理由",
  "buy_range": "買いの狙い目レンジ（例: '3300-3350'。不要なら null または空文字）",
  "sell_range": "売りの狙い目レンジ（例: '3450-3500'。不要なら null または空文字）"
}

余計な文章やコメントは一切書かず、必ずこの JSON だけを返してください。
"""
    contents.append({"type": "input_text", "text": base_instruction})

    # 環境要約（あれば）
    if env_summary:
        contents.append(
            {
                "type": "input_text",
                "text": f"【事前に登録された日足・材料などの環境要約】\n{env_summary}",
            }
        )

    # 5分足チャート
    contents.append(
        {
            "type": "input_text",
            "text": "▼これは5分足チャートです。直近の値動きと出来高から短期トレンドを判断してください。",
        }
    )
    contents.append({"type": "input_image", "image_url": to_data_url(chart_bytes)})

    # 板
    contents.append(
        {
            "type": "input_text",
            "text": "▼これは現在の気配値（板）の画像です。売り板・買い板の厚さや偏りを判断に使ってください。",
        }
    )
    contents.append({"type": "input_image", "image_url": to_data_url(board_bytes)})

    # メモ
    memo_text = (memo_text or "").strip()
    if memo_text:
        contents.append(
            {
                "type": "input_text",
                "text": f"ユーザーからの補足メモ:\n{memo_text}",
            }
        )

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[{"role": "user", "content": contents}],
        max_output_tokens=400,
    )

    try:
        output_text = response.output[0].content[0].text
        data = json.loads(output_text)
        action = str(data.get("action", "hold")).lower()
        reason = str(data.get("reason", "理由なし"))
        buy_range = data.get("buy_range") or ""
        sell_range = data.get("sell_range") or ""
        buy_range = str(buy_range).strip()
        sell_range = str(sell_range).strip()
    except Exception:
        action = "hold"
        reason = "出力解析エラーのため様子見としました。"
        buy_range = ""
        sell_range = ""

    if action not in ["buy", "sell", "hold"]:
        action = "hold"

    result = {
        "action": action.upper(),
        "reason": reason,
        "buy_range": buy_range,
        "sell_range": sell_range,
    }

    return result


# ========= Flask ルーティング =========
@app.route("/", methods=["GET", "POST"])
def index():
    # すべての環境データを読み込み（銘柄一覧用）
    symbol_map = load_context_symbols()
    env_list = []
    for sym, info in symbol_map.items():
        env_list.append(
            {
                "symbol": sym,
                "name": info.get("name", ""),
                "summary": info.get("summary", ""),
                "updated_at": info.get("updated_at", ""),
            }
        )

    result = None
    error = None
    env_message = None

    # どの銘柄が選択中か
    selected_symbol = None
    selected_info = None

    if request.method == "POST":
        mode = request.form.get("mode")
        # 画面上の共通銘柄入力から hidden にコピーされたやつ
        symbol_judge = (request.form.get("symbol_judge") or "").strip()
        symbol_env = (request.form.get("symbol_env") or "").strip()
        selected_symbol = symbol_judge or symbol_env

        # --- まず 5分足＋板による判定（頻度高いのでこちらを先） ---
        if mode == "judge":
            if not symbol_judge:
                error = "銘柄コードを入力してください。"
            else:
                chart_file = request.files.get("chart_image")
                board_file = request.files.get("board_image")
                memo_text = request.form.get("memo", "")

                if not chart_file or not chart_file.filename:
                    error = "5分足チャートの画像ファイルを選択してください。"
                elif not board_file or not board_file.filename:
                    error = "気配値（板）の画像ファイルを選択してください。"
                else:
                    try:
                        chart_bytes = chart_file.read()
                        board_bytes = board_file.read()
                        env_summary = None
                        if symbol_judge in symbol_map:
                            env_summary = symbol_map[symbol_judge].get("summary") or None

                        result = analyze_intraday(
                            chart_bytes=chart_bytes,
                            board_bytes=board_bytes,
                            memo_text=memo_text,
                            env_summary=env_summary,
                        )
                    except Exception as e:
                        error = f"判定中にエラーが発生しました: {e}"

        # --- 次に 日足＋材料などの環境登録（任意・上書き） ---
        elif mode == "update_env":
            if not symbol_env:
                error = "環境情報を登録する銘柄コードを入力してください。"
            else:
                daily_file = request.files.get("daily_image")
                extra_files = request.files.getlist("extra_images")
                urls_text = request.form.get("env_urls", "")
                env_memo = request.form.get("env_memo", "")
                stock_name = request.form.get("stock_name", "")

                daily_bytes = (
                    daily_file.read() if daily_file and daily_file.filename else None
                )
                extra_imgs = [f.read() for f in extra_files if f and f.filename]
                urls = urls_text.splitlines()

                try:
                    summary = build_environment_summary(
                        daily_bytes=daily_bytes,
                        extra_imgs=extra_imgs,
                        urls=urls,
                        memo_text=env_memo,
                    )
                    save_context_symbol(symbol_env, stock_name, summary)

                    # 再読込して一覧も更新
                    symbol_map = load_context_symbols()
                    env_list = []
                    for sym, info in symbol_map.items():
                        env_list.append(
                            {
                                "symbol": sym,
                                "name": info.get("name", ""),
                                "summary": info.get("summary", ""),
                                "updated_at": info.get("updated_at", ""),
                            }
                        )

                    env_message = f"銘柄 {symbol_env} の環境情報を更新しました。"
                except Exception as e:
                    error = f"環境更新中にエラーが発生しました: {e}"

    # 選択中銘柄の情報（モーダル用）
    if selected_symbol and selected_symbol in symbol_map:
        selected_info = symbol_map[selected_symbol]

    return render_template(
        "index.html",
        result=result,
        error=error,
        env_message=env_message,
        env_list=env_list,
        selected_symbol=selected_symbol,
        selected_info=selected_info,
    )


if __name__ == "__main__":
    # Render / ローカル両対応用
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
