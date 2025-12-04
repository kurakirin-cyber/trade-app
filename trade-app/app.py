import base64
import json
import os
import textwrap
from datetime import datetime

import requests
from flask import Flask, render_template, request
from openai import OpenAI

# ====★ 自分の OpenAI APIキーを入れる ★====
OPENAI_API_KEY = "sk-ここに自分のキーを貼る"
# =========================================

client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)

# 銘柄ごとの環境サマリを保存するファイル
# 形式: {
#   "6954": {"summary": "...", "updated_at": "..."},
#   "4503": {...}
# }
CONTEXT_FILE = "global_context.json"


# --------- 共通ユーティリティ ---------
def load_all_contexts():
    """保存している全銘柄の環境サマリを読み込む"""
    if not os.path.exists(CONTEXT_FILE):
        return {}
    try:
        with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def save_context_for_symbol(symbol: str, summary: str):
    """銘柄ごとの環境サマリを保存（上書き）"""
    symbol = symbol.strip()
    if not symbol:
        return

    all_ctx = load_all_contexts()
    all_ctx[symbol] = {
        "summary": summary,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_ctx, f, ensure_ascii=False, indent=2)


def get_context_for_symbol(symbol: str):
    """銘柄ごとの環境サマリを取得"""
    symbol = symbol.strip()
    if not symbol:
        return None, None
    all_ctx = load_all_contexts()
    info = all_ctx.get(symbol)
    if not info:
        return None, None
    return info.get("summary"), info.get("updated_at")


def to_data_url(img_bytes: bytes) -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# --------- 環境（日足＋追加資料）を更新する処理 ---------
def build_environment_summary(
    daily_bytes: bytes | None,
    extra_imgs: list[bytes],
    urls: list[str],
    memo_text: str,
    symbol: str,
):
    """
    日足画像（任意）、追加画像複数、URL群、テキストメモを GPT に渡して
    『その銘柄の当面の環境サマリ』を作ってもらう。
    """

    contents = []

    base_instruction = f"""あなたは日本株のトレード環境アナリストです。
対象銘柄: {symbol}

ユーザーがアップロードした「日足チャート」「材料に関する画像」「URL」「テキストメモ」から、
その銘柄の当面の相場環境を日本語で短く要約してください。

・日足チャート: 中長期〜数日スパンのトレンド（上昇/下降/レンジ）、重要なサポート・レジスタンス
・追加画像: ニュース、上位足、ランキング情報など
・URL: IR資料やニュース記事など
・メモ: ユーザーの補足コメント

出力フォーマットは、必ず次の JSON 形式だけにしてください:

{{
  "summary": "日本語で200文字以内の要約（地合い、日足トレンド、材料の方向性など）"
}}

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
    memo_text = memo_text.strip()
    if memo_text:
        contents.append(
            {
                "type": "input_text",
                "text": f"ユーザーからの環境メモ:\n{memo_text}",
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


# --------- 個別の5分足＋板を判定する処理 ---------
def analyze_intraday(
    chart_bytes: bytes,
    board_bytes: bytes,
    memo_text: str,
    env_summary: str | None,
    symbol: str,
):
    """
    5分足チャート＋板＋（任意）環境要約を元に、BUY/SELL/HOLD を判定。
    ついでに価格レンジも出してもらう。
    """
    contents = []

    base_instruction = f"""あなたは日本株の短期トレードアドバイザーです。
対象銘柄: {symbol}

以下の情報を総合的に使って、ごく短期（数分〜数十分）目線のアクションを判断してください。

必ず以下の3択から1つだけ選んでください:
- "buy"  : 新規買い or 追加エントリーが有利そうな場合
- "sell" : 利確や損切りを含め、売りが妥当そうな場合
- "hold" : まだ様子見が無難な場合

また、アクションに応じておおまかな価格レンジも出してください:

- buy のとき:
  - buy_range: 「買いを狙いたい価格帯」（例: "1230〜1250円"）
- sell のとき:
  - sell_range: 「売りたい価格帯」（例: "1290〜1310円"）
- hold のとき:
  - add_buy_range: 「もし追加で買うならこの辺が無難という価格帯」

出力フォーマットは次の JSON のみとします:

{{
  "action": "buy | sell | hold",
  "reason": "日本語で120文字以内の理由（5分足の形、出来高、板、環境サマリも含めて具体的に）",
  "buy_range": "buyのときの買いレンジ。その他のときは空文字か省略可",
  "sell_range": "sellのときの売りレンジ。その他のときは空文字か省略可",
  "add_buy_range": "holdのときの追加買いレンジ。その他のときは空文字か省略可"
}}

余計な文章やコメントは一切書かず、必ずこの JSON だけを返してください。
"""
    contents.append({"type": "input_text", "text": base_instruction})

    # 環境要約（あれば）
    if env_summary:
        contents.append(
            {
                "type": "input_text",
                "text": f"【事前に登録された環境・日足・材料の要約】\n{env_summary}",
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
    memo_text = memo_text.strip()
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
        reason = str(data.get("reason", "理由なし")).strip()
        buy_range = str(data.get("buy_range", "")).strip()
        sell_range = str(data.get("sell_range", "")).strip()
        add_buy_range = str(data.get("add_buy_range", "")).strip()
    except Exception:
        action = "hold"
        reason = "出力解析エラーのため様子見としました。"
        buy_range = ""
        sell_range = ""
        add_buy_range = ""

    if action not in ["buy", "sell", "hold"]:
        action = "hold"

    return action.upper(), reason, buy_range, sell_range, add_buy_range


# --------- Flask ルーティング ---------
@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error = None
    env_message = None

    # 判定側で使う環境サマリ（その銘柄分）
    judge_symbol = ""
    judge_env_summary = None
    judge_env_updated_at = None

    # 環境登録フォーム側での最新情報
    env_symbol = ""
    env_summary_for_symbol = None
    env_updated_at_for_symbol = None

    if request.method == "POST":
        mode = request.form.get("mode")

        # --- 判定モード（5分足＋板） ---
        if mode == "judge":
            judge_symbol = request.form.get("symbol", "").strip()
            chart_file = request.files.get("chart_image")
            board_file = request.files.get("board_image")
            memo_text = request.form.get("memo", "")

            if not judge_symbol:
                error = "銘柄コードまたは銘柄名を入力してください。"
            elif not chart_file or not chart_file.filename:
                error = "5分足チャートの画像ファイルを選択してください。"
            elif not board_file or not board_file.filename:
                error = "気配値（板）の画像ファイルを選択してください。"
            else:
                try:
                    # その銘柄の環境サマリを自動で読み込む
                    judge_env_summary, judge_env_updated_at = get_context_for_symbol(
                        judge_symbol
                    )

                    chart_bytes = chart_file.read()
                    board_bytes = board_file.read()
                    action, reason, buy_range, sell_range, add_buy_range = (
                        analyze_intraday(
                            chart_bytes=chart_bytes,
                            board_bytes=board_bytes,
                            memo_text=memo_text,
                            env_summary=judge_env_summary,
                            symbol=judge_symbol,
                        )
                    )
                    result = {
                        "symbol": judge_symbol,
                        "action": action,
                        "reason": reason,
                        "buy_range": buy_range,
                        "sell_range": sell_range,
                        "add_buy_range": add_buy_range,
                    }
                except Exception as e:
                    error = f"判定中にエラーが発生しました: {e}"

        # --- 環境更新モード（日足＋追加資料を更新） ---
        elif mode == "update_env":
            env_symbol = request.form.get("env_symbol", "").strip()
            daily_file = request.files.get("daily_image")
            extra_files = request.files.getlist("extra_images")
            urls_text = request.form.get("env_urls", "")
            env_memo = request.form.get("env_memo", "")

            if not env_symbol:
                error = "環境を登録したい銘柄コード（または銘柄名）を入力してください。"
            else:
                daily_bytes = (
                    daily_file.read()
                    if daily_file and daily_file.filename
                    else None
                )
                extra_imgs = [f.read() for f in extra_files if f and f.filename]
                urls = urls_text.splitlines()

                try:
                    summary = build_environment_summary(
                        daily_bytes=daily_bytes,
                        extra_imgs=extra_imgs,
                        urls=urls,
                        memo_text=env_memo,
                        symbol=env_symbol,
                    )
                    save_context_for_symbol(env_symbol, summary)
                    env_summary_for_symbol, env_updated_at_for_symbol = (
                        get_context_for_symbol(env_symbol)
                    )
                    env_message = f"銘柄「{env_symbol}」の環境情報を更新しました。"
                except Exception as e:
                    error = f"環境更新中にエラーが発生しました: {e}"

    return render_template(
        "index.html",
        result=result,
        error=error,
        env_message=env_message,
        judge_symbol=judge_symbol,
        judge_env_summary=judge_env_summary,
        judge_env_updated_at=judge_env_updated_at,
        env_symbol=env_symbol,
        env_summary_for_symbol=env_summary_for_symbol,
        env_updated_at_for_symbol=env_updated_at_for_symbol,
    )


if __name__ == "__main__":
    # Render / スマホからも見えるように 0.0.0.0 で起動
    app.run(host="0.0.0.0", port=5000, debug=True)
