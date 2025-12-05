import os
import json
import base64
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

# 銘柄ごとの環境情報を保存するディレクトリ
CONTEXT_DIR = "contexts"
os.makedirs(CONTEXT_DIR, exist_ok=True)


# ---------- 共通ユーティリティ ----------

def to_data_url(img_bytes: bytes) -> str:
    """画像バイト列を data URL 文字列に変換"""
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _context_path(symbol: str) -> str:
    safe = symbol.replace("/", "_").replace("\\", "_")
    return os.path.join(CONTEXT_DIR, f"{safe}.json")


def load_all_contexts() -> dict:
    """全銘柄の環境情報を読み込む {symbol: data}"""
    contexts: dict[str, dict] = {}
    for fname in os.listdir(CONTEXT_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(CONTEXT_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            symbol = data.get("symbol") or os.path.splitext(fname)[0]
            contexts[symbol] = data
        except Exception:
            # 壊れたファイルは無視
            continue
    return contexts


def load_context(symbol: str) -> dict | None:
    path = _context_path(symbol)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_context(data: dict) -> None:
    symbol = (data.get("symbol") or "").strip()
    if not symbol:
        return
    path = _context_path(symbol)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- 日足＋材料の環境要約を作る ----------

def build_environment_summary(
    symbol: str,
    name: str,
    daily_bytes: bytes | None,
    extra_imgs: list[bytes],
    urls: list[str],
    memo_text: str,
) -> str:
    """
    日足画像・追加画像・URL・メモから「その銘柄の環境要約テキスト」を作る
    """
    contents: list[dict] = []

    # ここは英語ベース＋「日本語で答えて」と指示（文字コード的に安全）
    base_instruction = """
You are a Japanese stock market environment analyst.

User will provide:
- Daily candlestick chart image (optional)
- Additional reference images (news, higher timeframes, rankings, etc.)
- One or more URL texts (IR, news, etc.)
- User memo text

Please summarize the medium-term environment and main factors for the stock
in **Japanese**, within 200 characters.

Output format (MUST be valid JSON, nothing else):

{
  "summary": "日本語で200文字以内の要約"
}
"""
    contents.append({"type": "input_text", "text": base_instruction})

    if daily_bytes:
        contents.append(
            {
                "type": "input_text",
                "text": "This is the daily candlestick chart image.",
            }
        )
        contents.append({"type": "input_image", "image_url": to_data_url(daily_bytes)})

    for idx, b in enumerate(extra_imgs, start=1):
        contents.append(
            {
                "type": "input_text",
                "text": f"Additional reference image {idx}.",
            }
        )
        contents.append({"type": "input_image", "image_url": to_data_url(b)})

    cleaned_urls = [u.strip() for u in urls if u.strip()]
    for idx, url in enumerate(cleaned_urls, start=1):
        snippet = ""
        try:
            resp = requests.get(url, timeout=5)
            text = resp.text
            snippet = textwrap.shorten(text, width=2000, placeholder="...")
        except Exception as e:
            snippet = f"(URL fetch error: {e})"

        contents.append(
            {
                "type": "input_text",
                "text": f"URL {idx}: {url}\nUse this page when summarizing.\nExcerpt:\n{snippet}",
            }
        )

    memo_text = memo_text.strip()
    if memo_text:
        contents.append(
            {
                "type": "input_text",
                "text": f"User memo:\n{memo_text}",
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
            summary = "要約生成に失敗しましたが、環境情報としてAIが内部的に利用します。"
    except Exception:
        summary = "要約の解析に失敗しました。環境情報の解釈はAIに任せてください。"

    # 銘柄名も含めて少しだけ補足
    prefix = f"{symbol} {name} の環境: "
    return prefix + summary


# ---------- 5分足＋板から短期アクションを判定 ----------

def analyze_intraday(
    symbol: str,
    chart_bytes: bytes,
    board_bytes: bytes,
    memo_text: str,
    env_summary: str | None,
):
    """
    5分足チャート＋板＋（任意）環境要約から
    buy / sell / hold と価格レンジのヒントを返す
    """

    contents: list[dict] = []

    base_instruction = """
You are a Japanese short-term trading advisor for Japanese stocks.

User will provide:
- 5-minute candlestick chart image
- Order book (板) image
- Optional memo text
- Optional pre-saved environment summary for this stock

Task:
1. Judge the very short-term action (several minutes to tens of minutes):
   - "buy"  (new/added long entry is favorable)
   - "sell" (taking profit or cutting loss is reasonable)
   - "hold" (waiting is safer)

2. Suggest reasonable price ranges (in yen) for:
   - If action = "buy" : recommended entry price range
   - If action = "sell": recommended selling price range
   - If action = "hold": suggested area to buy more, and area to sell if price extends

Output format (MUST be valid JSON, nothing else):

{
  "action": "buy | sell | hold",
  "reason": "日本語で100〜150文字程度の理由",
  "buy_range": "買うなら ◯◯〜◯◯円 など。不要なら空文字",
  "sell_range": "売るなら ◯◯〜◯◯円 など。不要なら空文字",
  "hold_plan": "ホールド時の追加買い/売りの目安レンジ。不要なら空文字"
}
"""
    contents.append({"type": "input_text", "text": base_instruction})

    if env_summary:
        contents.append(
            {
                "type": "input_text",
                "text": f"Pre-saved environment summary (Japanese):\n{env_summary}",
            }
        )

    contents.append(
        {
            "type": "input_text",
            "text": "This is the 5-minute candlestick chart image.",
        }
    )
    contents.append({"type": "input_image", "image_url": to_data_url(chart_bytes)})

    contents.append(
        {
            "type": "input_text",
            "text": "This is the order book (板) image.",
        }
    )
    contents.append({"type": "input_image", "image_url": to_data_url(board_bytes)})

    memo_text = memo_text.strip()
    if memo_text:
        contents.append(
            {
                "type": "input_text",
                "text": f"User memo:\n{memo_text}",
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
        buy_range = str(data.get("buy_range", "")).strip()
        sell_range = str(data.get("sell_range", "")).strip()
        hold_plan = str(data.get("hold_plan", "")).strip()
    except Exception:
        action = "hold"
        reason = "出力解析エラーのため様子見としました。"
        buy_range = ""
        sell_range = ""
        hold_plan = ""

    if action not in ["buy", "sell", "hold"]:
        action = "hold"

    result = {
        "symbol": symbol,
        "action": action.upper(),
        "reason": reason,
        "buy_range": buy_range,
        "sell_range": sell_range,
        "hold_plan": hold_plan,
    }
    return result


# ---------- Flask ルーティング ----------

@app.route("/", methods=["GET", "POST"])
def index():
    error = None
    result = None

    # すべての登録済み銘柄（環境情報付き）
    contexts = load_all_contexts()
    # URL クエリで選択中の銘柄
    active_symbol = request.args.get("symbol", "").strip()

    # POST 処理
    if request.method == "POST":
        mode = request.form.get("mode", "")

        # --- 5分足＋板から短期アクションを判定 ---
        if mode == "judge":
            symbol = request.form.get("judge_symbol", "").strip()
            chart_file = request.files.get("chart_image")
            board_file = request.files.get("board_image")
            memo_text = request.form.get("judge_memo", "")

            if not symbol:
                error = "銘柄コードを入力してください。"
            elif not chart_file or not chart_file.filename:
                error = "5分足チャート画像を選択してください。"
            elif not board_file or not board_file.filename:
                error = "気配値（板）の画像を選択してください。"
            else:
                try:
                    chart_bytes = chart_file.read()
                    board_bytes = board_file.read()

                    env_data = load_context(symbol)
                    env_summary = env_data.get("summary") if env_data else None

                    result = analyze_intraday(
                        symbol=symbol,
                        chart_bytes=chart_bytes,
                        board_bytes=board_bytes,
                        memo_text=memo_text,
                        env_summary=env_summary,
                    )
                    active_symbol = symbol
                except Exception as e:
                    # ここで ascii まわりの例外も全部まとめてメッセージにする
                    error = f"判定中にエラーが発生しました: {e}"

        # --- 日足・材料などの環境情報を登録／更新 ---
        elif mode == "update_env":
            symbol = request.form.get("env_symbol", "").strip()
            name = request.form.get("env_name", "").strip()
            daily_file = request.files.get("daily_image")
            extra_files = request.files.getlist("extra_images")
            urls_text = request.form.get("env_urls", "")
            env_memo = request.form.get("env_memo", "")

            if not symbol:
                error = "銘柄コード（環境登録用）は必須です。"
            else:
                daily_bytes = (
                    daily_file.read()
                    if daily_file is not None and daily_file.filename
                    else None
                )
                extra_imgs = [
                    f.read()
                    for f in extra_files
                    if f is not None and f.filename
                ]
                urls = urls_text.splitlines()

                try:
                    summary = build_environment_summary(
                        symbol=symbol,
                        name=name,
                        daily_bytes=daily_bytes,
                        extra_imgs=extra_imgs,
                        urls=urls,
                        memo_text=env_memo,
                    )
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    data = {
                        "symbol": symbol,
                        "name": name,
                        "summary": summary,
                        "updated_at": now_str,
                    }
                    save_context(data)
                    # 登録後の一覧を更新
                    contexts = load_all_contexts()
                    active_symbol = symbol
                except Exception as e:
                    error = f"環境情報更新中にエラーが発生しました: {e}"

    # テンプレに渡す用にソートした一覧を作る
    registered_list = []
    for sym, data in contexts.items():
        registered_list.append(
            {
                "symbol": sym,
                "name": data.get("name", ""),
                "updated_at": data.get("updated_at", ""),
            }
        )
    registered_list.sort(key=lambda x: x["symbol"])

    active_env = load_context(active_symbol) if active_symbol else None

    return render_template(
        "index.html",
        error=error,
        result=result,
        registered_list=registered_list,
        active_symbol=active_symbol,
        active_env=active_env,
    )


if __name__ == "__main__":
    # Render 上では gunicorn から呼ばれるので、ローカル開発用
    app.run(host="0.0.0.0", port=5000, debug=True)
