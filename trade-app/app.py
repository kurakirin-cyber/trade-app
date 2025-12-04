import base64
import json
import os
import textwrap
import requests
from datetime import datetime
from typing import Optional, Dict, Any

from flask import Flask, render_template, request
from openai import OpenAI

# ====★ OpenAI APIキーを環境変数から読む ★====
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が環境変数に設定されていません。")

client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)

# 銘柄ごとの環境まとめを保存するファイル
CONTEXT_FILE = "global_context.json"


# --------- 共通ユーティリティ ---------
def load_all_context() -> Dict[str, Any]:
    """全銘柄分の環境ファイルを読み込む"""
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


def load_context_for_symbol(symbol: str) -> Optional[Dict[str, str]]:
    """特定銘柄の環境要約を取得"""
    symbol = symbol.strip()
    if not symbol:
        return None
    all_ctx = load_all_context()
    return all_ctx.get(symbol)


def save_context_for_symbol(symbol: str, summary: str) -> None:
    """特定銘柄の環境要約を保存（上書き）"""
    symbol = symbol.strip()
    if not symbol:
        return

    all_ctx = load_all_context()
    all_ctx[symbol] = {
        "summary": summary,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_ctx, f, ensure_ascii=False, indent=2)


def to_data_url(img_bytes: bytes) -> str:
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# --------- 環境（日足＋追加資料）を更新する処理 ---------
def build_environment_summary(
    daily_bytes: Optional[bytes],
    extra_imgs: list[bytes],
    urls: list[str],
    memo_text: str,
) -> str:
    """
    日足画像（任意）、追加画像複数、URL群、テキストメモを GPT に渡して
    「今日の環境・材料」要約テキストを作ってもらう。
    """
    contents = []

    base_instruction = """あなたは日本株のトレード環境アナリストです。
ユーザーがアップロードした「日足チャート」「材料に関する画像」「URL」「テキストメモ」から、
その銘柄の当面の相場環境を日本語で短く要約してください。

・日足チャート: 中長期〜数日スパンのトレンド（上昇/下降/レンジ）、移動平均との位置など
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


# --------- 個別の5分足＋板を判定する処理 ---------
def analyze_intraday(
    chart_bytes: bytes,
    board_bytes: bytes,
    memo_text: str,
    env_summary: Optional[str],
    symbol: str,
):
    """
    5分足チャート＋板＋（任意）環境要約を元に、BUY/SELL/HOLD を判定。
    併せて、価格帯の目安も返す（チャートから価格が読めない場合は空文字）。
    """
    contents = []

    base_instruction = """あなたは日本株の短期トレードアドバイザーです。
対象銘柄について、以下の情報を総合的に使って、ごく短期（数分〜数十分）目線のアクションを判断してください。

必ず以下の3択から1つだけ選んでください:
- "buy"  : 新規買い or 追加エントリーが有利そうな場合
- "sell" : 利確 or 損切りを含む売りが妥当そうな場合
- "hold" : まだ様子見が無難な場合

理由はできるだけ具体的にしてください。
・直近の5分足トレンド（上昇/下降/レンジ、押し目/戻りなど）
・重要な価格帯（直近高値/安値、サポート・レジスタンス、VWAP 付近かどうか）
・板の偏り（買い板と売り板の厚さ、成行気配の方向）
・リスク要因（直近高値が近い、出来高が乏しい、乱高下している など）

あわせて、チャートの価格軸が読める場合は、具体的な価格帯の目安も出してください:
- "buy" の場合   : 「買いを入れたい価格帯」を buy_range に
- "sell" の場合  : 「売りたい／手仕舞いしたい価格帯」を sell_range に
- "hold" の場合  : 「追加で拾うなら狙いたい押し目の価格帯」を add_buy_range に
※価格が読み取れない場合は、その項目は空文字 "" のままにしてください。

出力フォーマットは次の JSON のみとします:
{
  "action": "buy | sell | hold",
  "reason": "日本語で200文字以内。2〜3個の具体的根拠を「・」で区切って列挙すること。",
  "buy_range": "例: 2120〜2150円 のような表現。該当しなければ空文字。",
  "sell_range": "例: 2300〜2330円 のような表現。該当しなければ空文字。",
  "add_buy_range": "例: 2050〜2080円 のような表現。該当しなければ空文字。"
}

余計な文章は一切書かず、必ずこの JSON だけを返してください。
"""
    contents.append({"type": "input_text", "text": base_instruction})

    # 銘柄名
    symbol = symbol.strip()
    if symbol:
        contents.append(
            {
                "type": "input_text",
                "text": f"【対象銘柄】{symbol}",
            }
        )

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
        max_output_tokens=380,
    )

    try:
        output_text = response.output[0].content[0].text
        data = json.loads(output_text)
        action = str(data.get("action", "hold")).lower()
        reason = str(data.get("reason", "理由なし"))
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
    symbol = (request.values.get("symbol") or "").strip().upper()

    env_summary = None
    env_updated_at = None
    result = None
    env_message = None
    error = None

    # 既存環境の読み込み（GET 時や POST 前に表示用）
    if symbol:
        ctx = load_context_for_symbol(symbol)
        if ctx:
            env_summary = ctx.get("summary")
            env_updated_at = ctx.get("updated_at")

    if request.method == "POST":
        mode = request.form.get("mode")

        # symbol は上で values から取っているが、念のため再取得
        symbol = (request.form.get("symbol") or "").strip().upper()

        if not symbol:
            error = "銘柄名／コードを入力してください。"
        else:
            # --- 判定モード（5分足＋板） ---
            if mode == "judge":
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
                        # 判定時にも最新の環境を取り直す
                        ctx = load_context_for_symbol(symbol)
                        env_summary_for_judge = ctx.get("summary") if ctx else None
                        action, reason, buy_range, sell_range, add_buy_range = analyze_intraday(
                            chart_bytes=chart_bytes,
                            board_bytes=board_bytes,
                            memo_text=memo_text,
                            env_summary=env_summary_for_judge,
                            symbol=symbol,
                        )
                        result = {
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
                daily_file = request.files.get("daily_image")
                extra_files = request.files.getlist("extra_images")
                urls_text = request.form.get("env_urls", "")
                env_memo = request.form.get("env_memo", "")

                daily_bytes = daily_file.read() if daily_file and daily_file.filename else None
                extra_imgs = [f.read() for f in extra_files if f and f.filename]
                urls = urls_text.splitlines()

                try:
                    summary = build_environment_summary(
                        daily_bytes=daily_bytes,
                        extra_imgs=extra_imgs,
                        urls=urls,
                        memo_text=env_memo,
                    )
                    save_context_for_symbol(symbol, summary)
                    env_summary = summary
                    env_ctx = load_context_for_symbol(symbol)
                    if env_ctx:
                        env_updated_at = env_ctx.get("updated_at")
                    env_message = f"{symbol} の環境・日足情報を更新しました。"
                except Exception as e:
                    error = f"環境更新中にエラーが発生しました: {e}"

    return render_template(
        "index.html",
        symbol=symbol,
        result=result,
        error=error,
        env_summary=env_summary,
        env_updated_at=env_updated_at,
        env_message=env_message,
    )


if __name__ == "__main__":
    # ローカル用（Render では gunicorn から起動）
    app.run(host="0.0.0.0", port=5000, debug=True)
